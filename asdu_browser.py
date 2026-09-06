"""Pure grouping, tree relationships, and per-run navigation state."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from asdu_sessions import Session, in_scope, session_label

ALL_SESSIONS = "__asdu_all_sessions__"


def primary_tag(session: Session) -> str:
    return session.tags[0]


def group_sessions(sessions: Iterable[Session], mode: str) -> dict[str, list[Session]]:
    groups: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        if mode == "tag":
            key = primary_tag(session)
        elif mode == "source":
            key = session.source
        elif mode == "origin":
            key = session.origin
        else:
            key = session.cwd
        groups[key].append(session)
    return groups


def browser_visible_sessions(
    sessions: list[Session], source_filter: str, mode: str, cwd_node: Path
) -> tuple[list[Session], list[Session]]:
    """Apply the TUI's source filter and virtual-folder scope in one place."""
    visible = (
        sessions
        if source_filter == "all"
        else [session for session in sessions if session.source == source_filter]
    )
    return visible, visible if mode == "cwd" else [
        session for session in visible if in_scope(session, cwd_node)
    ]


def item_key(item: tuple[str, str, list[Session]]) -> str:
    kind, name, entries = item
    return row_id(entries[0]) if kind == "session" else f"{kind}:{name}"


def browser_group_items(
    sessions: list[Session], mode: str, sort_by: str
) -> list[tuple[str, str, list[Session]]]:
    """Build virtual group rows; /all sessions is navigation, never a tag."""
    items = [
        ("group", name, entries)
        for name, entries in ordered_groups(group_sessions(sessions, mode), sort_by)
    ]
    if mode == "tag" and sessions:
        items.insert(0, ("group", ALL_SESSIONS, sessions))
    return items


def group_label(name: str, mode: str) -> str:
    """Render virtual and provenance groups without exposing internal keys."""
    if name == ALL_SESSIONS:
        return "all sessions"
    return origin_label(name) if mode == "origin" else name


def sort_label(sort_by: str) -> str:
    return {"size": "size↓", "date": "updated↓", "count": "count↓", "name": "name↑"}[
        sort_by
    ]


def ordered_groups(
    groups: dict[str, list[Session]], sort_by: str
) -> list[tuple[str, list[Session]]]:
    if sort_by == "name":
        return sorted(groups.items(), key=lambda item: item[0].lower())
    if sort_by == "date":
        return sorted(
            groups.items(),
            key=lambda item: (
                -max(session.modified for session in item[1]),
                item[0].lower(),
            ),
        )
    if sort_by == "count":
        return sorted(groups.items(), key=lambda item: (-len(item[1]), item[0].lower()))
    return sorted(
        groups.items(),
        key=lambda item: (-sum(s.size for s in item[1]), item[0].lower()),
    )


def cwd_listing(
    sessions: Iterable[Session], directory: Path, sort_by: str
) -> list[tuple[str, str, list[Session]]]:
    """Folder rows plus direct session rows, like an ncdu directory view."""
    folders: dict[str, list[Session]] = defaultdict(list)
    direct: list[Session] = []
    for session in sessions:
        if session.cwd == "(unknown)":
            continue
        try:
            relative = Path(session.cwd).resolve().relative_to(directory)
        except ValueError:
            continue
        if relative == Path("."):
            direct.append(session)
        else:
            folders[relative.parts[0]].append(session)
    result = [
        ("folder", name, entries) for name, entries in ordered_groups(folders, sort_by)
    ]
    if sort_by == "name":
        direct.sort(key=lambda session: session_label(session).casefold())
    elif sort_by == "date":
        direct.sort(key=lambda session: session.modified, reverse=True)
    else:
        direct.sort(key=lambda session: session.size, reverse=True)
    result.extend(("session", session.title, [session]) for session in direct)
    return result


def relative_folder(directory: Path, root: Path) -> str:
    try:
        value = directory.relative_to(root)
    except ValueError:
        return directory.name
    return str(value) if str(value) != "." else "."


def ordered_sessions(sessions: Iterable[Session], sort_by: str) -> list[Session]:
    if sort_by == "name":
        return sorted(sessions, key=lambda session: session_label(session).casefold())
    if sort_by == "date":
        return sorted(sessions, key=lambda session: session.modified, reverse=True)
    return sorted(sessions, key=lambda session: session.size, reverse=True)


def row_id(entry: Session) -> str:
    return f"{entry.source}:{entry.path}"


def parent_links(entries: Iterable[Session]) -> dict[str, str]:
    entries = list(entries)
    candidates: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for entry in entries:
        candidates[entry.source, entry.session_id].append(entry)
    links = {}
    for entry in entries:
        parents = candidates.get((entry.source, entry.parent_id), [])
        if len(parents) == 1 and row_id(parents[0]) != row_id(entry):
            links[row_id(entry)] = row_id(parents[0])
    for start in list(links):
        seen = set()
        node = start
        while node in links:
            if node in seen:
                del links[node]
                break
            seen.add(node)
            node = links[node]
    return links


def subtree_stats(entries: Iterable[Session]) -> dict[str, tuple[int, int]]:
    """Physical bytes and descendant counts; shared history is not deduplicated."""
    unique = {row_id(entry): entry for entry in entries}
    links = parent_links(unique.values())
    totals = {key: [entry.size, 0] for key, entry in unique.items()}
    for key, entry in unique.items():
        while key in links:
            key = links[key]
            totals[key][0] += entry.size
            totals[key][1] += 1
    return {key: (size, count) for key, (size, count) in totals.items()}


def session_tree(
    entries: list[Session], sort_by: str, collapsed: set[str] | None = None
) -> list[tuple[Session, str]]:
    """Return a stable, forest-shaped view using native parent thread IDs."""
    collapsed = collapsed or set()
    links = parent_links(entries)
    stats = subtree_stats(entries)
    children: dict[str, list[Session]] = defaultdict(list)
    roots: list[Session] = []
    for entry in entries:
        if row_id(entry) in links:
            children[links[row_id(entry)]].append(entry)
        else:
            roots.append(entry)

    def key(entry):
        name = session_label(entry).casefold()
        if sort_by == "name":
            return (name,)
        if sort_by == "date":
            return (-entry.modified, name)
        return (-stats[row_id(entry)][0], name)

    roots.sort(key=key)
    for nodes in children.values():
        nodes.sort(key=key)
    result: list[tuple[Session, str]] = []

    def visit(entry: Session, prefix: str, branch: str, seen: set[str]) -> None:
        nodes = children.get(row_id(entry), [])
        marker = "▸ " if nodes and row_id(entry) in collapsed else "▾ " if nodes else ""
        result.append((entry, prefix + branch + marker))
        if row_id(entry) in seen or row_id(entry) in collapsed:
            return
        next_seen = seen | {row_id(entry)}
        child_prefix = (
            prefix
            + ("   " if branch == "└─ " else "│  " if branch == "├─ " else "")
            + ("  " if nodes else "")
        )
        for index, child in enumerate(nodes):
            last = index == len(nodes) - 1
            visit(child, child_prefix, "└─ " if last else "├─ ", next_seen)

    for root in roots:
        visit(root, "", "", set())
    return result


def tree_with_ancestors(
    entries: list[Session], candidates: Iterable[Session]
) -> list[Session]:
    """Add native parents needed to render a selected group as a real tree.

    A tag group is a view, not a conversation boundary: a parent can reasonably
    classify as ``tooling`` while its workers classify as a project tag.  The
    added sessions are structural context only; callers keep ``entries`` for
    group totals and membership.
    """
    candidate_lists: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for entry in candidates:
        candidate_lists[(entry.source, entry.session_id)].append(entry)
    # Session IDs are unique for Codex, but Claude can emit several transcript
    # files for one session.  Only a unique ID is safe to use as a parent link.
    known = {
        key: values[0] for key, values in candidate_lists.items() if len(values) == 1
    }
    result = {(entry.source, str(entry.path)): entry for entry in entries}
    pending = list(entries)
    while pending:
        entry = pending.pop()
        if not entry.parent_id:
            continue
        parent = known.get((entry.source, entry.parent_id))
        key = (parent.source, str(parent.path)) if parent else None
        if parent is not None and key not in result:
            result[key] = parent
            pending.append(parent)
    return list(result.values())


def tree_rows(
    entries: list[Session],
    candidates: Iterable[Session],
    sort_by: str,
    enabled: bool,
    collapsed: set[str],
) -> list[tuple[Session, str]]:
    """Build display rows without curses; useful to both the UI and tests."""
    if not enabled:
        return [(entry, "") for entry in ordered_sessions(entries, sort_by)]
    return session_tree(tree_with_ancestors(entries, candidates), sort_by, collapsed)


def clamp_view(
    selected: int, offset: int, total: int, page_size: int
) -> tuple[int, int]:
    """Keep a selection visible, including empty and shrinking views."""
    selected = min(max(0, total - 1), selected)
    offset = min(offset, max(0, total - page_size))
    if selected < offset:
        offset = selected
    elif selected >= offset + page_size:
        offset = selected - page_size + 1
    return selected, offset


def folded_tree_nodes(entries: Iterable[Session]) -> set[str]:
    """Return parent IDs to fold when a tree view is first opened."""
    return set(parent_links(entries).values())


@dataclass
class BrowserState:
    """Ephemeral per-run UI state, intentionally never serialized."""

    tree_modes: set[tuple[str, str, str, str]]
    tree_folds: dict[tuple[str, str, str, str], set[str]]
    detail_key: tuple[str, str, str, str] | None = None
    selected: int = 0
    offset: int = 0
    detail: tuple[str, list[Session]] | None = None
    detail_return: tuple[str, int] | None = None
    pending_anchor: str | None = None
    cwd_node: Path = Path("/")
    folder_positions: dict[Path, tuple[str | None, int]] = field(default_factory=dict)

    @classmethod
    def create(cls, cwd: Path = Path("/")) -> BrowserState:
        return cls(set(), {}, cwd_node=cwd)

    def visit_folder(self, target: Path, anchor: str | None) -> None:
        self.folder_positions[self.cwd_node] = (anchor, self.offset)
        self.cwd_node = target
        self.pending_anchor, self.offset = self.folder_positions.get(target, (None, 0))
        self.selected = 0

    def leave_detail(self) -> None:
        self.detail = None
        self.pending_anchor, self.offset = self.detail_return or (None, 0)
        self.selected = 0
        self.detail_return = None
        self.close_group()

    def open_group(
        self,
        mode: str,
        name: str,
        source_filter: str,
        cwd: Path,
        entries: Iterable[Session],
    ) -> tuple[bool, set[str]]:
        key = (mode, name, source_filter, str(cwd))
        self.detail_key = key
        enabled = key in self.tree_modes
        folds = (
            self.tree_folds.setdefault(key, folded_tree_nodes(entries))
            if enabled
            else set()
        )
        return enabled, folds

    def toggle_tree(self, entries: Iterable[Session]) -> tuple[bool, set[str]]:
        if self.detail_key is None:
            return False, set()
        if self.detail_key in self.tree_modes:
            self.tree_modes.remove(self.detail_key)
            return False, set()
        self.tree_modes.add(self.detail_key)
        return True, self.tree_folds.setdefault(
            self.detail_key, folded_tree_nodes(entries)
        )

    def close_group(self) -> None:
        self.detail_key = None


def find_match(labels: list[str], query: str, start: int, step: int = 1) -> int:
    if query:
        for distance in range(1, len(labels) + 1):
            index = (start + step * distance) % len(labels)
            if query.casefold() in labels[index].casefold():
                return index
    return start


def origin_label(origin: str) -> str:
    return {
        "primary": "main",
        "subagent": "child",
        "sidechain": "side",
        "automation": "auto",
        "user": "user",
        "review": "review",
        "ide": "ide",
    }.get(origin, origin)
