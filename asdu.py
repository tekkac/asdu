#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["send2trash>=1.8.3"]
# ///
"""Disk-oriented browser for local agent-session transcripts.

Scanning and browsing are read-only.  Archive and Trash are deliberate,
interactive actions for individual transcripts.
"""

from __future__ import annotations

import argparse
import curses
import os
import re
import sys
import threading
import time
import tomllib
import unicodedata
from collections import defaultdict
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
from asdu_sessions import (
    ContentCache,
    Session,
    archive_session,
    disable_delete_confirmation,
    in_scope,
    load_tag_rules,
    move_to_trash,
    read_brief,
    record_action,
    require_unchanged,
    resume_command,
    scan,
    session_label,
    skip_delete_confirmation,
    source_adapters,
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

    def notice(self) -> str:
        parts = []
        if self.skipped:
            parts.append(f"{len(self.skipped)} files skipped")
        if damaged := self.invalid - self.skipped:
            parts.append(f"{len(damaged)} files contain invalid records")
        return "; ".join(parts)


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
    session: Session, poll: Callable[[], None] | None = None, preview: bool = False
) -> str:
    try:
        return read_digest(session, poll, preview)
    except OSError as error:
        return f"Transcript unavailable: {error.strerror or error}. Press r in the list to rescan."


def read_digest(
    session: Session, poll: Callable[[], None] | None = None, preview: bool = False
) -> str:
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
    if data.task_path:
        metadata.append(f"Task: {data.task_path}")
    if data.forked_from:
        metadata.append(f"Forked from: {data.forked_from}")
    if session.parent_id and session.parent_id != data.forked_from:
        # The stored parent link supports tree navigation.  It is not evidence
        # that this was created using a conversation-fork operation.
        metadata.append(f"Parent session: {session.parent_id}")
    resume = resume_command(session)
    sample = " (preview)" if preview else ""
    lines = [
        f"{human_size(session.size)}  {session.source} {origin_label(session.origin)}  {session_date(session)}",
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
        *([f"Resume: {resume}"] if resume else []),
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
            f"{human_size(session.size):>10}  {session.source:<6}  {origin_label(session.origin):<6}  {session_label(session)[:30]:<30}  {session_date(session):>8}  "
            f"[{', '.join(session.tags)}]\n"
            f"{'':>11}{session.session_id[:18]}  {session.cwd}\n"
        )


WHEEL_UP = curses.KEY_MAX + 1
WHEEL_DOWN = curses.KEY_MAX + 2
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
    attr = (
        getattr(window, "selection_attr", curses.A_REVERSE) | curses.A_BOLD
        if selected
        else color_attr(window, color)
    )
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
    return {"codex": 7, "claude": 8}.get(source, 0)


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
    kind = f"  {origin_label(session.origin):<6}"
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
                getattr(window, "selection_attr", curses.A_REVERSE) | curses.A_DIM,
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
    return action_dialog(
        window,
        "Session action",
        [session_label(session), "Archive compresses; Trash is recoverable."],
        ["archive", "trash", "cancel"],
        "archive",
        {
            ord(key): choice
            for keys, choice in (("aA", "archive"), ("tT", "trash"))
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
    if command := resume_command(session):
        known.append(f"Resume: {command}")
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
            for preview in (True, False):
                poll()
                body = digest(session, poll, preview)
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
    draw_line(window, height - 1, "/".ljust(width - 1))
    try:
        curses.echo()
        return window.getstr(height - 1, 1, max(1, width - 3)).decode(
            "utf-8", "replace"
        )
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
        ("ID:", "Folder:", "Task:", "Forked from:", "Parent session:", "Recorded via:")
    )
    header = re.match(r"^\S+ (?:B|KiB|MiB|GiB|TiB)  (codex|claude) ", line)
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
        elif key in (ord("/"), ord("n"), ord("N")):
            if key == ord("/"):
                query = search_prompt(window)
                match_index = offset - 1
            match_index = find_match(
                lines, query, match_index, -1 if key == ord("N") else 1
            )
            offset = min(maximum, max(0, match_index))


def choose(
    window: curses.window, title: str, options: list[str], current: str
) -> str | None:
    """A small htop-like keyboard chooser for a field with few values."""
    selected = options.index(current) if current in options else 0
    labels = {"cwd": "folder", "origin": "session type", "date": "updated"}
    while True:
        window.erase()
        height, width = window.getmaxyx()
        boxed = width >= 38 and height >= len(options) + 5
        inner = min(48, width - 3)
        if boxed:
            label = " " + compact_text(title, inner - 4) + " "
            draw_line(
                window,
                0,
                terminal_art(
                    "╭─" + label + "─" * (inner - 1 - display_width(label)) + "╮"
                ),
                bold=True,
            )
            draw_line(
                window,
                1,
                terminal_art(
                    "│" + " ↑↓ choose  Enter apply  Esc cancel".ljust(inner) + "│"
                ),
            )
            draw_line(window, 2, terminal_art("│" + " " * inner + "│"))
        else:
            draw_line(window, 0, title, bold=True, invert=True)
            draw_line(window, 1, "↑↓ choose  Enter apply  Esc cancel")
        for row, option in enumerate(options, 3):
            label = labels.get(option, option)
            if boxed:
                text = pad_display(compact_text(" " + label, inner), inner)
                draw_line(window, row, terminal_art("│" + text + "│"))
                if row - 3 == selected:
                    window.addnstr(row, 1, text, len(text), curses.A_REVERSE)
            else:
                draw_line(window, row, label, selected=row - 3 == selected)
        if boxed:
            draw_line(window, len(options) + 3, terminal_art("╰" + "─" * inner + "╯"))
        window.refresh()
        key = window.getch()
        if key in (27, ord("q")):
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            return options[selected]
        if key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(options) - 1, selected + 1)
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)


def tui(
    sessions: list[Session],
    initial_mode: str,
    initial_sort: str,
    scope: Path | None,
    ask_before_delete: bool,
    rescan: Callable[[Callable[[str, int, int, int, int], None]], list[Session]],
    scan_notice: Callable[[], str] = lambda: "",
    no_color: bool = False,
) -> None:
    """A small ncdu-like drill-down UI with explicit archive/Trash actions."""

    def run(window: curses.window) -> None:
        window = TerminalWindow(window)
        curses.curs_set(0)
        # New curses decodes wheel reports itself; old builds without BUTTON5
        # need the raw SGR parser. Both preserve wheel/keyboard provenance.
        try:
            down_button = getattr(curses, "BUTTON5_PRESSED", 0)
            curses.mousemask(curses.BUTTON4_PRESSED | down_button if down_button else 0)
            curses.mouseinterval(0)
        except curses.error:
            pass
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[?1000h\x1b[?1006h")
            sys.stdout.flush()
        window.colors_enabled = (
            not (no_color or os.environ.get("NO_COLOR")) and curses.has_colors()
        )
        if window.colors_enabled:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)  # explicit user
            curses.init_pair(2, curses.COLOR_YELLOW, -1)  # primary
            curses.init_pair(3, curses.COLOR_MAGENTA, -1)  # subagent
            curses.init_pair(4, curses.COLOR_CYAN, -1)  # review
            curses.init_pair(5, curses.COLOR_RED, -1)  # automation
            curses.init_pair(6, curses.COLOR_BLUE, -1)  # IDE-created
            if curses.COLORS >= 256:
                curses.init_pair(7, 33, -1)  # Codex: bright blue
                curses.init_pair(8, 208, -1)  # Claude: orange
                curses.init_pair(9, 255, 237)  # Selection: white on charcoal
            else:
                curses.init_pair(7, curses.COLOR_CYAN, -1)
                curses.init_pair(8, curses.COLOR_YELLOW, -1)
                curses.init_pair(9, curses.COLOR_WHITE, curses.COLOR_BLACK)
            window.selection_attr = curses.color_pair(9)
        mode, sort_by, source_filter, tree_mode = (
            initial_mode,
            initial_sort,
            "all",
            False,
        )
        collapsed_nodes: set[str] = set()
        cwd_root = scope or Path("/")
        browser = BrowserState.create(cwd_root)
        confirm_deletes = ask_before_delete
        query = ""
        status = ""

        def refresh_view() -> None:
            nonlocal tree_mode, collapsed_nodes
            _, current = browser_visible_sessions(
                sessions, source_filter, mode, browser.cwd_node
            )
            if browser.detail is None:
                return
            anchor = (
                row_id(display_entries[browser.selected][0])
                if display_entries
                else None
            )
            name = browser.detail[0]
            entries = (
                [s for s in current if Path(s.cwd).resolve() == browser.cwd_node]
                if mode == "cwd"
                else next(
                    (
                        es
                        for _, key, es in browser_group_items(current, mode, sort_by)
                        if key == name
                    ),
                    [],
                )
            )
            if not entries:
                browser.leave_detail()
                return
            browser.detail = (name, entries)
            tree_mode, collapsed_nodes = browser.open_group(
                mode,
                name,
                source_filter,
                browser.cwd_node,
                tree_with_ancestors(entries, current),
            )
            rows = tree_rows(entries, current, sort_by, tree_mode, collapsed_nodes)
            browser.selected = next(
                (i for i, (entry, _) in enumerate(rows) if row_id(entry) == anchor),
                browser.selected,
            )
            browser.selected, browser.offset = clamp_view(
                browser.selected, browser.offset, len(rows), page_size
            )

        while True:
            window.erase()
            height, width = window.getmaxyx()
            page_size = max(1, height - 4)
            visible, grouped_visible = browser_visible_sessions(
                sessions, source_filter, mode, browser.cwd_node
            )
            if browser.detail is not None and not browser.detail[1]:
                browser.leave_detail()
                continue
            if browser.detail is None:
                if mode == "cwd":
                    items = cwd_listing(visible, browser.cwd_node, sort_by)
                    direct = [es[0] for kind, _, es in items if kind == "session"]
                    tree_entries = tree_with_ancestors(direct, visible)
                    tree_mode, collapsed_nodes = browser.open_group(
                        mode,
                        str(browser.cwd_node),
                        source_filter,
                        browser.cwd_node,
                        tree_entries,
                    )
                    folder_rows = [item for item in items if item[0] != "session"]
                    session_rows = tree_rows(
                        direct, visible, sort_by, tree_mode, collapsed_nodes
                    )
                    branches = {row_id(entry): branch for entry, branch in session_rows}
                    items = folder_rows + [
                        ("session", entry.title, [entry]) for entry, _ in session_rows
                    ]
                    if browser.pending_anchor is not None and tree_mode:
                        links = parent_links(tree_entries)
                        shown = {item_key(item) for item in items}
                        while (
                            browser.pending_anchor not in shown
                            and browser.pending_anchor in links
                        ):
                            browser.pending_anchor = links[browser.pending_anchor]
                else:
                    tree_mode = False
                    items = browser_group_items(grouped_visible, mode, sort_by)
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
                if mode == "cwd":
                    location = relative_folder(browser.cwd_node, cwd_root)
                else:
                    location = {
                        "tag": "tags",
                        "source": "sources",
                        "origin": "session types",
                    }.get(mode, mode)
                stats = (
                    subtree_stats(tree_entries) if mode == "cwd" and tree_mode else {}
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
                    row = (
                        index - browser.offset + 2 + int(browser.offset < gap <= index)
                    )
                    if row >= height - 2:
                        break
                    display_name = (
                        "/" + name if kind == "folder" else group_label(name, mode)
                    )
                    group_size = sum(item.size for item in entries)
                    if kind == "session":
                        draw_session_line(
                            window,
                            row,
                            entries[0],
                            index == browser.selected,
                            branches.get(row_id(entries[0]), "")
                            if mode == "cwd"
                            else "",
                            stats.get(row_id(entries[0])),
                        )
                        continue
                    else:
                        text = f"{human_size(group_size):>10}  {size_bar(group_size, largest_group)}  {len(entries):>5}  {display_name}"
                        color = origin_color(name) if mode == "origin" else 0
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
                tree_entries = (
                    tree_with_ancestors(entries, grouped_visible)
                    if tree_mode
                    else entries
                )
                display_entries = tree_rows(
                    entries, grouped_visible, sort_by, tree_mode, collapsed_nodes
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
                # Keep a hierarchy alongside the flattened rendering so tree
                # mode can move between siblings and parent/child nodes.
                display_index = {
                    row_id(entry): index
                    for index, (entry, _) in enumerate(display_entries)
                }
                links = parent_links(tree_entries)
                entry_ids = set(display_index)
                entry_children: dict[str, list[str]] = defaultdict(list)
                for entry in tree_entries:
                    parent_id = links.get(row_id(entry))
                    if parent_id in entry_ids:
                        entry_children[parent_id].append(row_id(entry))
                tree_parents: dict[int, int | None] = {}
                tree_children: dict[int | None, list[int]] = defaultdict(list)
                for index, (entry, _) in enumerate(display_entries):
                    parent = (
                        display_index.get(links.get(row_id(entry)))
                        if tree_mode
                        else None
                    )
                    tree_parents[index] = parent
                    tree_children[parent].append(index)
                browser.selected, browser.offset = clamp_view(
                    browser.selected, browser.offset, len(display_entries), page_size
                )
                location = group_label(name, mode)
                stats = subtree_stats(tree_entries) if tree_mode else {}
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

            if browser.detail is None and mode == "cwd":
                display_index = {
                    item_key(item): i
                    for i, item in enumerate(items)
                    if item[0] == "session"
                }
                links = parent_links(tree_entries) if tree_mode else {}
                entry_children = defaultdict(list)
                for entry in tree_entries:
                    parent_id = links.get(row_id(entry))
                    if parent_id in display_index:
                        entry_children[parent_id].append(row_id(entry))
                tree_parents = {
                    i: display_index.get(links.get(item_key(item)))
                    for i, item in enumerate(items)
                }
                tree_children = defaultdict(list)
                for index, parent in tree_parents.items():
                    tree_children[parent].append(index)

            footer_sessions = (
                browser.detail[1]
                if browser.detail is not None
                else [
                    entry
                    for entry in grouped_visible
                    if in_scope(entry, browser.cwd_node)
                ]
            )
            footer = (
                f"Transcripts: {human_size(sum(session.size for session in footer_sessions))}  "
                f"{len(footer_sessions):,} sessions"
            )
            if warning := scan_notice():
                footer += "  " + warning
            label = f" asdu  {location}"
            if source_filter != "all":
                label += f"  [{source_filter}]"
            ordering = ("tree " if tree_mode else "") + sort_label(sort_by) + " "
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
                mode == "cwd" and browser.cwd_node != cwd_root
            ):
                commands.append("Backspace back")
            if browser.detail is None:
                commands.append("g group")
            commands.extend(["f filter", "s sort"])
            if has_sessions:
                commands.append("t tree")
            if selected_session:
                commands.append("a action")
            commands.append("? help")
            draw_line(window, height - 2, " " + (status or footer))
            draw_line(window, height - 1, " " + "  ".join(commands), invert=True)

            window.refresh()
            siblings = (
                tree_children.get(tree_parents.get(browser.selected), [])
                if tree_mode
                else []
            )
            first = browser.selected == (siblings[0] if siblings else 0)
            last = browser.selected == (siblings[-1] if siblings else max(0, total - 1))
            key = read_navigation(window, first, last)
            browser.selected, key = drain_navigation(
                window, key, browser.selected, siblings or range(total)
            )
            if key == -1:
                continue
            selected_entry = (
                display_entries[browser.selected][0]
                if browser.detail is not None and display_entries
                else items[browser.selected][2][0]
                if browser.detail is None
                and items
                and items[browser.selected][0] == "session"
                else None
            )
            if browser.detail is None and mode == "cwd" and selected_entry is None:
                if key in (curses.KEY_RIGHT, ord("l")):
                    key = 10
                elif key in (curses.KEY_LEFT, ord("h")):
                    key = 127
            status = ""
            if key in (ord("/"), ord("n"), ord("N")):
                if key == ord("/"):
                    query = search_prompt(window)
                labels = (
                    [session_label(entry) for entry, _ in display_entries]
                    if browser.detail
                    else [
                        session_label(es[0])
                        if kind == "session"
                        else group_label(name, mode)
                        for kind, name, es in items
                    ]
                )
                found = find_match(
                    labels, query, browser.selected, -1 if key == ord("N") else 1
                )
                status = (
                    f"/{query}"
                    if any(query.casefold() in label.casefold() for label in labels)
                    else f"No match: {query}"
                )
                browser.selected = found
                continue
            if key in (curses.KEY_HOME, curses.KEY_END):
                browser.selected = 0 if key == curses.KEY_HOME else max(0, total - 1)
                continue
            if key in (ord("q"), 27):
                return
            if key == ord("?"):
                text_view(
                    window,
                    "Help",
                    "Sizes: saved conversation bytes, not project files. Updated: file modification time.\n"
                    "Tag groups: primary tag only; briefs list every matching tag.\n\n"
                    "Enter: open folder or session brief\n/: find text; n/N: next/previous match\nHome/End: first/last\nBackspace: parent folder or previous list\nr: rescan local session roots\nf: filter by source\ns: choose sort\ng: group by folder, tag, source, or session type\nt: toggle session tree (↑↓ siblings, ←→ parent/child, Space fold, z all)\na: archive or move selected session to Trash\ni: open a session brief\nq: quit",
                )
                continue
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
                    _, progress_width = window.getmaxyx()
                    progress_height, _ = window.getmaxyx()
                    lines = indexing_lines(
                        min(progress_width - 1, 47)
                        if progress_height < 15
                        else progress_width - 1,
                        source,
                        current,
                        done_bytes,
                        total_bytes,
                    )
                    top, left = splash_position(
                        progress_width - 1, progress_height, lines
                    )
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
                    sessions[:] = rescan(show_rescan)
                except OSError as error:
                    status = f"Rescan failed: {error}"
                else:
                    refresh_view()
                continue
            if (
                selected_entry is not None
                and tree_mode
                and key
                in (
                    curses.KEY_DOWN,
                    ord("j"),
                    curses.KEY_UP,
                    ord("k"),
                    curses.KEY_LEFT,
                    ord("h"),
                    curses.KEY_RIGHT,
                    ord("l"),
                )
            ):
                entry = selected_entry
                parent = tree_parents.get(browser.selected)
                siblings = tree_children.get(parent, [])
                sibling_position = (
                    siblings.index(browser.selected)
                    if browser.selected in siblings
                    else 0
                )
                if key in (curses.KEY_DOWN, ord("j")) and sibling_position + 1 < len(
                    siblings
                ):
                    browser.selected = siblings[sibling_position + 1]
                elif key in (curses.KEY_UP, ord("k")) and sibling_position > 0:
                    browser.selected = siblings[sibling_position - 1]
                elif key in (curses.KEY_RIGHT, ord("l")):
                    if row_id(entry) in collapsed_nodes:
                        collapsed_nodes.remove(row_id(entry))
                    elif tree_children.get(browser.selected):
                        browser.selected = tree_children[browser.selected][0]
                elif key in (curses.KEY_LEFT, ord("h")):
                    if (
                        entry_children.get(row_id(entry))
                        and row_id(entry) not in collapsed_nodes
                    ):
                        collapsed_nodes.add(row_id(entry))
                    elif parent is not None:
                        browser.selected = parent
            elif selected_entry is not None and tree_mode and key == ord(" "):
                entry = selected_entry
                if entry_children.get(row_id(entry)):
                    if row_id(entry) in collapsed_nodes:
                        collapsed_nodes.remove(row_id(entry))
                    else:
                        collapsed_nodes.add(row_id(entry))
            elif tree_mode and key == ord("z"):
                if browser.detail is None and items:
                    browser.pending_anchor = item_key(items[browser.selected])
                nodes_with_children = set(entry_children)
                if nodes_with_children.issubset(collapsed_nodes):
                    collapsed_nodes.clear()
                else:
                    collapsed_nodes.update(nodes_with_children)
            elif key in (curses.KEY_DOWN, ord("j")):
                if browser.selected < total - 1:
                    browser.selected += 1
            elif key in (curses.KEY_UP, ord("k")):
                if browser.selected > 0:
                    browser.selected -= 1
            elif key == curses.KEY_NPAGE:
                if browser.selected < total - 1:
                    browser.selected = min(total - 1, browser.selected + page_size)
            elif key == curses.KEY_PPAGE:
                if browser.selected > 0:
                    browser.selected = max(0, browser.selected - page_size)
            elif browser.detail is None and key == ord("g"):
                choice = choose(
                    window,
                    "Group sessions by",
                    ["cwd", "tag", "source", "origin"],
                    mode,
                )
                if choice is not None:
                    mode = choice
                    browser.selected, browser.offset = 0, 0
            elif browser.detail is None and mode == "cwd" and key == ord("t"):
                browser.pending_anchor = (
                    item_key(items[browser.selected]) if items else None
                )
                tree_mode, collapsed_nodes = browser.toggle_tree(tree_entries)
            elif browser.detail is not None and key == ord("t"):
                # Build the same contextual tree used for rendering.  A
                # parent outside this tag would otherwise be added only on
                # the next frame and escape the initial folded set.
                contextual_tree = tree_with_ancestors(
                    browser.detail[1], grouped_visible
                )
                tree_mode, collapsed_nodes = browser.toggle_tree(contextual_tree)
                browser.selected, browser.offset = 0, 0
            elif key == ord("a") and (
                browser.detail is not None
                or (items and items[browser.selected][0] == "session")
            ):
                entry = (
                    display_entries[browser.selected][0]
                    if browser.detail is not None
                    else items[browser.selected][2][0]
                )
                choice = confirm_session_action(window, entry)
                if choice == "trash" and confirm_deletes:
                    confirmation = confirm_trash(window)
                    if confirmation is None:
                        choice = None
                    elif confirmation == "don't ask again":
                        disable_delete_confirmation()
                        confirm_deletes = False
                if choice is not None:
                    try:
                        if choice == "archive":
                            destination = archive_session(entry)
                            try:
                                record_action("archive", entry, destination)
                            except OSError as error:
                                status = f"Archived; action log unavailable: {error}"
                        else:
                            require_unchanged(entry)
                            move_to_trash(entry.path)
                            try:
                                record_action("trash", entry)
                            except OSError as error:
                                status = f"Trashed; action log unavailable: {error}"
                    except OSError as error:
                        status = str(error)
                    else:
                        sessions.remove(entry)
                        if browser.detail is not None and entry in browser.detail[1]:
                            browser.detail[1].remove(entry)
            elif key == ord("f"):
                available = ["all", *sorted({session.source for session in sessions})]
                choice = choose(window, "Source", available, source_filter)
                if choice is not None:
                    if browser.detail is None and items:
                        browser.pending_anchor = item_key(items[browser.selected])
                    source_filter = choice
                    refresh_view()
            elif browser.detail is not None and key == ord("s"):
                choice = choose(
                    window, "Sort sessions", ["size", "date", "name"], sort_by
                )
                if choice is not None:
                    browser.pending_anchor = (
                        row_id(display_entries[browser.selected][0])
                        if display_entries
                        else None
                    )
                    sort_by = choice
            elif browser.detail is None and key == ord("s"):
                choice = choose(
                    window, "Sort", ["size", "date", "count", "name"], sort_by
                )
                if choice is not None:
                    browser.pending_anchor = (
                        item_key(items[browser.selected]) if items else None
                    )
                    sort_by = choice
            elif browser.detail is None and key in (curses.KEY_ENTER, 10, 13):
                if mode == "cwd":
                    if not items:
                        continue
                    kind, name, entries = items[browser.selected]
                    if kind == "folder":
                        browser.visit_folder(
                            browser.cwd_node / name, item_key(items[browser.selected])
                        )
                        continue
                    else:
                        open_brief(
                            window, entries[0], tree_entries if tree_mode else visible
                        )
                        continue
                else:
                    items = browser_group_items(grouped_visible, mode, sort_by)
                    if not items:
                        continue
                    _, name, entries = items[browser.selected]
                    browser.detail = (name, entries)
                    browser.detail_return = (
                        item_key(items[browser.selected]),
                        browser.offset,
                    )
                    tree_entries = tree_with_ancestors(entries, grouped_visible)
                    tree_mode, collapsed_nodes = browser.open_group(
                        mode, name, source_filter, browser.cwd_node, tree_entries
                    )
                browser.selected, browser.offset = 0, 0
            elif (
                browser.detail is None
                and mode == "cwd"
                and key in (curses.KEY_BACKSPACE, 127, 8)
            ):
                if browser.cwd_node != cwd_root:
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
                entry = display_entries[browser.selected][0]
                open_brief(window, entry, tree_entries if tree_mode else visible)

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
    parser.add_argument("--version", action="version", version="asdu 0.1.3")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("browse", "summary", "sessions", "digest"),
        default="browse",
    )
    parser.add_argument(
        "--codex-root",
        type=Path,
        default=Path.home() / ".codex" / "sessions",
        help="Codex session storage directory",
    )
    parser.add_argument(
        "--claude-root",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="Claude session storage directory",
    )
    parser.add_argument(
        "--source",
        choices=("codex", "claude"),
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
        "--confirm-delete",
        action="store_true",
        help="Ask before deletion even when confirmation was disabled",
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
    sources = tuple(dict.fromkeys(args.source or ("codex", "claude")))
    adapters = source_adapters(args.codex_root, args.claude_root)
    source_roots = {source: adapter.root for source, adapter in adapters.items()}
    if args.source:
        for source in sources:
            if not source_roots[source].exists():
                parser.error(
                    f"{source} session root does not exist: {source_roots[source]}"
                )
    elif not any(root.exists() for root in source_roots.values()):
        parser.error(
            "no supported session roots found; pass --codex-root or --claude-root"
        )
    scope = None if args.all else (args.project or Path.cwd()).resolve()

    scan_warning = ""

    def scan_current(
        show_progress: bool,
        render: Callable[[str, int, int, int, int], None] | None = None,
    ) -> list[Session]:
        nonlocal scan_warning
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
        scan_warning = progress.notice()
        found = [session for session in found if in_scope(session, scope)]
        return found

    sessions = scan_current(True)
    if scan_warning and args.command != "browse":
        print(scan_warning, file=sys.stderr)

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
            args.confirm_delete or not skip_delete_confirmation(),
            lambda render: scan_current(True, render),
            lambda: scan_warning,
            no_color=args.no_color,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
