"""Local transcript readers, deterministic tags, and explicit storage actions."""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import shlex
import shutil
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Protocol

DEFAULT_TAGS = [
    {
        "name": "research",
        "keywords": ["literature review", "research question", "proof sketch", "arxiv"],
    },
    {
        "name": "tooling",
        "keywords": ["mcp server", "agent skill", "plugin", "command line tool"],
    },
    {
        "name": "operations",
        "keywords": ["deployment", "docker", "kubernetes", "terraform"],
    },
    {
        "name": "security",
        "keywords": [
            "security audit",
            "vulnerability",
            "threat model",
            "cryptographic",
        ],
    },
    {
        "name": "development",
        "keywords": ["implement", "test failure", "debug", "code review"],
    },
    {
        "name": "data",
        "keywords": [
            "sql query",
            "data pipeline",
            "dataset",
            "jupyter notebook",
            "dataframe",
        ],
    },
    {
        "name": "documentation",
        "keywords": [
            "write documentation",
            "update readme",
            "release notes",
            "documentation guide",
            "api reference",
        ],
    },
]


class ScanReporter(Protocol):
    skipped: set[Path]
    invalid: set[Path]

    def update(
        self,
        source: str,
        done: int,
        total: int,
        done_bytes: int = 0,
        total_bytes: int = 0,
    ) -> None: ...


@dataclass
class BriefData:
    first_user: str | None
    latest_user: str | None
    latest_reply: str | None
    first_objective: str | None
    event_counts: Counter[str]
    recorded_via: list[str]
    task_path: str
    forked_from: str


@dataclass(frozen=True)
class TagRule:
    name: str
    paths: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Session:
    path: Path
    size: int
    modified: float
    source: str
    origin: str
    cwd: str
    session_id: str
    parent_id: str | None
    title: str
    tags: tuple[str, ...]
    task_path: str = ""
    forked_from: str = ""


class ContentCache:
    """Cache source-aware user-message keyword results outside session stores."""

    EXTRACTOR_VERSION = 3

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.path = cache_home / "asdu" / "content-keywords-v3.json"
        self.entries: dict[str, dict[str, object]] = {}
        if not enabled:
            return
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            self.entries = data.get("entries", {}) if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass

    def get(self, path: Path, source: str, keywords: set[str]) -> set[str] | None:
        if not self.enabled:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        entry = self.entries.get(str(path))
        if (
            not isinstance(entry, dict)
            or entry.get("size") != stat.st_size
            or entry.get("mtime_ns") != stat.st_mtime_ns
        ):
            return None
        if (
            entry.get("source") != source
            or entry.get("extractor_version") != self.EXTRACTOR_VERSION
            or entry.get("keywords") != sorted(keywords)
            or not isinstance(entry.get("matches"), list)
        ):
            return None
        return {value for value in entry["matches"] if isinstance(value, str)}

    def put(
        self, path: Path, source: str, keywords: set[str], matches: set[str]
    ) -> None:
        if not self.enabled:
            return
        try:
            stat = path.stat()
        except OSError:
            return
        self.entries[str(path)] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "source": source,
            "extractor_version": self.EXTRACTOR_VERSION,
            "keywords": sorted(keywords),
            "matches": sorted(matches),
        }

    def save(self) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"entries": self.entries}, handle, separators=(",", ":"))
            temporary.replace(self.path)
        except OSError:
            pass


def session_label(session: Session) -> str:
    """Keep an untitled marker for briefs, not the space-constrained list."""
    if session.task_path:
        return session.task_path.rstrip("/").rsplit("/", 1)[-1].replace("_", " ")
    return re.sub(r"^untitled\s+[—-]\s*", "", session.title, count=1) or session.title


def load_tag_rules(config: Path | None) -> list[TagRule]:
    # Built-ins are small, portable, and high-confidence.  A TOML file
    # deliberately replaces them with stable, user-owned rules.
    raw_rules: list[dict] = DEFAULT_TAGS
    if config is not None:
        with config.open("rb") as handle:
            data = tomllib.load(handle)
        raw_rules = data.get("tag", [])
        if not isinstance(raw_rules, list):
            raise ValueError("config key 'tag' must be an array of tables")

    rules: list[TagRule] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            raise ValueError("each tag must be a table")
        for field in ("paths", "keywords"):
            values = item.get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise ValueError(f"tag {field} must be a list of strings")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError("every tag needs a non-empty name")
        rules.append(
            TagRule(
                name=name,
                paths=tuple(str(value).casefold() for value in item.get("paths", [])),
                keywords=tuple(
                    str(value).casefold() for value in item.get("keywords", [])
                ),
            )
        )
    return rules


def load_titles(codex_home: Path) -> dict[str, str]:
    index = codex_home / "session_index.jsonl"
    titles: dict[str, str] = {}
    for item in iter_jsonl(index):
        session_id = item.get("id")
        title = item.get("thread_name")
        if isinstance(session_id, str) and isinstance(title, str) and title.strip():
            titles[session_id] = title.strip()
    return titles


def iter_jsonl(
    path: Path, limit: int | None = None, progress: ScanReporter | None = None
) -> Iterable[dict[str, object]]:
    """Yield valid JSON object records; changing transcripts remain harmless."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if limit is not None and index >= limit:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    if progress is not None:
                        progress.invalid.add(path)
                    continue
                if isinstance(item, dict):
                    yield item
                elif progress is not None:
                    progress.invalid.add(path)
    except OSError:
        return


def task_metadata(payload: dict) -> tuple[str, str]:
    """Use explicit spawn/fork fields, never infer a task from inherited text."""
    spawn = payload.get("source")
    for key in ("subagent", "thread_spawn"):
        spawn = spawn.get(key) if isinstance(spawn, dict) else None
    task = spawn.get("agent_path") if isinstance(spawn, dict) else None
    fork = payload.get("forked_from_id")
    return (
        task if isinstance(task, str) and task.strip("/") else "",
        fork if isinstance(fork, str) else "",
    )


def read_metadata(
    path: Path, details: dict | None = None, progress: ScanReporter | None = None
) -> tuple[str, str, str, str | None]:
    """Read only the initial metadata records, not a whole transcript."""
    for item in iter_jsonl(path, 32, progress):
        if item.get("type") != "session_meta":
            continue
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            break
        if details is not None:
            details.update(payload)
        cwd = payload.get("cwd")
        session_id = payload.get("id") or payload.get("session_id")
        parent_id = payload.get("parent_thread_id")
        thread_source = payload.get("thread_source")
        source = payload.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        if not isinstance(parent_id, str) or not parent_id:
            parent_id = (
                spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
            )
        # Relationship and launcher are separate: an IDE can launch a child.
        if thread_source == "guardian_review" or (
            isinstance(subagent, dict) and subagent.get("other") == "guardian"
        ):
            origin = "review"
        elif (
            parent_id
            or thread_source == "subagent"
            or (isinstance(source, dict) and "subagent" in source)
        ):
            origin = "subagent"
        elif thread_source == "user" or source in ("cli", "vscode", "exec"):
            origin = "primary"
        else:
            origin = "unknown"
        return (
            cwd if isinstance(cwd, str) else "(unknown)",
            session_id if isinstance(session_id, str) else path.stem,
            origin,
            parent_id if isinstance(parent_id, str) else None,
        )
    return "(unknown)", path.stem, "unknown", None


def derive_title(path: Path) -> str:
    """Use the first real user request when session_index lacks a title.

    Codex serializes environment, skill, and AGENTS.md material as user-role
    messages too, so those preambles are deliberately skipped.  The bounded
    scan keeps an inventory fast even when a rollout is hundreds of megabytes.
    """
    fallback = ""
    for item in iter_jsonl(path, 4096):
        for text in user_texts("codex", item):
            title = substantive_user_text(text)
            if title:
                return title
        for text in assistant_texts("codex", item):
            fallback = text
    return f"reply: {fallback}" if fallback else "untitled"


def untitled_title(first_request: str) -> str:
    """Label a generated preview without pretending the session was titled."""
    if first_request == "untitled":
        return first_request
    return f"untitled — {first_request[:1024]}"


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict)
        )
    return ""


def message_texts(payload: dict) -> list[str]:
    content = payload.get("content", [])
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {
            "input_text",
            "output_text",
            "text",
        }:
            continue
        text = block.get("text")
        if isinstance(text, str):
            clean = " ".join(text.split())
            if clean:
                texts.append(clean)
    return texts


def assistant_texts(source: str, item: dict) -> list[str]:
    """Read assistant prose for both title fallbacks and briefs, excluding tools."""
    if source == "codex" and item.get("type") == "response_item":
        message = item.get("payload")
    elif source == "claude":
        message = item.get("message")
    else:
        return []
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    content = message.get("content")
    if source == "claude" and isinstance(content, str):
        clean = " ".join(content.split())
        return [clean] if clean else []
    return message_texts(message)


def is_real_user_text(text: str) -> bool:
    lowered = text.lower()
    ignored = (
        "<",
        "# agents",
        "here is a list of plugins",
        "you are codex",
        "each workspace has a .context",
        "do not rename the current branch",
        "by default, the user will only see",
        "respond directly to the user's prompt",
    )
    return len(text) >= 12 and not lowered.startswith(ignored)


def substantive_user_text(text: str) -> str:
    """Remove common agent-environment wrappers before using a user request."""
    text = re.sub(
        r"<system_instruction>.*?</system_instruction>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    clean = " ".join(text.split())
    return clean if is_real_user_text(clean) else ""


def preview_transcript(path: Path) -> io.StringIO:
    """Sample complete records at both ends; never read an unbounded JSONL line."""
    limit = 128 * 1024
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(0)
        head = handle.read(limit)
        if size <= limit:
            return io.StringIO(head.decode("utf-8", "replace"))
        head = head.rsplit(b"\n", 1)[0] if b"\n" in head else b""
        handle.seek(max(limit, size - limit))
        tail = handle.read(limit).split(b"\n", 1)[1:]
    return io.StringIO((head + b"\n" + b"".join(tail)).decode("utf-8", "replace"))


def resume_command(session: Session) -> str | None:
    """Return a copyable native resume command; never launch another agent."""
    command = {"codex": "codex resume", "claude": "claude --resume"}.get(session.source)
    if command is None:
        return None
    return f"{command} {shlex.quote(session.session_id)}"


def user_texts(source: str, item: dict[str, object]) -> Iterable[str]:
    """Emit searchable user text, never metadata, tools, or injected context."""
    if source == "codex":
        if item.get("type") == "event_msg":
            payload = item.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "user_message":
                message = payload.get("message")
                if isinstance(message, str):
                    yield message
        elif item.get("type") == "response_item":
            payload = item.get("payload")
            if isinstance(payload, dict) and payload.get("role") == "user":
                yield from message_texts(payload)
        return

    if source == "claude":
        if item.get("isMeta") is True:
            return
        message = item.get("message")
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                yield content
            elif isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ):
                        yield block["text"]
        elif (
            item.get("type") == "queue-operation" and item.get("operation") == "enqueue"
        ):
            content = item.get("content")
            if isinstance(content, str):
                yield content
        return


def transcript_keywords(
    path: Path,
    source: str,
    keywords: set[str],
    report_bytes: Callable[[int], None] | None = None,
) -> set[str]:
    """Mine only source-recognized user messages, streaming one JSONL file."""
    remaining = {keyword.casefold() for keyword in keywords if keyword}
    found: set[str] = set()
    if not remaining:
        return found
    try:
        with path.open("rb") as handle:
            scanned, next_report = 0, 1024 * 1024
            for raw_line in handle:
                scanned += len(raw_line)
                if report_bytes is not None and scanned >= next_report:
                    report_bytes(scanned)
                    next_report = scanned + 1024 * 1024
                line = raw_line.decode("utf-8", errors="replace")
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                for text in user_texts(source, item):
                    clean = substantive_user_text(text).casefold()
                    matched = {keyword for keyword in remaining if keyword in clean}
                    found.update(matched)
                    remaining.difference_update(matched)
                if not remaining:
                    break
            if report_bytes is not None:
                report_bytes(scanned)
    except OSError:
        pass
    return found


def cached_transcript_keywords(
    path: Path,
    source: str,
    keywords: set[str],
    cache: ContentCache,
    report_bytes: Callable[[int], None] | None = None,
) -> set[str]:
    cached = cache.get(path, source, keywords)
    if cached is not None:
        return cached
    matches = transcript_keywords(path, source, keywords, report_bytes)
    cache.put(path, source, keywords, matches)
    return matches


def classify(
    session: Session, rules: list[TagRule], content_matches: set[str]
) -> tuple[str, ...]:
    haystack = f"{session.cwd} {session.title}".casefold()
    tags: list[str] = []
    for rule in rules:
        path_match = any(
            value.casefold() in session.cwd.casefold() for value in rule.paths
        )
        keyword_match = any(value.casefold() in haystack for value in rule.keywords)
        if not keyword_match:
            keyword_match = any(
                value.casefold() in content_matches for value in rule.keywords
            )
        if path_match or keyword_match:
            tags.append(rule.name)
    return tuple(tags) or ("untagged",)


def scan_paths(
    source: str,
    label: str,
    paths: Iterable[Path],
    inspect: Callable[[Path], Session | None],
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanReporter,
    cache: ContentCache,
    scope: Path | None,
) -> list[Session]:
    """Apply the same scope, cache, progress, and classification rules to every source."""
    discovered = list(paths)
    keywords = {keyword for rule in rules for keyword in rule.keywords}
    sizes: list[int] = []
    for path in discovered:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            sizes.append(0)
    total_bytes = sum(sizes)
    sessions: list[Session] = []
    done_bytes = 0
    for index, (path, size) in enumerate(zip(discovered, sizes), 1):
        session = inspect(path)
        done_bytes += size
        if session is None:
            progress.skipped.add(path)
            progress.update(label, index, len(discovered), done_bytes, total_bytes)
            continue
        if not in_scope(session, scope):
            if session.cwd == "(unknown)":
                progress.skipped.add(path)
            progress.update(label, index, len(discovered), done_bytes, total_bytes)
            continue
        matches = (
            cached_transcript_keywords(
                path,
                source,
                keywords,
                cache,
                lambda scanned: progress.update(
                    label,
                    index,
                    len(discovered),
                    done_bytes - size + scanned,
                    total_bytes,
                ),
            )
            if content_keywords
            else set()
        )
        sessions.append(replace(session, tags=classify(session, rules, matches)))
        progress.update(label, index, len(discovered), done_bytes, total_bytes)
    return sessions


def scan_codex(
    root: Path,
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanReporter,
    cache: ContentCache,
    scope: Path | None,
) -> list[Session]:
    # The normal root is ~/.codex/sessions, whose direct parent owns
    # session_index.jsonl. Custom roots simply use their direct parent too.
    titles = load_titles(root.parent)

    def inspect(path: Path) -> Session | None:
        try:
            stat = path.stat()
            size, modified = stat.st_size, stat.st_mtime
            details: dict = {}
            cwd, session_id, origin, parent_id = read_metadata(path, details, progress)
        except OSError:
            return None
        if not details:
            return None
        session = Session(
            path,
            size,
            modified,
            "codex",
            origin,
            cwd,
            session_id,
            parent_id,
            "",
            (),
            *task_metadata(details),
        )
        if not in_scope(session, scope):
            return session
        title = (
            session_label(session)
            if session.task_path
            else titles.get(session_id) or untitled_title(derive_title(path))
        )
        return replace(session, title=title)

    return scan_paths(
        "codex",
        "Codex",
        root.rglob("rollout-*.jsonl"),
        inspect,
        rules,
        content_keywords,
        progress,
        cache,
        scope,
    )


def scan_claude(
    root: Path,
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanReporter,
    cache: ContentCache,
    scope: Path | None,
) -> list[Session]:
    """Small native Claude reader: CWD + session id live in normal JSONL events."""

    def inspect(path: Path) -> Session | None:
        cwd, session_id, origin, parent_id = "(unknown)", path.stem, "unknown", None
        title = "untitled"
        fallback = ""
        recognized = False
        for item in iter_jsonl(path, 4096, progress):
            if isinstance(item.get("cwd"), str):
                cwd = item["cwd"]
            if isinstance(item.get("sessionId"), str):
                session_id = item["sessionId"]
                recognized = True
            if item.get("isSidechain") is False and origin != "sidechain":
                origin = "primary"
            if item.get("isSidechain") is True:
                # Claude exposes message parents, not a parent session.
                origin = "sidechain"
            candidate = (
                next(iter(user_texts("claude", item)), "")
                if title == "untitled"
                else ""
            )
            for text in assistant_texts("claude", item):
                fallback = text
            if candidate:
                clean = substantive_user_text(candidate)
                if clean:
                    title = clean
            if cwd != "(unknown)" and session_id != path.stem and title != "untitled":
                break
        if not recognized and cwd == "(unknown)":
            return None
        try:
            stat = path.stat()
            size, modified = stat.st_size, stat.st_mtime
        except OSError:
            return None
        title = untitled_title(
            title
            if title != "untitled"
            else f"reply: {fallback}"
            if fallback
            else title
        )
        return Session(
            path,
            size,
            modified,
            "claude",
            origin,
            cwd,
            session_id,
            parent_id,
            title,
            (),
        )

    return scan_paths(
        "claude",
        "Claude",
        root.rglob("*.jsonl"),
        inspect,
        rules,
        content_keywords,
        progress,
        cache,
        scope,
    )


@dataclass(frozen=True)
class SourceAdapter:
    """One local transcript format and the reader that understands it."""

    root: Path
    scan: Callable[..., list[Session]]


def source_adapters(codex_root: Path, claude_root: Path) -> dict[str, SourceAdapter]:
    return {
        "codex": SourceAdapter(codex_root, scan_codex),
        "claude": SourceAdapter(claude_root, scan_claude),
    }


def scan(
    sources: tuple[str, ...],
    adapters: dict[str, SourceAdapter],
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanReporter,
    cache: ContentCache,
    scope: Path | None,
) -> list[Session]:
    sessions: list[Session] = []
    for source in sources:
        adapter = adapters[source]
        if adapter.root.exists():
            sessions.extend(
                adapter.scan(
                    adapter.root, rules, content_keywords, progress, cache, scope
                )
            )
    cache.save()
    return sessions


def in_scope(session: Session, scope: Path | None) -> bool:
    if scope is None:
        return True
    if session.cwd == "(unknown)":
        return False
    try:
        Path(session.cwd).resolve().relative_to(scope)
        return True
    except ValueError:
        return False


def move_to_trash(path: Path) -> None:
    """Move one transcript to the operating system's recoverable Trash."""
    try:
        from send2trash import send2trash
    except ImportError as error:
        raise OSError("send2trash is required for recoverable deletion") from error
    send2trash(str(path))


def require_unchanged(session: Session) -> None:
    """Refuse actions when a transcript changed since the browser scanned it."""
    try:
        current = session.path.stat()
    except OSError as error:
        raise OSError("session is no longer readable") from error
    if current.st_size != session.size or current.st_mtime != session.modified:
        raise OSError("session changed since scan; rescan before acting")


def archive_session(session: Session) -> Path:
    """Compress a transcript into a dated, user-owned archive and remove it."""
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    if session.source not in {"codex", "claude"}:
        raise OSError("Unsupported archive source")
    directory = data_home / "asdu" / "archive" / day / session.source
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="session-", suffix=".jsonl.gz", dir=directory)
    os.close(fd)
    destination = Path(name)
    try:
        require_unchanged(session)
        with (
            session.path.open("rb") as source,
            gzip.open(destination, "wb") as archived,
        ):
            shutil.copyfileobj(source, archived, length=1024 * 1024)
        require_unchanged(session)
        session.path.unlink()
        return destination
    except (OSError, KeyboardInterrupt):
        try:
            if session.path.exists():
                destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def action_log_path() -> Path:
    """Return the local, append-only record of successful storage actions."""
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    )
    return state_home / "asdu" / "actions.jsonl"


def record_action(
    action: str, session: Session, destination: Path | None = None
) -> None:
    """Record only action metadata, never transcript content, outside the UI."""
    path = action_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "action": action,
        "source": session.source,
        "session_id": session.session_id,
        "path": str(session.path),
        "size": session.size,
    }
    if destination is not None:
        event["archive"] = str(destination)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, separators=(",", ":")) + "\n")


def delete_settings_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "asdu" / "settings.json"


def skip_delete_confirmation() -> bool:
    try:
        with delete_settings_path().open(encoding="utf-8") as handle:
            return bool(json.load(handle).get("skip_delete_confirmation"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def disable_delete_confirmation() -> None:
    try:
        path = delete_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump({"skip_delete_confirmation": True}, handle)
    except OSError:
        pass


def read_brief(
    session: Session, poll: Callable[[], None] | None = None, preview: bool = False
) -> BriefData:
    """Read brief data without terminal formatting or model calls."""
    first_user: str | None = None
    latest_user: str | None = None
    latest_reply: str | None = None
    first_objective: str | None = None
    event_counts: Counter[str] = Counter()
    recorded_via: list[str] = []
    task_path, forked_from = session.task_path, session.forked_from

    def remember_user(text: str) -> None:
        nonlocal first_user, latest_user
        if not text:
            return
        first_user = first_user or text
        latest_user = text

    with (
        preview_transcript(session.path)
        if preview
        else session.path.open(encoding="utf-8", errors="replace")
    ) as handle:
        for index, line in enumerate(handle):
            if poll is not None and index % 128 == 0:
                poll()
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            event_counts[str(item.get("type", "unknown"))] += 1
            if session.source == "codex" and item.get("type") == "session_meta":
                payload = item.get("payload")
                if isinstance(payload, dict):
                    task_path, forked_from = task_metadata(payload)
                    source = payload.get("source")
                    originator = payload.get("originator")
                    provider = payload.get("model_provider")
                    if source == "vscode" or originator == "codex_vscode":
                        recorded_via.append("VS Code")
                    elif source == "exec":
                        recorded_via.append("Codex Exec")
                    if originator == "codex_sdk_ts":
                        recorded_via.append("Codex SDK (TypeScript)")
                    elif originator == "codex_exec":
                        recorded_via.append("Codex Exec")
                    if isinstance(provider, str) and provider:
                        recorded_via.append(f"provider: {provider}")
            elif session.source == "claude":
                entrypoint = item.get("entrypoint")
                if entrypoint == "claude-vscode":
                    recorded_via.append("VS Code")
                elif isinstance(entrypoint, str) and entrypoint:
                    recorded_via.append(entrypoint)
            if item.get("type") == "event_msg":
                payload = item.get("payload")
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "thread_goal_updated"
                ):
                    goal = payload.get("goal")
                    if isinstance(goal, dict) and isinstance(
                        goal.get("objective"), str
                    ):
                        first_objective = first_objective or " ".join(
                            goal["objective"].split()
                        )
            for text in user_texts(session.source, item):
                remember_user(substantive_user_text(text))
            for text in assistant_texts(session.source, item):
                latest_reply = text

    return BriefData(
        first_user,
        latest_user,
        latest_reply,
        first_objective,
        event_counts,
        recorded_via,
        task_path,
        forked_from,
    )
