"""Terminal state, navigation, and event loop for asdu."""

from __future__ import annotations

import curses
import os
import re
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import asdu_browser as model
import asdu_sessions as store
import asdu_sources as source
import asdu_views as view
from asdu_sessions import (
    Session,
    SessionControls,
)

CONTENT_RIGHT_MARGIN = 3


class ScanProgress:
    """TTY-only startup banner and in-place scan progress."""

    def __init__(
        self,
        enabled: bool,
        render: Callable[[str, int, int, int, int], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.render = render
        self.interactive = enabled and sys.stderr.isatty()
        self.wrote_line = False
        self.panel_left = 0
        self.has_panel = False
        self.skipped: set[Path] = set()
        self.invalid: set[Path] = set()
        if self.interactive and render is None:
            try:
                width = os.get_terminal_size(sys.stderr.fileno()).columns - 1
            except OSError:
                width = 79
            self.has_panel = width >= 48
            self.panel_left = max(0, (width - 48) // 2)
            lines = view.splash_lines(width)
            if self.has_panel:
                lines += [""] + [
                    " " * self.panel_left + line
                    for line in view.indexing_panel("", 0, 0, 0)
                ]
            sys.stderr.write("\n".join(lines) + "\n")
            sys.stderr.flush()

    def paint(
        self, source: str, current: int, count: int, done: int, total: int
    ) -> None:
        if self.has_panel:
            content = view.indexing_panel(source, current, done, total)[1:4]
            sys.stderr.write("\033[4A")
            for index, line in enumerate(content):
                sys.stderr.write("\r" + " " * self.panel_left + line + "\033[K")
                if index < len(content) - 1:
                    sys.stderr.write("\n")
            sys.stderr.write("\033[2B\r")
            sys.stderr.flush()
            return
        try:
            width = os.get_terminal_size(sys.stderr.fileno()).columns - 1
        except OSError:
            width = 79
        sys.stderr.write(
            "\r"
            + view.scan_status(source, current, count, done, total, width)
            + "\033[K"
        )
        sys.stderr.flush()
        self.wrote_line = True

    def update(
        self, source: str, current: int, total: int, done_bytes: int, total_bytes: int
    ) -> None:
        if not self.enabled or total == 0:
            return
        # Disk usage is the reason for the scan, so the bar tracks bytes
        # rather than treating a thousand tiny rollouts like one huge one.
        done_bytes = min(done_bytes, total_bytes)
        if self.render is not None:
            self.render(source, current, total, done_bytes, total_bytes)
            return
        if not self.interactive:
            return
        self.paint(source, current, total, done_bytes, total_bytes)

    def finish(self) -> None:
        if self.interactive and self.wrote_line:
            sys.stderr.write("\n")
        if self.interactive:
            sys.stderr.flush()


WHEEL_UP = curses.KEY_MAX + 1
WHEEL_DOWN = curses.KEY_MAX + 2
CTRL_F = 6
SEARCH_KEYS = (CTRL_F, ord("/"))
# Terminal wheel reports have no gesture-end marker. A quiet gap separates
# gestures; reversing the wheel also starts a new gesture immediately.
WHEEL_IDLE_SECONDS = 0.25


class TerminalWindow:
    """Keep terminal replies out of commands, including replies arriving late."""

    def __init__(self, window):
        self.raw = window
        self.in_osc = False
        self.osc_escape = False
        self.delay = -1
        self.last_wheel = None
        self.wheel_direction = None
        self.wheel_cancelled = False

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def timeout(self, delay):
        self.delay = delay
        self.raw.timeout(delay)

    def nodelay(self, enabled):
        self.delay = 0 if enabled else -1
        self.raw.nodelay(enabled)

    def getch(self):
        while True:
            key = self._getch()
            now = time.monotonic()
            active = (
                self.last_wheel is not None
                and now - self.last_wheel < WHEEL_IDLE_SECONDS
            )
            if key in (WHEEL_UP, WHEEL_DOWN):
                if not active or key != self.wheel_direction:
                    self.wheel_cancelled = False
                self.last_wheel, self.wheel_direction = now, key
                if self.wheel_cancelled:
                    continue
                return curses.KEY_UP if key == WHEEL_UP else curses.KEY_DOWN
            if key != -1 and active:
                self.wheel_cancelled = True
            return key

    def _getch(self):
        for _ in range(256):
            key = self.raw.getch()
            if key == -1:
                return key
            if key == curses.KEY_MOUSE:
                try:
                    buttons = curses.getmouse()[4]
                except curses.error:
                    return -1
                if buttons & curses.BUTTON4_PRESSED:
                    return WHEEL_UP
                if buttons & getattr(curses, "BUTTON5_PRESSED", 0):
                    return WHEEL_DOWN
                return -1
            if self.in_osc:
                if key == 7 or (self.osc_escape and key == ord("\\")):
                    self.in_osc = False
                self.osc_escape = key == 27
                continue
            if key != 27:
                return key
            self.raw.timeout(50)
            try:
                following = self.raw.getch()
                if following in (ord("["), ord("O")):
                    # A multiplexer can send normal-mode arrows while terminfo
                    # expects application-mode arrows (or the reverse).
                    sequence = ""
                    for _ in range(32):
                        part = self.raw.getch()
                        if not 0 <= part < 128:
                            return -1
                        sequence += chr(part)
                        if 0x40 <= part <= 0x7E:
                            break
                    if sequence.startswith("<"):
                        report = re.fullmatch(r"<(\d+);\d+;\d+M", sequence)
                        if report:
                            button = int(report[1]) & ~28  # Shift/Alt/Ctrl bits.
                            return {64: WHEEL_UP, 65: WHEEL_DOWN}.get(button, -1)
                        return -1
                    return {
                        "A": curses.KEY_UP,
                        "B": curses.KEY_DOWN,
                        "C": curses.KEY_RIGHT,
                        "D": curses.KEY_LEFT,
                        "H": curses.KEY_HOME,
                        "F": curses.KEY_END,
                        "5~": curses.KEY_PPAGE,
                        "6~": curses.KEY_NPAGE,
                    }.get(sequence, -1)
            finally:
                self.raw.timeout(self.delay)
            if following == ord("]"):
                self.in_osc = True
                self.osc_escape = False
                continue
            if following != -1:
                curses.ungetch(following)
            return key
        return -1


def read_navigation(window: curses.window, first: bool, last: bool) -> int:
    """Ignore repeated boundary keys without rebuilding or redrawing the view."""
    while True:
        key = window.getch()
        if first and key in (
            curses.KEY_UP,
            ord("k"),
            curses.KEY_PPAGE,
            curses.KEY_HOME,
        ):
            continue
        if last and key in (
            curses.KEY_DOWN,
            ord("j"),
            curses.KEY_NPAGE,
            curses.KEY_END,
        ):
            continue
        return key


def drain_navigation(window, key, selected, positions):
    """Apply queued arrows in order before drawing; retain the next command."""
    directions = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1}
    if key not in directions or not positions:
        return selected, key
    index = positions.index(selected)
    window.nodelay(True)
    try:
        # Like ncdu, finish pending navigation before redrawing. Do not replay
        # wheel-derived arrows through ungetch: that loses their provenance.
        while True:
            index = min(len(positions) - 1, max(0, index + directions[key]))
            key = window.getch()
            if key not in directions:
                return positions[index], key
    finally:
        window.nodelay(False)


def cycle_value(current: str, options: list[str]) -> str:
    """Advance a small visible setting without opening another screen."""
    try:
        return options[(options.index(current) + 1) % len(options)]
    except ValueError:
        return options[0]


@dataclass
class TuiState:
    sessions: list[Session]
    mode: str
    sort_by: str
    cwd_root: Path
    browser: model.BrowserState
    source_roots: dict[str, Path] = field(default_factory=dict)
    source_filter: str = "all"
    type_filter: str = "all"
    tree_mode: bool = False
    collapsed_nodes: set[str] = field(default_factory=set)
    query: str = ""
    status: str = ""
    search_results: list[Session] | None = None
    search_return: tuple[int, int] | None = None
    mode_state: dict[tuple[str, Path], tuple[str, int, int]] = field(
        default_factory=dict
    )
    view_key: tuple | None = None
    view_data: tuple | None = None

    def current_view(self):
        """Cache only the current view; navigation must not resolve paths again."""
        key = (
            self.source_filter,
            self.type_filter,
            self.mode,
            self.browser.cwd_node,
            self.sort_by,
        )
        if key != self.view_key:
            visible, grouped = model.browser_visible_sessions(
                self.sessions,
                self.source_filter,
                self.type_filter,
                self.mode,
                self.browser.cwd_node,
            )
            items = (
                model.cwd_listing(visible, self.browser.cwd_node, self.sort_by)
                if self.mode == "cwd"
                else [
                    ("session", session.title, [session])
                    for session in model.ordered_sessions(grouped, self.sort_by)
                ]
                if self.mode == "all"
                else model.browser_group_items(grouped, self.mode, self.sort_by)
            )
            scoped = (
                grouped
                if self.mode == "cwd" and self.browser.cwd_node == Path("/")
                else [s for s in grouped if store.in_scope(s, self.browser.cwd_node)]
                if self.mode == "cwd"
                else grouped
            )
            self.view_data = visible, grouped, items, scoped
            self.view_key = key
        return self.view_data

    def filtered_sessions(self) -> list[Session]:
        return [
            session
            for session in self.sessions
            if (self.source_filter == "all" or session.source == self.source_filter)
            and (
                self.type_filter == "all"
                or model.session_type(session) == self.type_filter
            )
        ]


@dataclass
class TuiFrame:
    page_size: int
    visible: list[Session]
    grouped_visible: list[Session]
    items: list[tuple[str, str, list[Session]]]
    tree_entries: list[Session]
    display_entries: list[tuple[Session, str]]
    entry_children: dict[str, list[str]]
    tree_parents: dict[int, int | None]
    tree_children: dict[int | None, list[int]]
    total: int
    selected_entry: Session | None


def refresh_tui_view(state: TuiState, frame: TuiFrame) -> None:
    """Keep an open group anchored after a filter, rescan, action, or sort."""
    _, current, current_items, _ = state.current_view()
    browser = state.browser
    if browser.detail is None:
        return
    anchor = (
        model.row_id(frame.display_entries[browser.selected][0])
        if frame.display_entries
        else None
    )
    name = browser.detail[0]
    entries = (
        [s for s in current if Path(s.cwd).resolve() == browser.cwd_node]
        if state.mode == "cwd"
        else next((es for _, key, es in current_items if key == name), [])
    )
    if not entries:
        browser.leave_detail()
        return
    browser.detail = (name, entries)
    state.tree_mode, state.collapsed_nodes = browser.open_group(
        state.mode,
        name,
        state.source_filter,
        state.type_filter,
        browser.cwd_node,
        model.tree_with_ancestors(entries, current),
    )
    rows = model.tree_rows(
        entries,
        current,
        state.sort_by,
        state.tree_mode,
        state.collapsed_nodes,
    )
    browser.selected = next(
        (i for i, (entry, _) in enumerate(rows) if model.row_id(entry) == anchor),
        browser.selected,
    )
    browser.selected, browser.offset = model.clamp_view(
        browser.selected, browser.offset, len(rows), frame.page_size
    )


def tree_navigation(
    positions: dict[str, int], entries: Iterable[Session], enabled: bool
) -> tuple[dict[str, list[str]], dict[int, int | None], dict[int | None, list[int]]]:
    """Build the relationships used by sibling and parent/child navigation."""
    links = model.parent_links(entries) if enabled else {}
    entry_children: dict[str, list[str]] = defaultdict(list)
    for identifier in positions:
        parent = links.get(identifier)
        if parent in positions:
            entry_children[parent].append(identifier)
    parents = {
        position: positions.get(links.get(identifier))
        for identifier, position in positions.items()
    }
    children: dict[int | None, list[int]] = defaultdict(list)
    for position, parent in parents.items():
        children[parent].append(position)
    return entry_children, parents, children


def draw_session_rows(
    window,
    entries,
    browser,
    page_size,
    content_row,
    largest,
    stats=None,
    show_folder=False,
) -> None:
    stats = stats or {}
    for index, (session, branch) in enumerate(
        entries[browser.offset : browser.offset + page_size], browser.offset
    ):
        view.draw_session_line(
            window,
            index - browser.offset + content_row,
            session,
            index == browser.selected,
            branch,
            stats.get(model.row_id(session)),
            largest,
            session.cwd if show_folder else "",
        )


def render_tui_frame(window: curses.window, state: TuiState) -> TuiFrame:
    """Render one browser frame and return only the data needed by key handling."""
    window.erase()
    height, width = window.getmaxyx()
    visible, grouped_visible, base_items, scoped = state.current_view()
    browser = state.browser
    searching = state.search_results is not None
    show_columns = (
        searching
        or browser.detail is not None
        or state.mode == "all"
        or bool(base_items)
        and all(item[0] == "session" for item in base_items)
    )
    content_row = 2 if show_columns else 1
    base_page_size = max(1, height - content_row - 2)
    page_size = base_page_size
    if browser.detail is not None and not browser.detail[1]:
        browser.leave_detail()
        return render_tui_frame(window, state)

    branches: dict[str, str] = {}
    display_entries: list[tuple[Session, str]] = []
    entry_children: dict[str, list[str]] = defaultdict(list)
    tree_parents: dict[int, int | None] = {}
    tree_children: dict[int | None, list[int]] = defaultdict(list)
    if searching:
        entries = model.ordered_sessions(state.search_results or [], state.sort_by)
        items = []
        tree_entries = entries
        display_entries = [(entry, "") for entry in entries]
        browser.selected, browser.offset = model.clamp_view(
            browser.selected, browser.offset, len(entries), page_size
        )
        location = f'find "{state.query}"'
        largest_session = max((entry.size for entry in entries), default=0)
        draw_session_rows(
            window,
            display_entries,
            browser,
            page_size,
            content_row,
            largest_session,
            show_folder=True,
        )
        total = len(display_entries)
    elif browser.detail is None:
        items = base_items
        if state.mode in {"cwd", "all"}:
            direct = (
                [entries[0] for kind, _, entries in items if kind == "session"]
                if state.mode == "cwd"
                else grouped_visible
            )
            tree_entries = model.tree_with_ancestors(direct, scoped)
            state.tree_mode, state.collapsed_nodes = browser.open_group(
                state.mode,
                str(browser.cwd_node) if state.mode == "cwd" else "all sessions",
                state.source_filter,
                state.type_filter,
                browser.cwd_node,
                tree_entries,
                tree_by_default=state.mode == "cwd",
            )
            folder_rows = (
                [item for item in items if item[0] != "session"]
                if state.mode == "cwd"
                else []
            )
            session_rows = model.tree_rows(
                direct,
                scoped,
                state.sort_by,
                state.tree_mode,
                state.collapsed_nodes,
            )
            branches = {model.row_id(entry): branch for entry, branch in session_rows}
            items = folder_rows + [
                ("session", entry.title, [entry]) for entry, _ in session_rows
            ]
            if browser.pending_anchor is not None and state.tree_mode:
                links = model.parent_links(tree_entries)
                shown = {model.item_key(item) for item in items}
                while (
                    browser.pending_anchor not in shown
                    and browser.pending_anchor in links
                ):
                    browser.pending_anchor = links[browser.pending_anchor]
        elif state.mode == "source":
            state.tree_mode = False
            tree_entries = []
        first_session = next(
            (index for index, item in enumerate(items) if item[0] == "session"),
            None,
        )
        has_row_gap = first_session is not None and any(
            item[0] == "folder" for item in items[:first_session]
        )
        if has_row_gap:
            page_size = max(1, base_page_size - 1)
        if browser.pending_anchor is not None:
            browser.selected = next(
                (
                    i
                    for i, item in enumerate(items)
                    if model.item_key(item) == browser.pending_anchor
                ),
                browser.selected,
            )
            browser.pending_anchor = None
        browser.selected, browser.offset = model.clamp_view(
            browser.selected, browser.offset, len(items), page_size
        )
        location = (
            model.relative_folder(browser.cwd_node, state.cwd_root)
            if state.mode == "cwd"
            else f"{model.relative_folder(browser.cwd_node, state.cwd_root)}  sources"
            if state.mode == "source"
            else f"{model.relative_folder(browser.cwd_node, state.cwd_root)}  all sessions"
        )
        stats = model.subtree_stats(tree_entries) if state.tree_mode else {}
        largest_session = max(
            (
                stats.get(model.row_id(entries[0]), (entries[0].size, 0))[0]
                for kind, _, entries in items
                if kind == "session"
            ),
            default=0,
        )
        largest_group = max(
            (sum(item.size for item in entries) for _, _, entries in items),
            default=0,
        )
        for index, (kind, name, entries) in enumerate(
            items[browser.offset : browser.offset + page_size], browser.offset
        ):
            row = (
                index
                - browser.offset
                + content_row
                + int(
                    has_row_gap
                    and first_session is not None
                    and browser.offset < first_session <= index
                )
            )
            if row >= height - 2:
                break
            if kind == "session":
                view.draw_session_line(
                    window,
                    row,
                    entries[0],
                    index == browser.selected,
                    branches.get(model.row_id(entries[0]), "")
                    if state.tree_mode
                    else "",
                    stats.get(model.row_id(entries[0])),
                    largest_session,
                )
                continue
            display_name = "/" + name if kind == "folder" else name
            group_size = sum(item.size for item in entries)
            bar_width = view.row_bar_width(width)
            bar = (
                f"{view.size_bar(group_size, largest_group, bar_width)}  "
                if bar_width
                else ""
            )
            text = f"{view.human_size(group_size):>10}  {bar}{len(entries):>5}  {display_name}"
            view.draw_line(
                window,
                row,
                "  " + text,
                index == browser.selected,
                0,
                pointer=True,
                right_margin=CONTENT_RIGHT_MARGIN,
            )
        total = len(items)
    else:
        name, entries = browser.detail
        items = base_items
        tree_entries = (
            model.tree_with_ancestors(entries, grouped_visible)
            if state.tree_mode
            else entries
        )
        display_entries = model.tree_rows(
            entries,
            grouped_visible,
            state.sort_by,
            state.tree_mode,
            state.collapsed_nodes,
        )
        if browser.pending_anchor is not None:
            browser.selected = next(
                (
                    i
                    for i, (entry, _) in enumerate(display_entries)
                    if model.row_id(entry) == browser.pending_anchor
                ),
                browser.selected,
            )
            browser.pending_anchor = None
        positions = {
            model.row_id(entry): index
            for index, (entry, _) in enumerate(display_entries)
        }
        entry_children, tree_parents, tree_children = tree_navigation(
            positions, tree_entries, state.tree_mode
        )
        browser.selected, browser.offset = model.clamp_view(
            browser.selected, browser.offset, len(display_entries), page_size
        )
        location = (
            f"{model.relative_folder(browser.cwd_node, state.cwd_root)}  {name}"
            if state.mode == "source"
            else name
        )
        stats = model.subtree_stats(tree_entries) if state.tree_mode else {}
        largest_session = max((entry.size for entry, _ in display_entries), default=0)
        draw_session_rows(
            window,
            display_entries,
            browser,
            page_size,
            content_row,
            largest_session,
            stats,
        )
        total = len(display_entries)

    if browser.detail is None and state.mode in {"cwd", "all"}:
        positions = {
            model.item_key(item): i
            for i, item in enumerate(items)
            if item[0] == "session"
        }
        entry_children, tree_parents, tree_children = tree_navigation(
            positions, tree_entries, state.tree_mode
        )
        if state.tree_mode and state.mode == "cwd":
            folder_positions = [
                index for index, item in enumerate(items) if item[0] == "folder"
            ]
            tree_children[None] = sorted(folder_positions + tree_children[None])
            tree_parents.update({position: None for position in folder_positions})

    active_filters = [
        "?" if value == "unknown" else value
        for value in (state.source_filter, state.type_filter)
        if value != "all"
    ]
    if total == 0 and not searching:
        location_text = view.short_path(str(browser.cwd_node))
        sources = sorted(
            state.source_roots
            or {s.source: Path(s.source_home) for s in state.sessions}
        )
        names = ", ".join("OMP" if name == "omp" else name.title() for name in sources)
        message = (
            f"  No sessions match: {', '.join(active_filters)}"
            if active_filters
            else f"  No {names or 'supported'} sessions were started in {location_text}."
        )
        view.draw_line(window, 3, message)
        row = 5
        view.draw_line(window, row, "  Scanned", bold=True)
        row += 1
        for source_name in sources:
            entries = [
                session for session in state.sessions if session.source == source_name
            ]
            root = state.source_roots.get(source_name)
            path = view.short_path(str(root)) if root else source_name
            view.draw_line(
                window,
                row,
                f"  {path:<28}  {len(entries):>6,} sessions  "
                f"{view.human_size(sum(session.size for session in entries)):>10}",
                dim=True,
            )
            row += 1
        if browser.cwd_node != Path("/") and row < height - 2:
            parent = browser.cwd_node.parent
            parent_entries = [
                session
                for session in visible
                if parent == Path("/") or store.in_scope(session, parent)
            ]
            view.draw_line(
                window,
                row + 1,
                f"  Press Backspace to go up to {view.short_path(str(parent))} "
                f"({len(parent_entries):,} sessions, "
                f"{view.human_size(sum(session.size for session in parent_entries))})",
            )

    footer_sessions = (
        state.search_results
        if searching
        else browser.detail[1]
        if browser.detail is not None
        else scoped
    )
    footer_size = sum(session.size for session in footer_sessions)
    label = f" asdu  {location}"
    if active_filters:
        label += "  " + "  ".join(active_filters)
    count = f"{len(footer_sessions):,} sessions"
    if active_filters and browser.detail is None and not searching:
        unfiltered_scope = (
            state.sessions
            if browser.cwd_node == Path("/")
            else [
                session
                for session in state.sessions
                if store.in_scope(session, browser.cwd_node)
            ]
        )
        count = f"{len(unfiltered_scope):,} → {len(footer_sessions):,} sessions"
    ordering = (
        f"{view.human_size(footer_size)}  {count}  {model.sort_label(state.sort_by)} "
    )
    available = max(0, width - 1 - view.display_width(ordering))
    view.draw_line(
        window,
        0,
        view.pad_display(view.compact_text(label, available), available) + ordering,
        bold=True,
        invert=True,
    )
    if show_columns:
        view.draw_line(
            window,
            1,
            view.compact_text(
                view.session_column_header(width, searching),
                max(0, width - CONTENT_RIGHT_MARGIN),
            ),
            dim=True,
        )
    selected_entry = (
        display_entries[browser.selected][0]
        if (searching or browser.detail is not None) and display_entries
        else items[browser.selected][2][0]
        if browser.detail is None and items and items[browser.selected][0] == "session"
        else None
    )
    has_sessions = (
        searching
        or browser.detail is not None
        or any(kind == "session" for kind, _, _ in items)
    )
    commands = ["Enter open"] if total else []
    if (
        searching
        or browser.detail is not None
        or (state.mode == "cwd" and browser.cwd_node != Path("/"))
    ):
        commands.append("Backspace back")
    if selected_entry is not None and source.available_actions(selected_entry):
        commands.append("a action")
    if (
        has_sessions
        and not searching
        and (browser.detail is not None or state.mode in {"cwd", "all"})
    ):
        commands.append("t tree")
    commands.append("/ find")
    if browser.detail is None:
        commands.append("g group")
    commands.extend(["f source", "F type", "s sort"])
    selected_info = (
        f"{selected_entry.source} {selected_entry.session_id[:18]} | "
        f"{datetime.fromtimestamp(selected_entry.modified, datetime.now().astimezone().tzinfo).strftime('%Y-%m-%d %H:%M')} | "
        f"{view.short_path(str(selected_entry.path))}"
        if selected_entry is not None
        else ""
    )
    view.draw_line(
        window,
        height - 2,
        " " + (state.status or selected_info),
        dim=not state.status,
    )
    footer_tail = "? help  q quit"
    footer_width = max(0, width - 1)
    left_width = max(0, footer_width - view.display_width(footer_tail) - 2)
    footer = (
        view.pad_display(
            view.compact_text(" " + "  ".join(commands), left_width), left_width
        )
        + "  "
        + footer_tail
        if left_width
        else view.compact_text(footer_tail, footer_width)
    )
    view.draw_line(window, height - 1, footer, invert=True)
    window.refresh()

    return TuiFrame(
        page_size=page_size,
        visible=visible,
        grouped_visible=grouped_visible,
        items=items,
        tree_entries=tree_entries,
        display_entries=display_entries,
        entry_children=entry_children,
        tree_parents=tree_parents,
        tree_children=tree_children,
        total=total,
        selected_entry=selected_entry,
    )


ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
BACK_KEYS = (curses.KEY_BACKSPACE, 127, 8)
UP_KEYS = (curses.KEY_UP, ord("k"))
DOWN_KEYS = (curses.KEY_DOWN, ord("j"))


def handle_list_navigation(state: TuiState, frame: TuiFrame, key: int) -> bool:
    """Move within the current list and report whether the key was consumed."""
    browser = state.browser
    if frame.total == 0 and key in (
        *UP_KEYS,
        *DOWN_KEYS,
        curses.KEY_HOME,
        curses.KEY_END,
        curses.KEY_NPAGE,
        curses.KEY_PPAGE,
    ):
        return True
    if key in (curses.KEY_HOME, curses.KEY_END):
        browser.selected = 0 if key == curses.KEY_HOME else max(0, frame.total - 1)
    elif key in DOWN_KEYS:
        browser.selected = min(frame.total - 1, browser.selected + 1)
    elif key in UP_KEYS:
        browser.selected = max(0, browser.selected - 1)
    elif key == curses.KEY_NPAGE:
        browser.selected = min(frame.total - 1, browser.selected + frame.page_size)
    elif key == curses.KEY_PPAGE:
        browser.selected = max(0, browser.selected - frame.page_size)
    else:
        return False
    return True


def handle_tree_navigation(state: TuiState, frame: TuiFrame, key: int) -> bool:
    """Apply sibling, parent/child, and folding keys in an active tree."""
    entry = frame.selected_entry
    if state.search_results is not None or not state.tree_mode:
        return False
    if key == ord("z"):
        if state.browser.detail is None and frame.items:
            state.browser.pending_anchor = model.item_key(
                frame.items[state.browser.selected]
            )
        nodes_with_children = set(frame.entry_children)
        if nodes_with_children.issubset(state.collapsed_nodes):
            state.collapsed_nodes.clear()
        else:
            state.collapsed_nodes.update(nodes_with_children)
        return True
    if entry is None:
        return False
    identifier = model.row_id(entry)
    if key == ord(" "):
        if frame.entry_children.get(identifier):
            if identifier in state.collapsed_nodes:
                state.collapsed_nodes.remove(identifier)
            else:
                state.collapsed_nodes.add(identifier)
        return True
    if key not in (
        *UP_KEYS,
        *DOWN_KEYS,
        curses.KEY_LEFT,
        ord("h"),
        curses.KEY_RIGHT,
        ord("l"),
    ):
        return False
    browser = state.browser
    parent = frame.tree_parents.get(browser.selected)
    siblings = frame.tree_children.get(parent, [])
    position = siblings.index(browser.selected) if browser.selected in siblings else 0
    if key in DOWN_KEYS and position + 1 < len(siblings):
        browser.selected = siblings[position + 1]
    elif key in UP_KEYS and position > 0:
        browser.selected = siblings[position - 1]
    elif key in (curses.KEY_RIGHT, ord("l")):
        if identifier in state.collapsed_nodes:
            state.collapsed_nodes.remove(identifier)
        elif frame.tree_children.get(browser.selected):
            browser.selected = frame.tree_children[browser.selected][0]
    elif key in (curses.KEY_LEFT, ord("h")):
        if (
            frame.entry_children.get(identifier)
            and identifier not in state.collapsed_nodes
        ):
            state.collapsed_nodes.add(identifier)
        elif parent is not None:
            browser.selected = parent
    return True


def rescan_sessions(
    window: curses.window,
    state: TuiState,
    frame: TuiFrame,
    rescan: Callable[[Callable[[str, int, int, int, int], None]], list[Session]],
) -> None:
    """Rescan storage while preserving the current browser context."""
    browser = state.browser
    if browser.detail is None and frame.items:
        browser.pending_anchor = model.item_key(frame.items[browser.selected])

    def show_progress(
        source_name: str,
        current: int,
        total: int,
        done_bytes: int,
        total_bytes: int,
    ) -> None:
        window.erase()
        _, width = window.getmaxyx()
        view.draw_line(window, 0, " asdu  rescanning sessions ", bold=True, invert=True)
        status = view.scan_status(
            source_name,
            current,
            total,
            done_bytes,
            total_bytes,
            max(0, width - 3),
        )
        view.draw_line(window, 2, "  " + status)
        window.refresh()

    try:
        state.sessions[:] = rescan(show_progress)
    except OSError as error:
        state.status = f"Rescan failed: {error}"
        return
    state.view_key = None
    if state.search_results is not None:
        state.search_results = model.search_sessions(
            state.filtered_sessions(), state.query
        )
    refresh_tui_view(state, frame)


def run_session_action(
    window: curses.window, state: TuiState, frame: TuiFrame, entry: Session
) -> None:
    """Choose and execute one source-supported lifecycle action."""
    if not source.available_actions(entry):
        state.status = f"{entry.source.upper()} sessions are read-only."
        return
    subtree = model.session_subtree(state.sessions, entry)
    choice = view.confirm_session_action(window, entry, subtree)
    tree_action = bool(choice and choice.endswith(" tree"))
    action = choice.removesuffix(" tree") if choice else None
    targets = subtree if tree_action else [entry]
    targets = [
        target
        for target in targets
        if action is not None and action in source.available_actions(target)
    ]
    confirmed = (
        view.confirm_trash(window, len(targets))
        if action == "trash"
        else view.confirm_permanent_delete(window, len(targets))
        if action == "delete"
        else action is not None
    )
    if not confirmed:
        return
    try:
        source.prepare_session_actions(targets, action)
    except OSError as error:
        state.status = str(error)
        return

    result = {
        "archive": "Archived",
        "unarchive": "Unarchived",
        "trash": "Trashed",
        "delete": "Deleted",
    }[action]
    failures = []
    log_error = None
    changed = 0
    for target in targets:
        try:
            outcome = source.perform_prepared_action(target, action)
        except OSError as error:
            failures.append(str(error))
            break
        changed += 1
        try:
            store.record_action(action, target, outcome.destination)
        except OSError as error:
            log_error = error
        replacement = outcome.replacement
        views = [state.sessions]
        if state.browser.detail is not None:
            views.append(state.browser.detail[1])
        if state.search_results is not None:
            views.append(state.search_results)
        for entries in views:
            if target not in entries:
                continue
            position = entries.index(target)
            if replacement is None:
                entries.pop(position)
            else:
                entries[position] = replacement
    state.view_key = None
    if failures:
        state.status = f"{result} {changed}/{len(targets)}; stopped: {failures[0]}"
    else:
        freed = (
            0
            if action in {"archive", "unarchive"}
            else sum(target.size for target in targets[:changed])
        )
        if state.search_results is not None:
            current = state.search_results
        elif state.browser.detail is not None:
            current = state.browser.detail[1]
        else:
            current = state.current_view()[3]
        scope_total = sum(item.size for item in current)
        permanence = {
            "archive": "reversible",
            "unarchive": "restored",
            "trash": "recoverable",
            "delete": "permanent",
        }[action]
        count = f"{changed} sessions | " if changed > 1 else ""
        state.status = (
            f"{result} {count}{view.human_size(freed)} | {entry.source} "
            f"{entry.session_id[:18]} | {view.human_size(scope_total)} now | {permanence}"
        )
    if log_error is not None:
        state.status += f"; action log unavailable: {log_error}"


def cycle_filter(state: TuiState, frame: TuiFrame, key: int) -> None:
    """Cycle one of the two composable filters and preserve selection."""
    browser = state.browser
    if browser.detail is None and frame.items:
        browser.pending_anchor = model.item_key(frame.items[browser.selected])
    if key == ord("f"):
        options = ["all", *sorted({session.source for session in state.sessions})]
        state.source_filter = cycle_value(state.source_filter, options)
    else:
        options = ["all", "main", "child", "review", "unknown"]
        state.type_filter = cycle_value(state.type_filter, options)
    if state.search_results is not None:
        state.search_results = model.search_sessions(
            state.filtered_sessions(), state.query
        )
    refresh_tui_view(state, frame)


def cycle_sort(state: TuiState, frame: TuiFrame) -> None:
    """Cycle valid sorts for the current flat or grouped view."""
    browser = state.browser
    flat = (
        state.search_results is not None
        or browser.detail is not None
        or state.mode == "all"
    )
    browser.pending_anchor = (
        model.row_id(frame.display_entries[browser.selected][0])
        if (state.search_results is not None or browser.detail is not None)
        and frame.display_entries
        else model.item_key(frame.items[browser.selected])
        if frame.items
        else None
    )
    options = ["size", "date", "name"]
    if not flat:
        options.insert(2, "count")
    state.sort_by = cycle_value(state.sort_by, options)


def open_selected(window: curses.window, state: TuiState, frame: TuiFrame) -> None:
    """Open the selected folder, group, or session."""
    browser = state.browser
    if state.search_results is not None:
        if frame.display_entries:
            open_brief(
                window,
                frame.display_entries[browser.selected][0],
                state.search_results,
            )
        return
    if browser.detail is not None:
        entry = frame.display_entries[browser.selected][0]
        open_brief(
            window,
            entry,
            frame.tree_entries if state.tree_mode else frame.visible,
        )
        return
    if not frame.items:
        return
    kind, name, entries = frame.items[browser.selected]
    if state.mode == "all":
        open_brief(window, entries[0], frame.grouped_visible)
        return
    if state.mode == "cwd" and kind == "folder" and name != "(unknown folder)":
        browser.visit_folder(
            browser.cwd_node / name, model.item_key(frame.items[browser.selected])
        )
        return
    if state.mode == "cwd" and kind != "folder":
        open_brief(
            window,
            entries[0],
            frame.tree_entries if state.tree_mode else frame.visible,
        )
        return

    browser.detail = (name, entries)
    browser.detail_return = (
        model.item_key(frame.items[browser.selected]),
        browser.offset,
    )
    tree_entries = (
        entries
        if state.mode == "cwd"
        else model.tree_with_ancestors(entries, frame.grouped_visible)
    )
    state.tree_mode, state.collapsed_nodes = browser.open_group(
        state.mode,
        name,
        state.source_filter,
        state.type_filter,
        browser.cwd_node,
        tree_entries,
    )
    browser.selected, browser.offset = 0, 0


def go_back(state: TuiState, frame: TuiFrame) -> None:
    """Leave search/detail context or climb one virtual folder."""
    browser = state.browser
    if state.search_results is not None:
        state.search_results = None
        browser.selected, browser.offset = state.search_return or (0, 0)
        state.search_return = None
    elif browser.detail is not None:
        browser.leave_detail()
    elif state.mode == "cwd" and browser.cwd_node != Path("/"):
        browser.visit_folder(
            browser.cwd_node.parent,
            model.item_key(frame.items[browser.selected]) if frame.items else None,
        )


def handle_tui_key(
    window: curses.window,
    state: TuiState,
    frame: TuiFrame,
    key: int,
    rescan: Callable[[Callable[[str, int, int, int, int], None]], list[Session]],
) -> bool:
    """Apply one key to browser state; return false only when the TUI should exit."""
    browser = state.browser
    items = frame.items
    selected_entry = frame.selected_entry
    state.status = ""
    searching = state.search_results is not None
    if (
        not searching
        and browser.detail is None
        and state.mode in {"cwd", "all"}
        and selected_entry is None
    ):
        if key in (curses.KEY_RIGHT, ord("l")):
            key = 10
        elif key in (curses.KEY_LEFT, ord("h")):
            key = 127

    if key in SEARCH_KEYS:
        query = view.search_prompt(window)
        if query is None:
            return True
        if not searching:
            state.search_return = (browser.selected, browser.offset)
        state.query = query
        candidates = state.filtered_sessions()
        state.search_results = model.search_sessions(candidates, query)
        browser.selected, browser.offset = 0, 0
        state.status = (
            f"{len(state.search_results):,} matches"
            if state.search_results
            else f"No match: {query}"
        )
        return True
    if key in (curses.KEY_HOME, curses.KEY_END):
        handle_list_navigation(state, frame, key)
        return True
    if key in (ord("q"), 27):
        return False
    if key == ord("?"):
        text_view(
            window,
            "Help",
            "Sizes: saved conversation bytes, not project files. Updated: file modification time.\n"
            "Folders are based on each session's recorded working directory.\n\n"
            "Enter: open folder or session brief\n"
            "Ctrl-F or /: search all scanned sessions\n"
            "Home/End: first/last\n"
            "Backspace: parent folder or previous list\n"
            "r: rescan local session roots\n"
            "f: cycle source filter\n"
            "F: cycle session-type filter (all, main, child, review, ?)\n"
            "s: cycle sort\n"
            "g: switch between folder, source, and all-session views\n"
            "t: toggle session tree (↑↓ siblings, ←→ parent/child, Space fold, z all)\n"
            "a: source-supported session actions\n"
            "q: quit",
        )
        return True
    if key == ord("r"):
        rescan_sessions(window, state, frame, rescan)
        return True
    if handle_tree_navigation(state, frame, key):
        return True
    if handle_list_navigation(state, frame, key):
        return True
    if not searching and browser.detail is None and key == ord("g"):
        state.mode_state[(state.mode, browser.cwd_node)] = (
            state.sort_by,
            browser.selected,
            browser.offset,
        )
        previous_sort = state.sort_by
        state.mode = cycle_value(state.mode, ["cwd", "all", "source"])
        state.sort_by, browser.selected, browser.offset = state.mode_state.get(
            (state.mode, browser.cwd_node),
            ("size" if state.mode == "all" else previous_sort, 0, 0),
        )
        browser.pending_anchor = None
    elif (
        not searching
        and browser.detail is None
        and state.mode in {"cwd", "all"}
        and key == ord("t")
    ):
        browser.pending_anchor = (
            model.item_key(items[browser.selected]) if items else None
        )
        state.tree_mode, state.collapsed_nodes = browser.toggle_tree(frame.tree_entries)
    elif not searching and browser.detail is not None and key == ord("t"):
        contextual_tree = model.tree_with_ancestors(
            browser.detail[1], frame.grouped_visible
        )
        state.tree_mode, state.collapsed_nodes = browser.toggle_tree(contextual_tree)
        browser.selected, browser.offset = 0, 0
    elif key == ord("a") and selected_entry is not None:
        run_session_action(window, state, frame, selected_entry)
    elif key in (ord("f"), ord("F")):
        cycle_filter(state, frame, key)
    elif key == ord("s"):
        cycle_sort(state, frame)
    elif key in ENTER_KEYS:
        open_selected(window, state, frame)
    elif key in BACK_KEYS:
        go_back(state, frame)
    return True


def tui(
    sessions: list[Session],
    initial_mode: str,
    initial_sort: str,
    start: Path,
    rescan: Callable[[Callable[[str, int, int, int, int], None]], list[Session]],
    no_color: bool = False,
    source_roots: dict[str, Path] | None = None,
) -> None:
    """A small ncdu-like drill-down UI with explicit source-aware actions."""

    def run(raw_window: curses.window) -> None:
        window = TerminalWindow(raw_window)
        curses.curs_set(0)
        # Zellij owns pane-local selection and translates alternate-screen
        # scrolling into arrows. Capturing clicks here steals its selection.
        capture_mouse = not os.environ.get("ZELLIJ")
        try:
            down_button = getattr(curses, "BUTTON5_PRESSED", 0)
            curses.mousemask(
                curses.BUTTON4_PRESSED | down_button
                if capture_mouse and down_button
                else 0
            )
            curses.mouseinterval(0)
        except curses.error:
            pass
        if capture_mouse and sys.stdout.isatty():
            sys.stdout.write("\x1b[?1000h\x1b[?1006h")
            sys.stdout.flush()

        window.colors_enabled = (
            not (no_color or os.environ.get("NO_COLOR")) and curses.has_colors()
        )
        if window.colors_enabled:
            curses.start_color()
            curses.use_default_colors()
            colors = {
                1: curses.COLOR_GREEN,
                2: curses.COLOR_YELLOW,
                3: curses.COLOR_MAGENTA,
                4: curses.COLOR_CYAN,
                5: curses.COLOR_RED,
                6: curses.COLOR_BLUE,
                7: 33 if curses.COLORS >= 256 else curses.COLOR_CYAN,
                8: 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW,
                10: 37 if curses.COLORS >= 256 else curses.COLOR_GREEN,
            }
            for pair, color in colors.items():
                curses.init_pair(pair, color, -1)
            curses.init_pair(
                view.SELECTED_COLOR,
                222 if curses.COLORS >= 256 else curses.COLOR_YELLOW,
                236 if curses.COLORS >= 256 else curses.COLOR_BLACK,
            )

        cwd_root = start
        state = TuiState(
            sessions,
            initial_mode,
            initial_sort,
            cwd_root,
            model.BrowserState.create(cwd_root),
            source_roots or {},
        )
        while True:
            frame = render_tui_frame(window, state)
            positions = (
                frame.tree_children.get(
                    frame.tree_parents.get(state.browser.selected), []
                )
                if state.tree_mode and state.browser.selected in frame.tree_parents
                else range(frame.total)
            )
            first = not positions or state.browser.selected == positions[0]
            last = not positions or state.browser.selected == positions[-1]
            key = read_navigation(window, first, last)
            state.browser.selected, key = drain_navigation(
                window,
                key,
                state.browser.selected,
                positions,
            )
            if key != -1 and not handle_tui_key(window, state, frame, key, rescan):
                return

    try:
        curses.wrapper(run)
    finally:
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[?1000l\x1b[?1006l")
            sys.stdout.flush()
    clear_terminal()


class BriefCancelled(Exception):
    """Return to the browser without finishing the transcript scan."""


def open_brief(
    window: curses.window, session: Session, entries: Iterable[Session]
) -> None:
    """Display a bounded preview, then full results; only the UI thread draws."""
    sizes = view.brief_sizes(session, entries)
    known = [sizes, f"ID: {session.session_id}", f"Folder: {session.cwd}"]
    if session.task_path:
        known.append(f"Task: {session.task_path}")
    if session.forked_from:
        known.append(f"Forked from: {session.forked_from}")
    window.erase()
    view.draw_line(window, 0, store.session_label(session), bold=True)
    view.draw_line(window, 1, "Reading conversation…  Backspace return")
    for row, line in enumerate("\n".join(known).splitlines(), 3):
        view.draw_line(window, row, line, right_margin=CONTENT_RIGHT_MARGIN)
    window.refresh()

    cancelled = threading.Event()
    updates: list[str] = []

    def poll() -> None:
        if cancelled.is_set():
            raise BriefCancelled

    def load() -> None:
        try:
            body = view.digest(session, poll, True, SessionControls())
            tree = sizes.splitlines()[1:]
            updates.append(body + ("\n" + "\n".join(tree) if tree else ""))
            poll()
            controls = source.session_controls(session)
            body = view.digest(session, poll, False, controls)
            updates.append(body + ("\n" + "\n".join(tree) if tree else ""))
        except BriefCancelled:
            pass
        except Exception as error:  # noqa: BLE001 - report worker failures in the UI
            updates.append(
                f"Brief unavailable: {error}. Press r in the list to rescan."
            )

    worker = threading.Thread(target=load, daemon=True)
    worker.start()
    try:
        text_view(
            window,
            store.session_label(session),
            "\n".join(known),
            update=lambda: updates[-1] if updates else None,
        )
    finally:
        cancelled.set()
        window.nodelay(False)


def text_view(
    window: curses.window,
    title: str,
    body: str,
    update: Callable[[], str | None] | None = None,
) -> None:
    """Scrollable pane for a selected session's structural summary."""
    offset = 0
    query = ""
    match_index = -1
    last_frame = None
    while True:
        if update and (new_body := update()) is not None:
            body = new_body
        height, width = window.getmaxyx()
        lines: list[str] = []
        for raw in body.splitlines():
            lines.extend(
                view.wrap_cells(raw, max(1, min(120, width - 1 - CONTENT_RIGHT_MARGIN)))
            )
        offset = min(offset, max(0, len(lines) - max(1, height - 2)))
        frame = (body, offset, height, width)
        if update is None or frame != last_frame:
            window.erase()
            view.draw_line(
                window,
                0,
                view.compact_text(title, 120),
                bold=True,
                right_margin=CONTENT_RIGHT_MARGIN,
            )
            for row, line in enumerate(lines[offset : offset + height - 2], 2):
                view.draw_brief_line(window, row, line)
            window.refresh()
            last_frame = frame
        maximum = max(0, len(lines) - max(1, height - 2))
        if update:
            window.timeout(100)
        key = read_navigation(window, offset == 0, offset >= maximum)
        offset, key = drain_navigation(window, key, offset, range(maximum + 1))
        if key == -1:
            continue
        if key in (
            ord("q"),
            27,
            curses.KEY_BACKSPACE,
            127,
            8,
            curses.KEY_ENTER,
            10,
            13,
        ):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            offset = min(maximum, offset + 1)
        elif key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key == curses.KEY_NPAGE:
            offset = min(maximum, offset + max(1, height - 4))
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - max(1, height - 4))
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = maximum
        elif key in (*SEARCH_KEYS, ord("n"), ord("N")):
            if key in SEARCH_KEYS:
                entered = view.search_prompt(window)
                if entered is None:
                    continue
                query = entered
                match_index = offset - 1
            match_index = model.find_match(
                lines, query, match_index, -1 if key == ord("N") else 1
            )
            offset = min(maximum, max(0, match_index))


def clear_terminal() -> None:
    stream = sys.stderr if sys.stderr.isatty() else sys.stdout
    if stream.isatty():
        stream.write("\033[0m\033[?25h\033[2J\033[H")
        stream.flush()
