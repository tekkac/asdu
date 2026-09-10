"""Pure grouping, tree relationships, and per-run navigation state."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from asdu_sessions import Session, in_scope, session_label


def session_type(session: Session) -> str:
    """Return the small structural type vocabulary exposed by the filter."""
    return {
        "primary": "main",
        "subagent": "child",
        "sidechain": "child",
        "review": "review",
    }.get(session.origin, session.origin)


def group_sessions(sessions: Iterable[Session], mode: str) -> dict[str, list[Session]]:
    groups: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        groups[session.source if mode == "source" else session.cwd].append(session)
    return groups


def browser_visible_sessions(
    sessions: list[Session],
    source_filter: str,
    type_filter: str,
    mode: str,
    cwd_node: Path,
) -> tuple[list[Session], list[Session]]:
    """Compose filters, then apply virtual-folder scope for grouped views."""
    visible = [
        session
        for session in sessions
        if (source_filter == "all" or session.source == source_filter)
        and (type_filter == "all" or session_type(session) == type_filter)
    ]
    grouped = (
        visible
        if cwd_node == Path("/")
        else [session for session in visible if in_scope(session, cwd_node)]
    )
    return visible, visible if mode == "cwd" else grouped


def item_key(item: tuple[str, str, list[Session]]) -> str:
    kind, name, entries = item
    return row_id(entries[0]) if kind == "session" else f"{kind}:{name}"


def browser_group_items(
    sessions: list[Session], mode: str, sort_by: str
) -> list[tuple[str, str, list[Session]]]:
    """Build virtual group rows."""
    return [
        ("group", name, entries)
        for name, entries in ordered_groups(group_sessions(sessions, mode), sort_by)
    ]


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
            if directory == Path("/"):
                folders["(unknown folder)"].append(session)
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
        value = directory
    text = str(value) if str(value) != "." else str(root)
    home = str(Path.home())
    return (
        "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text
    )


def ordered_sessions(sessions: Iterable[Session], sort_by: str) -> list[Session]:
    if sort_by == "name":
        return sorted(sessions, key=lambda session: session_label(session).casefold())
    if sort_by == "date":
        return sorted(sessions, key=lambda session: session.modified, reverse=True)
    return sorted(sessions, key=lambda session: session.size, reverse=True)


def row_id(entry: Session) -> str:
    return entry.storage_key


def lineage_parent(entry: Session) -> str | None:
    """Prefer execution ancestry, then a recorded conversation fork."""
    return entry.parent_id or entry.forked_from or None


def parent_links(entries: Iterable[Session]) -> dict[str, str]:
    entries = list(entries)
    candidates: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for entry in entries:
        candidates[entry.key].append(entry)
    links = {}
    for entry in entries:
        parents = candidates.get((entry.source, lineage_parent(entry)), [])
        if len(parents) == 1 and row_id(parents[0]) != row_id(entry):
            links[row_id(entry)] = row_id(parents[0])
    for start in list(links):
        order: list[str] = []
        seen: dict[str, int] = {}
        node = start
        while node in links:
            if node in seen:
                for cycle_node in order[seen[node] :]:
                    links.pop(cycle_node, None)
                break
            seen[node] = len(order)
            order.append(node)
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


def session_subtree(entries: Iterable[Session], root: Session) -> list[Session]:
    """Return one complete lineage, descendants first so its root changes last."""
    unique = {row_id(entry): entry for entry in entries}
    root_id = row_id(root)
    if root_id not in unique:
        return [root]
    children: dict[str, list[str]] = defaultdict(list)
    for child, parent in parent_links(unique.values()).items():
        children[parent].append(child)
    result: list[Session] = []

    def visit(identifier: str) -> None:
        for child in children.get(identifier, []):
            visit(child)
        result.append(unique[identifier])

    visit(root_id)
    return result


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

    A folder or source view is not a conversation boundary. The added sessions
    are structural context only; callers keep ``entries`` for view membership.
    """
    candidate_lists: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for entry in candidates:
        candidate_lists[entry.key].append(entry)
    # Session IDs are unique for Codex, but Claude can emit several transcript
    # files for one session.  Only a unique ID is safe to use as a parent link.
    known = {
        key: values[0] for key, values in candidate_lists.items() if len(values) == 1
    }
    result = {entry.storage_key: entry for entry in entries}
    pending = list(entries)
    while pending:
        entry = pending.pop()
        parent_id = lineage_parent(entry)
        if not parent_id:
            continue
        parent = known.get((entry.source, parent_id))
        key = parent.storage_key if parent else None
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

    flat_modes: set[tuple[str, str, str, str, str]]
    tree_folds: dict[tuple[str, str, str, str, str], set[str]]
    detail_key: tuple[str, str, str, str, str] | None = None
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
        type_filter: str,
        cwd: Path,
        entries: Iterable[Session],
        *,
        tree_by_default: bool = True,
    ) -> tuple[bool, set[str]]:
        key = (mode, name, source_filter, type_filter, str(cwd))
        self.detail_key = key
        if key not in self.tree_folds:
            self.tree_folds[key] = folded_tree_nodes(entries)
            if not tree_by_default:
                self.flat_modes.add(key)
        enabled = key not in self.flat_modes
        folds = self.tree_folds[key] if enabled else set()
        return enabled, folds

    def toggle_tree(self, entries: Iterable[Session]) -> tuple[bool, set[str]]:
        if self.detail_key is None:
            return False, set()
        if self.detail_key in self.flat_modes:
            self.flat_modes.remove(self.detail_key)
            return True, self.tree_folds.setdefault(
                self.detail_key, folded_tree_nodes(entries)
            )
        self.flat_modes.add(self.detail_key)
        return False, set()

    def close_group(self) -> None:
        self.detail_key = None


def find_match(labels: list[str], query: str, start: int, step: int = 1) -> int:
    if query:
        for distance in range(1, len(labels) + 1):
            index = (start + step * distance) % len(labels)
            if query.casefold() in labels[index].casefold():
                return index
    return start


def search_sessions(sessions: Iterable[Session], query: str) -> list[Session]:
    """Search the complete scan by title, folder, or native ID prefix."""
    needle = query.strip().casefold()
    if not needle:
        return []
    return [
        session
        for session in sessions
        if needle
        in f"{session_label(session)}\n{session.cwd}\n{session.session_id}".casefold()
    ]


def origin_label(origin: str) -> str:
    return {
        "unknown": "?",  # Diagnostic marker while provenance coverage grows.
        "primary": "main",
        "subagent": "child",
        "sidechain": "child",
        "automation": "auto",
        "user": "user",
        "review": "review",
        "ide": "ide",
    }.get(origin, origin)
