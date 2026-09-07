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
from dataclasses import field as dataclass_field
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
    providers: list[str] = dataclass_field(default_factory=list)
    latest_model: str = ""


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

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.session_id

    @property
    def storage_key(self) -> str:
        """Opaque browser identity; distinct files may share a native session ID."""
        return f"{self.source}:{self.path}"

    @property
    def actions(self) -> frozenset[str]:
        from asdu_sources import ACTIONS

        return ACTIONS.get(self.source, frozenset())


class ContentCache:
    """Cache source-aware user-message keyword results outside session stores."""

    EXTRACTOR_VERSION = 4

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
    from asdu_sources import READERS

    return READERS[source].assistant_texts(item)


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


def user_texts(source: str, item: dict) -> Iterable[str]:
    from asdu_sources import READERS

    return READERS[source].user_texts(item)


def transcript_keywords(
    path: Path,
    source: str,
    keywords: set[str],
    report_bytes: Callable[[int], None] | None = None,
) -> set[str]:
    """Mine only source-recognized user messages, streaming one JSONL file."""
    from asdu_sources import READERS

    read_users = READERS[source].user_texts
    clean_user_text = READERS[source].clean_user_text
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
                for text in read_users(item):
                    clean = clean_user_text(text).casefold()
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


@dataclass(frozen=True)
class SourceAdapter:
    """One local transcript format and the reader that understands it."""

    root: Path
    scan: Callable[..., list[Session]]


def source_adapters(
    codex_root: Path, claude_root: Path, omp_root: Path | None = None
) -> dict[str, SourceAdapter]:
    from asdu_sources import READERS

    roots = {"codex": codex_root, "claude": claude_root}
    if omp_root is not None:
        roots["omp"] = omp_root
    return {
        name: SourceAdapter(root, READERS[name].discover)
        for name, root in roots.items()
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


def require_action(session: Session, action: str) -> None:
    if action not in session.actions:
        raise OSError(f"{session.source} sessions are read-only for {action}")


def trash_session(session: Session) -> None:
    require_action(session, "trash")
    require_unchanged(session)
    move_to_trash(session.path)


def archive_session(session: Session) -> Path:
    """Compress a transcript into a dated, user-owned archive and remove it."""
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    require_action(session, "archive")
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


def read_brief(session: Session, poll=None, preview: bool = False) -> BriefData:
    from asdu_sources import READERS

    return READERS[session.source].load_brief(session, poll, preview)


def read_jsonl_brief(
    session: Session, poll=None, preview=False, enrich=None
) -> BriefData:
    """Shared streaming and preview mechanics; formats belong to source readers."""
    from asdu_sources import READERS

    reader = READERS[session.source]
    data = BriefData(
        None, None, None, None, Counter(), [], session.task_path, session.forked_from
    )
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
            data.event_counts[str(item.get("type", "unknown"))] += 1
            if enrich is not None:
                enrich(item, data)
            for text in reader.user_texts(item):
                clean = reader.clean_user_text(text)
                if clean:
                    data.first_user = data.first_user or clean
                    data.latest_user = clean
            for text in reader.assistant_texts(item):
                data.latest_reply = text
    return data
