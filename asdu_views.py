"""Terminal formatting, rows, dialogs, and session briefs for asdu."""

from __future__ import annotations

import curses
import re
import shlex
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path

from asdu_browser import (
    group_sessions,
    ordered_groups,
    origin_label,
    row_id,
    subtree_stats,
)
from asdu_sessions import Session, SessionControls, session_label
from asdu_sources import available_actions, read_brief, session_controls

ASCII_UI = False
CONTENT_RIGHT_MARGIN = 3
SELECTED_COLOR = 9
SIZE_COLUMN_WIDTH = 10
SOURCE_COLUMN_WIDTH = 6
TYPE_COLUMN_WIDTH = 6
DATE_COLUMN_WIDTH = 8


def human_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    raise AssertionError("unreachable")


def session_date(session: Session) -> str:
    seconds = max(0, int(time.time() - session.modified))
    for limit, divisor, suffix in (
        (10, 1, "now"),
        (60, 1, "s ago"),
        (3600, 60, "m ago"),
        (86400, 3600, "h ago"),
        (30 * 86400, 86400, "d ago"),
        (365 * 86400, 30 * 86400, "mo ago"),
    ):
        if seconds < limit:
            return suffix if suffix == "now" else f"{seconds // divisor}{suffix}"
    return f"{seconds // (365 * 86400)}y ago"


def ascii_ui() -> bool:
    try:
        "╭█".encode(sys.stderr.encoding or "utf-8")
    except UnicodeEncodeError:
        return True
    return ASCII_UI


def terminal_art(text: str) -> str:
    """Keep content intact while replacing terminal decoration in ASCII mode."""
    if not ascii_ui():
        return text
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


def row_bar_width(terminal_width: int) -> int:
    return 12 if terminal_width >= 100 else 8 if terminal_width >= 90 else 0


def progress_bar(percent: int, width: int) -> str:
    filled = min(width, max(0, width * percent // 100))
    return "[" + ("#" if ascii_ui() else "=") * filled + " " * (width - filled) + "]"


def splash_lines(width: int) -> list[str]:
    """Return the centered logo using supported terminal glyphs."""
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
    if width < logo_width:
        return ["asdu", "agent session disk usage"]
    left = " " * max(0, (width - 48) // 2)
    inner_left = " " * ((48 - logo_width) // 2)
    return [left + pad_display(inner_left + line, 48) for line in logo] + [
        left + " " * 48,
        left + "agent session disk usage".center(48),
    ]


def indexing_panel(source: str, current: int, done: int, total: int) -> list[str]:
    """Fixed-width progress panel whose three content rows update in place."""
    percent = min(100, max(0, done * 100 // total)) if total else 0
    status = f"Indexing {source}" if source else "Discovering sessions"
    count = f"{current:,} sessions"

    def row(text: str) -> str:
        return terminal_art("│ " + pad_display(compact_text(text, 44), 44) + " │")

    return [
        terminal_art("╭" + "─" * 46 + "╮"),
        row(status),
        row(f"{size_bar(percent, 100, 36 if ascii_ui() else 38)}  {percent:3d}%"),
        row(pad_display(count, max(0, 44 - len(human_size(done)))) + human_size(done)),
        terminal_art("╰" + "─" * 46 + "╯"),
    ]


def scan_status(
    source: str, current: int, count: int, done: int, total: int, width: int
) -> str:
    percent = min(100, max(0, done * 100 // total)) if total else 0
    suffix = (
        f" {percent:3d}%  {current:,}/{count:,} sessions  "
        f"{human_size(done)} / {human_size(total)}"
    )
    prefix = f"asdu: indexing {source} "
    bar_width = max(8, min(24, width - display_width(prefix + suffix) - 2))
    return compact_text(prefix + progress_bar(percent, bar_width) + suffix, width)


def compact_path(path: str, width: int) -> str:
    return path if len(path) <= width else "…" + path[-(width - 1) :]


def compact_text(text: str, width: int) -> str:
    """Truncate prose from the right; its opening words carry the context."""
    if width <= 0:
        return ""
    text = "".join(character if character.isprintable() else " " for character in text)
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for character in text:
        character_width = display_width(character)
        if used + character_width > width - 1:
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
    messages = data.user_messages + data.assistant_messages
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
        *commands,
    ]
    return "\n".join(lines)


def print_summary(sessions: list[Session], mode: str, sort_by: str) -> None:
    groups = ordered_groups(group_sessions(sessions, mode), sort_by)
    width = max((len(name) for name, _ in groups), default=10)
    label = {"cwd": "folder", "origin": "session type"}.get(mode, mode)
    print(
        f"{len(sessions):,} sessions  "
        f"{human_size(sum(s.size for s in sessions))}  grouped by {label}\n"
    )
    print(f"{'group':<{width}}  sessions       size")
    print(f"{'-' * width}  --------  ---------")
    for name, entries in groups:
        print(
            f"{name:<{width}}  {len(entries):>8,}  "
            f"{human_size(sum(s.size for s in entries)):>10}"
        )


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
    if selected:
        attr = color_attr(window, SELECTED_COLOR) | curses.A_BOLD
        if not getattr(window, "colors_enabled", True):
            attr |= curses.A_REVERSE
    else:
        attr = color_attr(window, color)
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


def source_color(source: str) -> int:
    return {"codex": 7, "claude": 8, "omp": 10}.get(source, 0)


def session_type_label(session: Session) -> str:
    """Archived is the actionable state; the brief retains the stored type."""
    return "arch" if session.archived else origin_label(session.origin)


def session_column_header(width: int, show_folder: bool = False) -> str:
    """Build a header from the same widths used by session rows."""
    bar_width = row_bar_width(width)
    columns = f"  {'size':>{SIZE_COLUMN_WIDTH}}  "
    if bar_width:
        columns += f"{'share':<{bar_width}}  "
    columns += (
        f"{'source':<{SOURCE_COLUMN_WIDTH}}  "
        f"{'type':<{TYPE_COLUMN_WIDTH}}  "
        f"{'updated':>{DATE_COLUMN_WIDTH}}  "
    )
    return columns + ("folder / session" if show_folder else "session")


def draw_session_line(
    window: curses.window,
    row: int,
    session: Session,
    selected: bool,
    branch: str = "",
    stats: tuple[int, int] | None = None,
    largest: int = 0,
    folder: str = "",
) -> None:
    """Render stable columns; only the free-text tail is allowed to truncate."""
    _, width = window.getmaxyx()
    size, descendants = stats if stats is not None else (session.size, 0)
    bar_width = row_bar_width(width)
    bar = f"{size_bar(size, largest, bar_width)}  " if bar_width else ""
    prefix = f"  {human_size(size):>{SIZE_COLUMN_WIDTH}}  {bar}"
    source = f"{session.source:<{SOURCE_COLUMN_WIDTH}}  "
    kind = f"{session_type_label(session):<{TYPE_COLUMN_WIDTH}}  "
    date = f"{session_date(session):>{DATE_COLUMN_WIDTH}}  "
    folder_text = f"{compact_path(folder, 24):<24}  " if folder else ""
    branch = terminal_art(branch)
    title_width = max(
        0,
        width
        - 1
        - CONTENT_RIGHT_MARGIN
        - display_width(prefix + source + kind + date + folder_text + branch),
    )
    count = f" (+{descendants})" if descendants else ""
    title = pad_display(
        compact_text(session_label(session), max(0, title_width - len(count))) + count,
        title_width,
    )
    line = prefix + source + kind + date + folder_text + branch + title
    if selected:
        draw_line(
            window,
            row,
            line,
            selected=True,
            pointer=True,
            right_margin=CONTENT_RIGHT_MARGIN,
        )
        return
    limit = max(0, width - 1 - CONTENT_RIGHT_MARGIN)
    column = 0
    for text, color, style in (
        (prefix, 0, 0),
        (source, source_color(session.source), curses.A_BOLD),
        (kind, 0, 0),
        (date, 0, curses.A_DIM),
        (folder_text + branch + title, 0, 0),
    ):
        if column >= limit:
            break
        attr = color_attr(window, color)
        attr |= style | (curses.A_DIM if session.archived else 0)
        text = compact_text(text, limit - column)
        window.addnstr(row, column, text, len(text), attr)
        column += display_width(text)


def action_dialog(window, title, body, options, default, shortcuts, labels=None):
    """Use identical keys and confirmation defaults at every terminal size."""
    selected = options.index(default)
    while True:
        height, width = window.getmaxyx()
        boxed = width >= 44 and height >= len(body) + len(options) + 5
        box_width = min(100, width - 4) if boxed else max(1, width - 1)
        left = (width - box_width) // 2 if boxed else 0
        top = (height - len(body) - len(options) - 4) // 2 if boxed else 0
        if not boxed:
            window.erase()
        inner = box_width - 2
        rendered_options = [(labels or {}).get(option, option) for option in options]
        lines = [title, *body, "", *rendered_options]
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


def short_path(path: str) -> str:
    home = str(Path.home())
    return (
        "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path
    )


def confirm_session_action(
    window: curses.window, session: Session, subtree: list[Session] | None = None
) -> str | None:
    subtree = subtree or [session]
    descendants = max(0, len(subtree) - 1)
    supported = available_actions(session)
    actions = [
        action
        for action in ("archive", "unarchive", "trash", "delete")
        if action in supported
    ]
    options = [
        choice
        for action in actions
        for choice in ([action, f"{action} tree"] if descendants else [action])
    ]
    total = sum(entry.size for entry in subtree)
    body = [
        session_label(session),
        (
            f"{session.source} | {human_size(session.size)} | "
            f"{short_path(session.cwd)} | {session_date(session)} | "
            f"{session.session_id[:18]}"
        ),
    ]
    labels = {}
    verbs = {
        "archive": "Archive",
        "unarchive": "Unarchive",
        "trash": "Trash",
        "delete": "Delete",
    }
    keys = {"archive": "a", "unarchive": "u", "trash": "t", "delete": "d"}
    for option in options:
        action = option.removesuffix(" tree")
        tree_action = option.endswith(" tree")
        affected = total if tree_action else session.size
        count = len(subtree) if tree_action else 1
        if action == "archive":
            destination = (
                short_path(str(Path(session.source_home) / "archived_sessions"))
                if session.source_home
                else "Codex archive"
            )
            detail = f"move to {destination} | frees 0 B | reversible"
        elif action == "unarchive":
            destination = (
                short_path(str(Path(session.source_home) / "sessions"))
                if session.source_home
                else "active sessions"
            )
            detail = f"restore to {destination} | frees 0 B"
        elif action == "trash":
            detail = f"system Trash | frees {human_size(affected)} | recoverable"
        else:
            detail = f"run codex delete | permanent | frees {human_size(affected)}"
        suffix = f" | {count} sessions" if tree_action else ""
        key = keys[action].upper() if tree_action else keys[action]
        labels[option] = (
            f"{key}  {verbs[action]}{' tree' if tree_action else '':<9}  "
            f"{detail}{suffix}"
        )
    labels["cancel"] = "   Cancel"
    return action_dialog(
        window,
        "Session action",
        body,
        [*options, "cancel"],
        actions[0] if actions else "cancel",
        {
            ord(key): choice + (" tree" if key.isupper() and descendants else "")
            for key, choice in (
                ("a", "archive"),
                ("u", "unarchive"),
                ("t", "trash"),
                ("d", "delete"),
                ("A", "archive"),
                ("U", "unarchive"),
                ("T", "trash"),
                ("D", "delete"),
            )
            if choice in actions
        },
        labels,
    )


def confirm_action(window, title: str, question: str, count: int) -> bool:
    target = "this session" if count == 1 else f"these {count} sessions"
    return (
        action_dialog(
            window,
            title,
            [question.format(target=target)],
            ["yes", "no"],
            "no",
            {ord("y"): "yes", ord("Y"): "yes"},
        )
        == "yes"
    )


def confirm_trash(window: curses.window, count: int = 1) -> bool:
    return confirm_action(
        window, "Confirm Trash", "Move {target} to system Trash?", count
    )


def confirm_permanent_delete(window: curses.window, count: int = 1) -> bool:
    return confirm_action(
        window,
        "Confirm delete",
        "Permanently delete {target} through Codex?",
        count,
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


def search_prompt(window: curses.window) -> str | None:
    """Read a small inline query; Escape cancels without becoming input."""
    height, width = window.getmaxyx()
    label = "Find: "
    query = ""
    while True:
        draw_line(
            window,
            height - 1,
            (label + query).ljust(max(0, width - 1)),
            invert=True,
        )
        window.refresh()
        key = window.getch()
        if key == 27:
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            return query.strip()
        if key in (curses.KEY_BACKSPACE, 127, 8):
            query = query[:-1]
        elif (
            32 <= key <= 0x10FFFF
            and display_width(label + query + chr(key)) < width - 1
        ):
            query += chr(key)


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
