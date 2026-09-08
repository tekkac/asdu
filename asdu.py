#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["send2trash>=1.8.3"]
# ///
"""Disk-oriented browser for local agent-session transcripts.

Scanning and browsing are read-only. Session actions are deliberate and
source-aware.
"""

from __future__ import annotations

import argparse
import curses
import os
import re
import shlex
import sys
import threading
import time
import tomllib
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from asdu_browser import (
    BrowserState,
    browser_group_items,
    browser_visible_sessions,
    clamp_view,
    cwd_listing,
    find_match,
    group_label,
    group_sessions,
    item_key,
    ordered_groups,
    ordered_sessions,
    origin_label,
    parent_links,
    primary_tag,
    relative_folder,
    row_id,
    sort_label,
    subtree_stats,
    tree_rows,
    tree_with_ancestors,
)
from asdu_sources import (
    available_actions,
    perform_session_action,
    read_brief,
    scan,
    session_controls,
    source_adapters,
)
from asdu_sessions import (
    ContentCache,
    Session,
    SessionControls,
    disable_trash_confirmation,
    in_scope,
    load_tag_rules,
    record_action,
    session_label,
    skip_trash_confirmation,
)

ASCII_UI = False
CONTENT_RIGHT_MARGIN = 3


class ScanProgress:
    """Startup splash and progress for local session scans."""

    def __init__(
        self,
        enabled: bool,
        render: Callable[[str, int, int, int, int], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.render = render
        self.interactive = enabled and sys.stderr.isatty()
        self.last_percent = -1
        self.frame_rows = 0
        self.last_frame = None
        self.skipped: set[Path] = set()
        self.invalid: set[Path] = set()
        if self.interactive and render is None:
            self.paint("", 0, 0, 0)

    def paint(self, source: str, current: int, done: int, total: int) -> None:
        try:
            terminal = os.get_terminal_size(sys.stderr.fileno())
            width, height = terminal.columns - 1, terminal.lines
        except OSError:
            width, height = 79, 24
        lines = indexing_lines(
            min(width, 47) if height < 15 else width, source, current, done, total
        )
        top, left = splash_position(width, height, lines)
        lines = [""] * top + [" " * left + line for line in lines]
        if lines == self.last_frame:
            return
        styled = [
            f"\033[1m{line}\033[0m"
            if top <= row < top + 6 and height >= 15 and width >= 48
            else line
            for row, line in enumerate(lines)
        ]
        sys.stderr.write("\033[H\033[J" + "\n".join(styled) + "\n")
        sys.stderr.flush()
        self.frame_rows = len(lines)
        self.last_frame = lines

    def update(
        self, source: str, current: int, total: int, done_bytes: int, total_bytes: int
    ) -> None:
        if not self.enabled or total == 0:
            return
        # Disk usage is the reason for the scan, so the bar tracks bytes
        # rather than treating a thousand tiny rollouts like one huge one.
        done_bytes = min(done_bytes, total_bytes)
        percent = (
            int(done_bytes * 100 / total_bytes)
            if total_bytes
            else int(current * 100 / total)
        )
        if self.render is not None:
            self.render(source, current, total, done_bytes, total_bytes)
            return
        if self.interactive:
            self.paint(source, current, done_bytes, total_bytes)
            return
        # Do not flood redirected output; a terminal gets smooth updates.
        if not self.interactive and percent < 100 and percent == self.last_percent:
            return
        self.last_percent = percent
        try:
            columns = os.get_terminal_size(sys.stderr.fileno()).columns
        except OSError:
            columns = 80
        suffix = f" {percent:3d}% {source} {current:,}/{total:,} {human_size(done_bytes)}/{human_size(total_bytes)}"
        bar_width = max(0, min(26, columns - len(suffix) - 3))
        if bar_width >= 8:
            line = f"{progress_bar(percent, bar_width)}{suffix}"
        else:
            line = suffix.strip()
        line = line[: max(1, columns - 1)]
        print(line, file=sys.stderr, flush=True)

    def finish(self) -> None:
        if self.interactive:
            sys.stderr.flush()

def human_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    raise AssertionError("unreachable")


def session_date(session: Session) -> str:
    seconds = max(0, int(time.time() - session.modified))
    if seconds < 10:
        return "now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 30 * 86400:
        return f"{seconds // 86400}d ago"
    if seconds < 365 * 86400:
        return f"{seconds // (30 * 86400)}mo ago"
    return f"{seconds // (365 * 86400)}y ago"


def ascii_ui() -> bool:
    try:
        "╭█".encode(sys.stderr.encoding or "utf-8")
    except UnicodeEncodeError:
        return True
    return ASCII_UI


def terminal_art(text: str) -> str:
    """Keep content intact while replacing terminal decoration in ASCII mode."""
    if ascii_ui():
        return text.translate(
            str.maketrans(
                {
                    **dict.fromkeys("╭╮╰╯├┌└┐┘┤╔╗╚╝╠╣┏┓┗┛┣┫", "+"),
                    **dict.fromkeys("─━═", "-"),
                    **dict.fromkeys("│┃║", "|"),
                    "↑": "^",
                    "↓": "v",
                    "←": "<",
                    "→": ">",
                    "▸": ">",
                    "›": ">",
                    "▾": "v",
                    "…": "~",
                    "\u00b7": "/",
                }
            )
        )
    return text


def size_bar(size: int, largest: int, width: int = 12) -> str:
    """Fixed-cell relative usage, with eighth-cell precision for small items."""
    width = max(0, width)
    units = (
        min(width * 8, max(1, size * width * 8 // largest))
        if size > 0 and largest > 0 and width
        else 0
    )
    filled, fraction = divmod(units, 8)
    bar = "█" * filled + ("▏▎▍▌▋▊▉"[fraction - 1] if fraction else "")
    bar += "░" * (width - len(bar))
    if ascii_ui():
        filled = min(width, filled + bool(fraction))
        return "[" + "#" * filled + " " * (width - filled) + "]"
    return bar


def progress_bar(percent: int, width: int) -> str:
    filled = min(width, max(0, width * percent // 100))
    return "[" + ("#" if ascii_ui() else "=") * filled + " " * (width - filled) + "]"


def splash_lines(width: int) -> list[str]:
    if width < 48:
        return []
    logo = (
        (
            " █████╗ ███████╗██████╗ ██╗   ██╗",
            "██╔══██╗██╔════╝██╔══██╗██║   ██║",
            "███████║███████╗██║  ██║██║   ██║",
            "██╔══██║╚════██║██║  ██║██║   ██║",
            "██║  ██║███████║██████╔╝╚██████╔╝",
            "╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝",
        )
        if not ascii_ui()
        else (
            "",
            "     _   ___ ___  _   _",
            "    /_\\ / __|   \\| | | |",
            "   / _ \\__ \\ |) | |_| |",
            "  /_/ \\_\\___/___/ \\___/",
            "",
        )
    )
    logo_width = max(map(display_width, logo))
    left = " " * ((48 - logo_width) // 2)
    return [pad_display(left + line, 48) for line in logo] + [
        " " * 48,
        "agent session disk usage".center(48),
    ]


def splash_position(width: int, height: int, lines: list[str]) -> tuple[int, int]:
    return max(0, (height - len(lines)) // 2), max(
        0, (width - max(map(display_width, lines), default=0)) // 2
    )


def indexing_lines(
    width: int, source: str, current: int, done: int, total: int
) -> list[str]:
    percent = min(100, max(0, done * 100 // total)) if total else 0
    if width < 48:
        return [compact_text(f"indexing {source.lower()} {percent}%", max(0, width))]
    count = f"{current:,} sessions"
    status = f"Indexing {source}" if source else "Discovering sessions"

    def row(text):
        return terminal_art("│ " + pad_display(compact_text(text, 44), 44) + " │")

    return [
        *splash_lines(width),
        " " * 48,
        terminal_art("╭" + "─" * 46 + "╮"),
        row(status),
        row(f"{size_bar(percent, 100, 36 if ascii_ui() else 38)}  {percent:3d}%"),
        row(pad_display(count, max(0, 44 - len(human_size(done)))) + human_size(done)),
        terminal_art("╰" + "─" * 46 + "╯"),
    ]


def compact_path(path: str, width: int) -> str:
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1) :]


def compact_text(text: str, width: int) -> str:
    """Truncate prose from the right; its opening words carry the context."""
    if width <= 0:
        return ""
    text = "".join(c if c.isprintable() else " " for c in text)
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for character in text:
        character_width = display_width(character)
        if used + character_width > max(0, width - 1):
            break
        kept.append(character)
        used += character_width
    return "".join(kept) + "…"


def display_width(text: str) -> int:
    """Approximate terminal cell width without a runtime dependency."""
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def digest(
    session: Session,
    poll: Callable[[], None] | None = None,
    preview: bool = False,
    controls: SessionControls | None = None,
) -> str:
    try:
        return read_digest(session, poll, preview, controls)
    except OSError as error:
        return f"Transcript unavailable: {error.strerror or error}. Press r in the list to rescan."


def read_digest(
    session: Session,
    poll: Callable[[], None] | None = None,
    preview: bool = False,
    controls: SessionControls | None = None,
) -> str:
    controls = session_controls(session) if controls is None else controls
    data = read_brief(session, poll, preview)
    event_counts = data.event_counts

    def excerpt(text: str | None) -> str:
        return f"│ {compact_text(text, 320) if text else 'none found'}"

    events = sum(event_counts.values())
    turns = event_counts["turn_context"]
    messages = sum(event_counts[kind] for kind in ("message", "user", "assistant"))
    compactions = event_counts["compacted"] + event_counts["context_compacted"]
    activity = f"{turns:,} turns" if turns else f"{messages:,} messages"
    initial_label = "Initial objective" if data.first_objective else "First request"
    if data.forked_from:
        initial_label += " (may be inherited)"
    provenance = list(dict.fromkeys(data.recorded_via))
    metadata = [f"Recorded via: {', '.join(provenance)}"] if provenance else []
    if data.providers:
        metadata.append(f"Provider: {' → '.join(data.providers)}")
    if data.latest_model:
        metadata.append(f"Model: {data.latest_model}")
    if data.task_path:
        metadata.append(f"Task: {data.task_path}")
    if data.forked_from:
        metadata.append(f"Forked from: {data.forked_from}")
    if session.parent_id and session.parent_id != data.forked_from:
        # The stored parent link supports tree navigation.  It is not evidence
        # that this was created using a conversation-fork operation.
        metadata.append(f"Parent session: {session.parent_id}")
    if controls.runtime_id:
        metadata.append(
            f"{controls.runtime_kind.title() if controls.runtime_kind else 'Agent'}: "
            f"{controls.runtime_state or 'available'} ({controls.runtime_id})"
        )
    commands = [
        f"{command.label}: {shlex.join(command.argv)}" for command in controls.commands
    ]
    sample = " (preview)" if preview else ""
    state = " archived" if session.archived else ""
    lines = [
        f"{human_size(session.size)}  {session.source} {origin_label(session.origin)}{state}  {session_date(session)}",
        f"Folder: {session.cwd}",
        "Counting activity…"
        if preview
        else f"{activity} across {events:,} events; {compactions:,} compactions.",
        "",
        f"╭ Latest request{sample}",
        excerpt(data.latest_user),
        "│",
        f"├ Last reply{sample}",
        excerpt(data.latest_reply),
        "│",
        f"├ {initial_label}{sample}",
        excerpt(data.first_objective or data.first_user),
        "╰",
        "",
        f"ID: {session.session_id}",
        *metadata,
        f"Tags: {', '.join(session.tags)}",
        *commands,
    ]
    return "\n".join(lines)


def print_summary(sessions: list[Session], mode: str, sort_by: str) -> None:
    groups = ordered_groups(group_sessions(sessions, mode), sort_by)
    width = max((len(name) for name, _ in groups), default=10)
    label = {"cwd": "folder", "origin": "session type"}.get(mode, mode)
    print(
        f"{len(sessions):,} sessions  {human_size(sum(s.size for s in sessions))}  grouped by {label}\n"
    )
    print(f"{'group':<{width}}  sessions       size")
    print(f"{'-' * width}  --------  ---------")
    for name, entries in groups:
        print(
            f"{name:<{width}}  {len(entries):>8,}  {human_size(sum(s.size for s in entries)):>10}"
        )


def print_sessions(sessions: list[Session], sort_by: str) -> None:
    ordered = ordered_sessions(sessions, sort_by)
    for session in ordered:
        print(
            f"{human_size(session.size):>10}  {session.source:<6}  {session_type_label(session):<6}  {session_label(session)[:30]:<30}  {session_date(session):>8}  "
            f"[{', '.join(session.tags)}]\n"
            f"{'':>11}{session.session_id[:18]}  {session.cwd}\n"
        )


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


def color_attr(window, color: int) -> int:
    return (
        curses.color_pair(color)
        if color and getattr(window, "colors_enabled", True)
        else curses.A_NORMAL
    )


def draw_line(
    window: curses.window,
    row: int,
    text: str,
    selected: bool = False,
    color: int = 0,
    bold: bool = False,
    dim: bool = False,
    invert: bool = False,
    pointer: bool = False,
    right_margin: int = 0,
) -> None:
    height, width = window.getmaxyx()
    if row >= height or width < 2:
        return
    if selected and pointer:
        text = "›" + text[1:]
    text = compact_text(terminal_art(text), max(0, width - 1 - right_margin))
    if invert or selected:
        text = pad_display(text, width - 1)
    attr = curses.A_REVERSE | curses.A_BOLD if selected else color_attr(window, color)
    if bold:
        attr |= curses.A_BOLD
    if dim:
        attr |= curses.A_DIM
    if invert:
        attr |= curses.A_REVERSE
    window.addnstr(row, 0, text, max(0, width - 1), attr)
    for match in re.finditer("░+", text):
        window.addnstr(
            row,
            display_width(text[: match.start()]),
            match.group(),
            len(match.group()),
            attr | curses.A_DIM,
        )


def origin_color(origin: str) -> int:
    return {
        "user": 1,
        "primary": 2,
        "subagent": 3,
        "sidechain": 3,
        "review": 4,
        "automation": 5,
        "ide": 6,
    }.get(origin, 0)


def source_color(source: str) -> int:
    return {"codex": 7, "claude": 8, "omp": 10}.get(source, 0)


def session_type_label(session: Session) -> str:
    """Archived is the actionable state; the brief retains the stored type."""
    return "arch" if session.archived else origin_label(session.origin)


def draw_session_line(
    window: curses.window,
    row: int,
    session: Session,
    selected: bool,
    branch: str = "",
    stats: tuple[int, int] | None = None,
) -> None:
    """Keep source identity visible without sacrificing selection contrast."""
    _, width = window.getmaxyx()
    size, descendants = stats if stats is not None else (session.size, 0)
    prefix = f"  {human_size(size):>10}  "
    source = f"{session.source:<6}"
    kind = f"  {session_type_label(session):<6}"
    date = f"  {session_date(session):>8}  "
    branch = terminal_art(branch)
    title_width = max(
        0,
        width
        - 1
        - CONTENT_RIGHT_MARGIN
        - display_width(prefix + source + kind + date + branch),
    )
    count = f" (+{descendants})" if descendants else ""
    title = pad_display(
        compact_text(session_label(session), max(0, title_width - len(count))) + count,
        title_width,
    )
    suffix = kind + date + branch + title
    if selected:
        draw_line(
            window,
            row,
            prefix + source + suffix,
            selected=True,
            pointer=True,
            right_margin=CONTENT_RIGHT_MARGIN,
        )
        column = display_width(prefix + source + kind)
        remaining = max(0, width - 1 - CONTENT_RIGHT_MARGIN - column)
        if remaining:
            muted_date = compact_text(date, remaining)
            window.addnstr(
                row,
                column,
                muted_date,
                len(muted_date),
                curses.A_REVERSE | curses.A_DIM,
            )
        return
    _, width = window.getmaxyx()
    limit = max(0, width - 1 - CONTENT_RIGHT_MARGIN)
    column = 0
    for text, color, style in (
        (prefix, 0, 0),
        (source, source_color(session.source), curses.A_BOLD),
        (kind, origin_color(session.origin), 0),
        (date, 0, curses.A_DIM),
        (branch + title, origin_color(session.origin), 0),
    ):
        if column >= limit:
            break
        attr = color_attr(window, color)
        attr |= style
        text = compact_text(text, limit - column)
        window.addnstr(row, column, text, len(text), attr)
        column += display_width(text)


def action_dialog(window, title, body, options, default, shortcuts):
    """Use identical keys and confirmation defaults at every terminal size."""
    selected = options.index(default)
    while True:
        height, width = window.getmaxyx()
        boxed = width >= 44 and height >= len(body) + len(options) + 5
        box_width = min(72, width - 4) if boxed else max(1, width - 1)
        left = (width - box_width) // 2 if boxed else 0
        top = (height - len(body) - len(options) - 4) // 2 if boxed else 0
        if not boxed:
            window.erase()
        inner = box_width - 2
        lines = [title, *body, "", *options]
        for index, line in enumerate(lines):
            row = top + index + int(boxed)
            if row >= height:
                break
            is_option = index >= len(body) + 2
            active = is_option and index - len(body) - 2 == selected
            text = (
                pad_display(compact_text(" " + line, max(0, inner)), max(0, inner))
                if boxed
                else compact_text(line, box_width)
            )
            if boxed:
                text = terminal_art("│" + text + "│")
            window.addnstr(
                row,
                left,
                text,
                box_width,
                curses.A_REVERSE if active else curses.A_NORMAL,
            )
        if boxed:
            window.addnstr(top, left, terminal_art("╭" + "─" * inner + "╮"), box_width)
            window.addnstr(
                top + len(lines) + 1,
                left,
                terminal_art("╰" + "─" * inner + "╯"),
                box_width,
            )
        window.refresh()
        key = window.getch()
        if key in (27, ord("q"), ord("n"), ord("N")):
            return None
        if key in shortcuts:
            return shortcuts[key]
        if key in (curses.KEY_ENTER, 10, 13):
            choice = options[selected]
            return None if choice in ("cancel", "no") else choice
        if key in (curses.KEY_UP, curses.KEY_LEFT, ord("k"), ord("h")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, curses.KEY_RIGHT, ord("j"), ord("l")):
            selected = min(len(options) - 1, selected + 1)


def confirm_session_action(window: curses.window, session: Session) -> str | None:
    supported = available_actions(session)
    actions = [
        action
        for action in ("archive", "unarchive", "trash", "delete")
        if action in supported
    ]
    if session.source == "codex":
        explanation = (
            "Unarchive restores it; Delete is permanent."
            if session.archived
            else "Archive keeps it on disk; Delete is permanent."
        )
    elif actions:
        explanation = "Archive compresses; Trash is recoverable."
    else:
        explanation = "Read-only source."
    return action_dialog(
        window,
        "Session action",
        [session_label(session), explanation],
        [*actions, "cancel"],
        actions[0] if actions else "cancel",
        {
            ord(key): choice
            for keys, choice in (
                ("aA", "archive"),
                ("uU", "unarchive"),
                ("tT", "trash"),
                ("dD", "delete"),
            )
            if choice in actions
            for key in keys
        },
    )


def confirm_trash(window: curses.window) -> str | None:
    return action_dialog(
        window,
        "Confirm Trash",
        ["Move this session to system Trash?"],
        ["yes", "no", "don't ask again"],
        "no",
        {ord("y"): "yes", ord("Y"): "yes"},
    )


def confirm_permanent_delete(window: curses.window) -> bool:
    return (
        action_dialog(
            window,
            "Confirm delete",
            ["Permanently delete this session through Codex?"],
            ["yes", "no"],
            "no",
            {ord("y"): "yes", ord("Y"): "yes"},
        )
        == "yes"
    )


def brief_sizes(session: Session, entries: Iterable[Session]) -> str:
    total, descendants = subtree_stats(entries).get(row_id(session), (session.size, 0))
    sizes = f"File: {human_size(session.size)}"
    if descendants:
        sizes += f"\nTree: {human_size(total)} including {descendants} descendants in this view"
    return sizes


def session_brief(session: Session, entries: Iterable[Session]) -> str:
    tree = brief_sizes(session, entries).splitlines()[1:]
    return digest(session) + ("\n" + "\n".join(tree) if tree else "")


class BriefCancelled(Exception):
    """Return to the browser without finishing the transcript scan."""


def open_brief(
    window: curses.window, session: Session, entries: Iterable[Session]
) -> None:
    """Display a bounded preview, then full results; only the UI thread draws."""
    sizes = brief_sizes(session, entries)
    known = [sizes, f"ID: {session.session_id}", f"Folder: {session.cwd}"]
    if session.task_path:
        known.append(f"Task: {session.task_path}")
    if session.forked_from:
        known.append(f"Forked from: {session.forked_from}")
    window.erase()
    draw_line(window, 0, session_label(session), bold=True)
    draw_line(window, 1, "Reading conversation…  Backspace return")
    for row, line in enumerate("\n".join(known).splitlines(), 3):
        draw_line(window, row, line, right_margin=CONTENT_RIGHT_MARGIN)
    window.refresh()

    cancelled = threading.Event()
    updates: list[str] = []

    def poll() -> None:
        if cancelled.is_set():
            raise BriefCancelled

    def load() -> None:
        try:
            controls = session_controls(session)
            for preview in (True, False):
                poll()
                body = digest(session, poll, preview, controls)
                tree = sizes.splitlines()[1:]
                updates.append(body + ("\n" + "\n".join(tree) if tree else ""))
        except BriefCancelled:
            pass
        except Exception as error:
            updates.append(
                f"Brief unavailable: {error}. Press r in the list to rescan."
            )

    worker = threading.Thread(target=load, daemon=True)
    worker.start()
    try:
        text_view(
            window,
            session_label(session),
            "\n".join(known),
            update=lambda: updates[-1] if updates else None,
        )
    finally:
        cancelled.set()
        window.nodelay(False)


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


def search_prompt(window: curses.window) -> str:
    height, width = window.getmaxyx()
    label = "Find: "
    draw_line(window, height - 1, label.ljust(width - 1))
    try:
        curses.echo()
        return window.getstr(
            height - 1, len(label), max(1, width - len(label) - 2)
        ).decode("utf-8", "replace")
    finally:
        curses.noecho()


def wrap_cells(raw: str, width: int) -> list[str]:
    prefix = "│ " if raw.startswith("│ ") and width > 2 else ""
    text = raw[len(prefix) :]
    lines = []
    line = prefix
    for word in text.split():
        for character in (" " if line != prefix else "") + word:
            if display_width(line + character) > width:
                lines.append(line)
                line = prefix
            if display_width(line + character) <= width:
                line += character
    return lines + [line]


def draw_brief_line(window, row: int, line: str) -> None:
    heading = line.startswith(("╭ ", "├ "))
    metadata = line.startswith(
        (
            "ID:",
            "Folder:",
            "Task:",
            "Forked from:",
            "Parent session:",
            "Recorded via:",
            "Provider:",
            "Model:",
            "Background:",
            "Agent:",
        )
    )
    header = re.match(r"^\S+ (?:B|KiB|MiB|GiB|TiB)  (\w+) ", line)
    draw_line(window, row, line, bold=heading, dim=metadata or bool(header))
    width = window.getmaxyx()[1]
    if line.startswith(("╭", "├", "│", "╰")) and width > 1:
        border = terminal_art(line[0])
        window.addnstr(row, 0, border, len(border), curses.A_DIM)
    if header:
        source = header.group(1)
        column = display_width(line[: header.start(1)])
        if column + len(source) < width:
            window.addnstr(
                row,
                column,
                source,
                len(source),
                color_attr(window, source_color(source)) | curses.A_BOLD,
            )


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
                wrap_cells(raw, max(1, min(120, width - 1 - CONTENT_RIGHT_MARGIN)))
            )
        offset = min(offset, max(0, len(lines) - max(1, height - 2)))
        frame = (body, offset, height, width)
        if update is None or frame != last_frame:
            window.erase()
            draw_line(
                window,
                0,
                compact_text(title, 120),
                bold=True,
                right_margin=CONTENT_RIGHT_MARGIN,
            )
            for row, line in enumerate(lines[offset : offset + height - 2], 2):
                draw_brief_line(window, row, line)
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
                query = search_prompt(window)
                match_index = offset - 1
            match_index = find_match(
                lines, query, match_index, -1 if key == ord("N") else 1
            )
            offset = min(maximum, max(0, match_index))


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
    browser: BrowserState
    confirm_trash_actions: bool
    source_filter: str = "all"
    tree_mode: bool = False
    collapsed_nodes: set[str] = field(default_factory=set)
    query: str = ""
    status: str = ""
    view_key: tuple | None = None
    view_data: tuple | None = None

    def current_view(self):
        """Cache only the current view; navigation must not resolve paths again."""
        key = (self.source_filter, self.mode, self.browser.cwd_node, self.sort_by)
        if key != self.view_key:
            visible, grouped = browser_visible_sessions(
                self.sessions, self.source_filter, self.mode, self.browser.cwd_node
            )
            items = (
                cwd_listing(visible, self.browser.cwd_node, self.sort_by)
                if self.mode == "cwd"
                else browser_group_items(grouped, self.mode, self.sort_by)
            )
            scoped = (
                [s for s in grouped if in_scope(s, self.browser.cwd_node)]
                if self.mode == "cwd"
                else grouped
            )
            self.view_data = visible, grouped, items, scoped
            self.view_key = key
        return self.view_data


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
        row_id(frame.display_entries[browser.selected][0])
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
        browser.cwd_node,
        tree_with_ancestors(entries, current),
    )
    rows = tree_rows(
        entries,
        current,
        state.sort_by,
        state.tree_mode,
        state.collapsed_nodes,
    )
    browser.selected = next(
        (i for i, (entry, _) in enumerate(rows) if row_id(entry) == anchor),
        browser.selected,
    )
    browser.selected, browser.offset = clamp_view(
        browser.selected, browser.offset, len(rows), frame.page_size
    )


def render_tui_frame(window: curses.window, state: TuiState) -> TuiFrame:
    """Render one browser frame and return only the data needed by key handling."""
    window.erase()
    height, width = window.getmaxyx()
    page_size = max(1, height - 4)
    visible, grouped_visible, base_items, scoped = state.current_view()
    browser = state.browser
    if browser.detail is not None and not browser.detail[1]:
        browser.leave_detail()
        return render_tui_frame(window, state)

    branches: dict[str, str] = {}
    display_entries: list[tuple[Session, str]] = []
    entry_children: dict[str, list[str]] = defaultdict(list)
    tree_parents: dict[int, int | None] = {}
    tree_children: dict[int | None, list[int]] = defaultdict(list)

    if browser.detail is None:
        items = base_items
        if state.mode == "cwd":
            direct = [entries[0] for kind, _, entries in items if kind == "session"]
            tree_entries = tree_with_ancestors(direct, visible)
            state.tree_mode, state.collapsed_nodes = browser.open_group(
                state.mode,
                str(browser.cwd_node),
                state.source_filter,
                browser.cwd_node,
                tree_entries,
            )
            folder_rows = [item for item in items if item[0] != "session"]
            session_rows = tree_rows(
                direct,
                visible,
                state.sort_by,
                state.tree_mode,
                state.collapsed_nodes,
            )
            branches = {row_id(entry): branch for entry, branch in session_rows}
            items = folder_rows + [
                ("session", entry.title, [entry]) for entry, _ in session_rows
            ]
            if browser.pending_anchor is not None and state.tree_mode:
                links = parent_links(tree_entries)
                shown = {item_key(item) for item in items}
                while (
                    browser.pending_anchor not in shown
                    and browser.pending_anchor in links
                ):
                    browser.pending_anchor = links[browser.pending_anchor]
        else:
            state.tree_mode = False
            tree_entries = []
        if browser.pending_anchor is not None:
            browser.selected = next(
                (
                    i
                    for i, item in enumerate(items)
                    if item_key(item) == browser.pending_anchor
                ),
                browser.selected,
            )
            browser.pending_anchor = None
        browser.selected, browser.offset = clamp_view(
            browser.selected, browser.offset, len(items), page_size
        )
        location = (
            relative_folder(browser.cwd_node, state.cwd_root)
            if state.mode == "cwd"
            else {
                "tag": "tags",
                "source": "sources",
                "origin": "session types",
            }.get(state.mode, state.mode)
        )
        stats = (
            subtree_stats(tree_entries)
            if state.mode == "cwd" and state.tree_mode
            else {}
        )
        gap = next(
            (
                i
                for i in range(1, len(items))
                if items[i][0] == "session" and items[i - 1][0] != "session"
            ),
            0,
        )
        if (
            browser.selected
            - browser.offset
            + int(browser.offset < gap <= browser.selected)
            >= page_size
        ):
            browser.offset += 1
        largest_group = max(
            (sum(item.size for item in entries) for _, _, entries in items),
            default=0,
        )
        for index, (kind, name, entries) in enumerate(
            items[browser.offset : browser.offset + page_size], browser.offset
        ):
            row = index - browser.offset + 2 + int(browser.offset < gap <= index)
            if row >= height - 2:
                break
            if kind == "session":
                draw_session_line(
                    window,
                    row,
                    entries[0],
                    index == browser.selected,
                    branches.get(row_id(entries[0]), "") if state.mode == "cwd" else "",
                    stats.get(row_id(entries[0])),
                )
                continue
            display_name = (
                "/" + name if kind == "folder" else group_label(name, state.mode)
            )
            group_size = sum(item.size for item in entries)
            text = (
                f"{human_size(group_size):>10}  "
                f"{size_bar(group_size, largest_group)}  "
                f"{len(entries):>5}  {display_name}"
            )
            color = origin_color(name) if state.mode == "origin" else 0
            draw_line(
                window,
                row,
                "  " + text,
                index == browser.selected,
                color,
                pointer=True,
                right_margin=CONTENT_RIGHT_MARGIN,
            )
        total = len(items)
    else:
        name, entries = browser.detail
        items = base_items
        tree_entries = (
            tree_with_ancestors(entries, grouped_visible)
            if state.tree_mode
            else entries
        )
        display_entries = tree_rows(
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
                    if row_id(entry) == browser.pending_anchor
                ),
                browser.selected,
            )
            browser.pending_anchor = None
        display_index = {
            row_id(entry): index for index, (entry, _) in enumerate(display_entries)
        }
        links = parent_links(tree_entries)
        entry_ids = set(display_index)
        for entry in tree_entries:
            parent_id = links.get(row_id(entry))
            if parent_id in entry_ids:
                entry_children[parent_id].append(row_id(entry))
        for index, (entry, _) in enumerate(display_entries):
            parent = (
                display_index.get(links.get(row_id(entry))) if state.tree_mode else None
            )
            tree_parents[index] = parent
            tree_children[parent].append(index)
        browser.selected, browser.offset = clamp_view(
            browser.selected, browser.offset, len(display_entries), page_size
        )
        location = group_label(name, state.mode)
        stats = subtree_stats(tree_entries) if state.tree_mode else {}
        for index, (session, branch) in enumerate(
            display_entries[browser.offset : browser.offset + page_size],
            browser.offset,
        ):
            draw_session_line(
                window,
                index - browser.offset + 2,
                session,
                index == browser.selected,
                branch,
                stats.get(row_id(session)),
            )
        total = len(display_entries)

    if browser.detail is None and state.mode == "cwd":
        display_index = {
            item_key(item): i for i, item in enumerate(items) if item[0] == "session"
        }
        links = parent_links(tree_entries) if state.tree_mode else {}
        for entry in tree_entries:
            parent_id = links.get(row_id(entry))
            if parent_id in display_index:
                entry_children[parent_id].append(row_id(entry))
        tree_parents = {
            i: display_index.get(links.get(item_key(item)))
            for i, item in enumerate(items)
        }
        for index, parent in tree_parents.items():
            tree_children[parent].append(index)

    footer_sessions = browser.detail[1] if browser.detail is not None else scoped
    footer = (
        f"Transcripts: {human_size(sum(session.size for session in footer_sessions))}  "
        f"{len(footer_sessions):,} sessions"
    )
    label = f" asdu  {location}"
    if state.source_filter != "all":
        label += f"  [{state.source_filter}]"
    ordering = ("tree " if state.tree_mode else "") + sort_label(state.sort_by) + " "
    available = max(0, width - 1 - display_width(ordering))
    draw_line(
        window,
        0,
        pad_display(compact_text(label, available), available) + ordering,
        bold=True,
        invert=True,
    )
    selected_session = browser.detail is not None or (
        bool(items) and items[browser.selected][0] == "session"
    )
    has_sessions = browser.detail is not None or any(
        kind == "session" for kind, _, _ in items
    )
    commands = ["Enter open"]
    if browser.detail is not None or (
        state.mode == "cwd" and browser.cwd_node != state.cwd_root
    ):
        commands.append("Backspace back")
    if browser.detail is None:
        commands.append("g group")
    commands.extend(["f filter", "s sort", "Ctrl-F find"])
    if has_sessions:
        commands.append("t tree")
    if selected_session:
        commands.append("a action")
    commands.append("? help")
    draw_line(window, height - 2, " " + (state.status or footer))
    draw_line(window, height - 1, " " + "  ".join(commands), invert=True)
    window.refresh()

    selected_entry = (
        display_entries[browser.selected][0]
        if browser.detail is not None and display_entries
        else items[browser.selected][2][0]
        if browser.detail is None and items and items[browser.selected][0] == "session"
        else None
    )
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
    if browser.detail is None and state.mode == "cwd" and selected_entry is None:
        if key in (curses.KEY_RIGHT, ord("l")):
            key = 10
        elif key in (curses.KEY_LEFT, ord("h")):
            key = 127

    if key in (*SEARCH_KEYS, ord("n"), ord("N")):
        if key in SEARCH_KEYS:
            state.query = search_prompt(window)
        labels = (
            [session_label(entry) for entry, _ in frame.display_entries]
            if browser.detail
            else [
                session_label(entries[0])
                if kind == "session"
                else group_label(name, state.mode)
                for kind, name, entries in items
            ]
        )
        found = find_match(
            labels,
            state.query,
            browser.selected,
            -1 if key == ord("N") else 1,
        )
        state.status = (
            f"Find: {state.query}"
            if any(state.query.casefold() in label.casefold() for label in labels)
            else f"No match: {state.query}"
        )
        browser.selected = found
        return True
    if key in (curses.KEY_HOME, curses.KEY_END):
        browser.selected = 0 if key == curses.KEY_HOME else max(0, frame.total - 1)
        return True
    if key in (ord("q"), 27):
        return False
    if key == ord("?"):
        text_view(
            window,
            "Help",
            "Sizes: saved conversation bytes, not project files. Updated: file modification time.\n"
            "Tag groups: primary tag only; briefs list every matching tag.\n\n"
            "Enter: open folder or session brief\n"
            "Ctrl-F or /: find text; n/N: next/previous match\n"
            "Home/End: first/last\n"
            "Backspace: parent folder or previous list\n"
            "r: rescan local session roots\n"
            "f: cycle source filter\n"
            "s: cycle sort\n"
            "g: cycle folder, tag, source, or session-type groups\n"
            "t: toggle session tree (↑↓ siblings, ←→ parent/child, Space fold, z all)\n"
            "a: source-supported session actions\n"
            "i: open a session brief\n"
            "q: quit",
        )
        return True
    if key == ord("r"):
        if browser.detail is None and items:
            browser.pending_anchor = item_key(items[browser.selected])

        def show_rescan(
            source: str,
            current: int,
            total: int,
            done_bytes: int,
            total_bytes: int,
        ) -> None:
            window.erase()
            progress_height, progress_width = window.getmaxyx()
            lines = indexing_lines(
                min(progress_width - 1, 47)
                if progress_height < 15
                else progress_width - 1,
                source,
                current,
                done_bytes,
                total_bytes,
            )
            top, left = splash_position(progress_width - 1, progress_height, lines)
            for row, line in enumerate(lines):
                draw_line(
                    window,
                    top + row,
                    " " * left + line,
                    bold=row < 6 and len(lines) > 1,
                )
            window.refresh()

        window.erase()
        draw_line(
            window,
            0,
            " asdu  |  rescanning local sessions ",
            bold=True,
            invert=True,
        )
        draw_line(window, 2, "  Collecting local transcripts…")
        window.refresh()
        try:
            state.sessions[:] = rescan(show_rescan)
        except OSError as error:
            state.status = f"Rescan failed: {error}"
        else:
            state.view_key = None
            refresh_tui_view(state, frame)
        return True

    tree_keys = (
        curses.KEY_DOWN,
        ord("j"),
        curses.KEY_UP,
        ord("k"),
        curses.KEY_LEFT,
        ord("h"),
        curses.KEY_RIGHT,
        ord("l"),
    )
    if selected_entry is not None and state.tree_mode and key in tree_keys:
        parent = frame.tree_parents.get(browser.selected)
        siblings = frame.tree_children.get(parent, [])
        sibling_position = (
            siblings.index(browser.selected) if browser.selected in siblings else 0
        )
        if key in (curses.KEY_DOWN, ord("j")) and sibling_position + 1 < len(siblings):
            browser.selected = siblings[sibling_position + 1]
        elif key in (curses.KEY_UP, ord("k")) and sibling_position > 0:
            browser.selected = siblings[sibling_position - 1]
        elif key in (curses.KEY_RIGHT, ord("l")):
            if row_id(selected_entry) in state.collapsed_nodes:
                state.collapsed_nodes.remove(row_id(selected_entry))
            elif frame.tree_children.get(browser.selected):
                browser.selected = frame.tree_children[browser.selected][0]
        elif key in (curses.KEY_LEFT, ord("h")):
            if (
                frame.entry_children.get(row_id(selected_entry))
                and row_id(selected_entry) not in state.collapsed_nodes
            ):
                state.collapsed_nodes.add(row_id(selected_entry))
            elif parent is not None:
                browser.selected = parent
    elif selected_entry is not None and state.tree_mode and key == ord(" "):
        identifier = row_id(selected_entry)
        if frame.entry_children.get(identifier):
            if identifier in state.collapsed_nodes:
                state.collapsed_nodes.remove(identifier)
            else:
                state.collapsed_nodes.add(identifier)
    elif state.tree_mode and key == ord("z"):
        if browser.detail is None and items:
            browser.pending_anchor = item_key(items[browser.selected])
        nodes_with_children = set(frame.entry_children)
        if nodes_with_children.issubset(state.collapsed_nodes):
            state.collapsed_nodes.clear()
        else:
            state.collapsed_nodes.update(nodes_with_children)
    elif key in (curses.KEY_DOWN, ord("j")):
        if browser.selected < frame.total - 1:
            browser.selected += 1
    elif key in (curses.KEY_UP, ord("k")):
        if browser.selected > 0:
            browser.selected -= 1
    elif key == curses.KEY_NPAGE:
        if browser.selected < frame.total - 1:
            browser.selected = min(frame.total - 1, browser.selected + frame.page_size)
    elif key == curses.KEY_PPAGE:
        if browser.selected > 0:
            browser.selected = max(0, browser.selected - frame.page_size)
    elif browser.detail is None and key == ord("g"):
        state.mode = cycle_value(state.mode, ["cwd", "tag", "source", "origin"])
        browser.selected, browser.offset = 0, 0
    elif browser.detail is None and state.mode == "cwd" and key == ord("t"):
        browser.pending_anchor = item_key(items[browser.selected]) if items else None
        state.tree_mode, state.collapsed_nodes = browser.toggle_tree(frame.tree_entries)
    elif browser.detail is not None and key == ord("t"):
        contextual_tree = tree_with_ancestors(browser.detail[1], frame.grouped_visible)
        state.tree_mode, state.collapsed_nodes = browser.toggle_tree(contextual_tree)
        browser.selected, browser.offset = 0, 0
    elif key == ord("a") and (
        browser.detail is not None
        or (items and items[browser.selected][0] == "session")
    ):
        entry = (
            frame.display_entries[browser.selected][0]
            if browser.detail is not None
            else items[browser.selected][2][0]
        )
        choice = confirm_session_action(window, entry)
        if choice == "trash" and state.confirm_trash_actions:
            confirmation = confirm_trash(window)
            if confirmation is None:
                choice = None
            elif confirmation == "don't ask again":
                disable_trash_confirmation()
                state.confirm_trash_actions = False
        elif choice == "delete" and not confirm_permanent_delete(window):
            choice = None
        if choice is not None:
            try:
                outcome = perform_session_action(entry, choice)
                result = {
                    "archive": "Archived",
                    "unarchive": "Unarchived",
                    "trash": "Trashed",
                    "delete": "Deleted",
                }[choice]
                try:
                    record_action(choice, entry, outcome.destination)
                except OSError as error:
                    state.status = f"{result}; action log unavailable: {error}"
                else:
                    state.status = result
            except OSError as error:
                state.status = str(error)
            else:
                replacement = outcome.replacement
                position = state.sessions.index(entry)
                if replacement is None:
                    state.sessions.pop(position)
                else:
                    state.sessions[position] = replacement
                state.view_key = None
                if browser.detail is not None and entry in browser.detail[1]:
                    position = browser.detail[1].index(entry)
                    if replacement is None:
                        browser.detail[1].pop(position)
                    else:
                        browser.detail[1][position] = replacement
    elif key == ord("f"):
        available = ["all", *sorted({session.source for session in state.sessions})]
        if browser.detail is None and items:
            browser.pending_anchor = item_key(items[browser.selected])
        state.source_filter = cycle_value(state.source_filter, available)
        refresh_tui_view(state, frame)
    elif browser.detail is not None and key == ord("s"):
        browser.pending_anchor = (
            row_id(frame.display_entries[browser.selected][0])
            if frame.display_entries
            else None
        )
        state.sort_by = cycle_value(state.sort_by, ["size", "date", "name"])
    elif browser.detail is None and key == ord("s"):
        browser.pending_anchor = item_key(items[browser.selected]) if items else None
        state.sort_by = cycle_value(
            state.sort_by, ["size", "date", "count", "name"]
        )
    elif browser.detail is None and key in (curses.KEY_ENTER, 10, 13):
        if not items:
            return True
        kind, name, entries = items[browser.selected]
        if state.mode == "cwd":
            if kind == "folder":
                browser.visit_folder(
                    browser.cwd_node / name, item_key(items[browser.selected])
                )
            else:
                open_brief(
                    window,
                    entries[0],
                    frame.tree_entries if state.tree_mode else frame.visible,
                )
            return True
        browser.detail = (name, entries)
        browser.detail_return = (item_key(items[browser.selected]), browser.offset)
        tree_entries = tree_with_ancestors(entries, frame.grouped_visible)
        state.tree_mode, state.collapsed_nodes = browser.open_group(
            state.mode,
            name,
            state.source_filter,
            browser.cwd_node,
            tree_entries,
        )
        browser.selected, browser.offset = 0, 0
    elif (
        browser.detail is None
        and state.mode == "cwd"
        and key in (curses.KEY_BACKSPACE, 127, 8)
    ):
        if browser.cwd_node != state.cwd_root:
            browser.visit_folder(
                browser.cwd_node.parent,
                item_key(items[browser.selected]) if items else None,
            )
    elif browser.detail is not None and key in (curses.KEY_BACKSPACE, 127, 8):
        browser.leave_detail()
    elif browser.detail is not None and key in (
        ord("i"),
        curses.KEY_ENTER,
        10,
        13,
    ):
        entry = frame.display_entries[browser.selected][0]
        open_brief(
            window,
            entry,
            frame.tree_entries if state.tree_mode else frame.visible,
        )
    return True


def tui(
    sessions: list[Session],
    initial_mode: str,
    initial_sort: str,
    scope: Path | None,
    ask_before_trash: bool,
    rescan: Callable[[Callable[[str, int, int, int, int], None]], list[Session]],
    no_color: bool = False,
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
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_MAGENTA, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            curses.init_pair(5, curses.COLOR_RED, -1)
            curses.init_pair(6, curses.COLOR_BLUE, -1)
            if curses.COLORS >= 256:
                curses.init_pair(7, 33, -1)
                curses.init_pair(8, 208, -1)
                curses.init_pair(10, 37, -1)
            else:
                curses.init_pair(7, curses.COLOR_CYAN, -1)
                curses.init_pair(8, curses.COLOR_YELLOW, -1)
                curses.init_pair(10, curses.COLOR_GREEN, -1)

        cwd_root = scope or Path("/")
        state = TuiState(
            sessions,
            initial_mode,
            initial_sort,
            cwd_root,
            BrowserState.create(cwd_root),
            ask_before_trash,
        )
        while True:
            frame = render_tui_frame(window, state)
            siblings = (
                frame.tree_children.get(
                    frame.tree_parents.get(state.browser.selected), []
                )
                if state.tree_mode
                else []
            )
            first = state.browser.selected == (siblings[0] if siblings else 0)
            last = state.browser.selected == (
                siblings[-1] if siblings else max(0, frame.total - 1)
            )
            key = read_navigation(window, first, last)
            state.browser.selected, key = drain_navigation(
                window,
                key,
                state.browser.selected,
                siblings or range(frame.total),
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


def clear_terminal() -> None:
    stream = sys.stderr if sys.stderr.isatty() else sys.stdout
    if stream.isatty():
        stream.write("\033[0m\033[?25h\033[2J\033[H")
        stream.flush()


def default_codex_root() -> Path:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return home.expanduser() / "sessions"


def default_claude_root() -> Path:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return home.expanduser() / "projects"


def main() -> int:
    try:
        return run_main()
    except curses.error as error:
        print(f"asdu: terminal error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # curses.wrapper has already restored terminal modes at this point.
        clear_terminal()
        return 130


def run_main() -> int:
    global ASCII_UI
    parser = argparse.ArgumentParser(
        description="ncdu-style browser for local agent session storage."
    )
    parser.add_argument("--version", action="version", version="asdu 0.3.0")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("browse", "summary", "sessions", "digest"),
        default="browse",
    )
    parser.add_argument(
        "--codex-root",
        type=Path,
        default=default_codex_root(),
        help="Codex session storage directory",
    )
    parser.add_argument(
        "--claude-root",
        type=Path,
        default=default_claude_root(),
        help="Claude session storage directory",
    )
    parser.add_argument(
        "--omp-root",
        type=Path,
        default=Path.home() / ".omp" / "agent" / "sessions",
        help="OMP session storage directory (read-only)",
    )
    parser.add_argument(
        "--source",
        choices=("codex", "claude", "omp"),
        action="append",
        help="Repeat to select sources; default is all available",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show all selected-source sessions, not only the current directory",
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="Scope to this project directory instead of the current directory",
    )
    parser.add_argument(
        "--config", type=Path, help="TOML tag rules; replaces the built-in rules"
    )
    parser.add_argument(
        "--group",
        choices=("tag", "folder", "source", "type"),
        default="tag",
        help="Group by folder, tag, source, or session type",
    )
    parser.add_argument(
        "--sort", choices=("size", "updated", "count", "name"), default="size"
    )
    parser.add_argument("--tag", help="With 'sessions', show only this primary tag")
    parser.add_argument(
        "--session", help="With 'digest', an exact session ID or unique ID prefix"
    )
    parser.add_argument(
        "--content-keywords",
        "--context-keywords",
        dest="content_keywords",
        action="store_true",
        help="Search recognized user-message text for keyword rules (slower)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress the startup splash and scan progress",
    )
    parser.add_argument(
        "--no-content-cache",
        action="store_true",
        help="Rescan every transcript instead of using the keyword cache",
    )
    parser.add_argument(
        "--confirm-trash",
        action="store_true",
        help="Ask before Claude Trash even when confirmation was disabled",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="Use plain ASCII boxes, tree markers, and ncdu-style size bars",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colors; retain bold, dim, and selection (also respects NO_COLOR)",
    )
    args = parser.parse_args()
    ASCII_UI = args.ascii
    args.group = {"folder": "cwd", "type": "origin"}.get(args.group, args.group)
    args.sort = {"updated": "date"}.get(args.sort, args.sort)

    if args.command == "sessions" and args.sort == "count":
        parser.error("--sort count applies to groups, not individual sessions")

    try:
        rules = load_tag_rules(args.config)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        parser.error(str(error))
    adapters = source_adapters(args.codex_root, args.claude_root, args.omp_root)
    sources = tuple(dict.fromkeys(args.source or adapters))
    source_roots = {source: adapter.root for source, adapter in adapters.items()}
    if args.source:
        for source in sources:
            if not source_roots[source].exists():
                parser.error(
                    f"{source} session root does not exist: {source_roots[source]}"
                )
    elif not any(root.exists() for root in source_roots.values()):
        parser.error(
            "no supported session roots found; pass --codex-root, --claude-root, or --omp-root"
        )
    scope = None if args.all else (args.project or Path.cwd()).resolve()

    def scan_current(
        show_progress: bool,
        render: Callable[[str, int, int, int, int], None] | None = None,
    ) -> list[Session]:
        progress = ScanProgress(
            show_progress
            and (
                render is not None
                or (
                    not args.no_progress
                    and (
                        args.content_keywords
                        or (args.command == "browse" and sys.stderr.isatty())
                    )
                )
            ),
            render,
        )
        cache = ContentCache(args.content_keywords and not args.no_content_cache)
        found = scan(
            sources, adapters, rules, args.content_keywords, progress, cache, scope
        )
        progress.finish()
        found = [session for session in found if in_scope(session, scope)]
        return found

    sessions = scan_current(True)

    if args.command == "summary":
        print_summary(sessions, args.group, args.sort)
    elif args.command == "sessions":
        if args.tag:
            sessions = [
                session for session in sessions if primary_tag(session) == args.tag
            ]
        print_sessions(sessions, args.sort)
    elif args.command == "digest":
        if not args.session:
            parser.error("digest requires --session SESSION_ID")
        matches = [
            session
            for session in sessions
            if session.session_id.startswith(args.session)
        ]
        if not matches:
            parser.error(f"no session begins with: {args.session}")
        if len(matches) > 1:
            parser.error(
                f"session prefix is ambiguous ({len(matches)} matches); provide more characters"
            )
        print(session_brief(matches[0], sessions))
    else:
        tui(
            sessions,
            args.group,
            args.sort,
            scope,
            args.confirm_trash or not skip_trash_confirmation(),
            lambda render: scan_current(True, render),
            no_color=args.no_color,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
