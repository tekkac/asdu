"""Local transcript readers and explicit storage actions."""

from __future__ import annotations

import io
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path
from typing import Protocol


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
    user_messages: int = 0
    assistant_messages: int = 0


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
    task_path: str = ""
    forked_from: str = ""
    archived: bool = False
    source_home: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.session_id

    @property
    def storage_key(self) -> str:
        """Opaque browser identity; distinct files may share a native session ID."""
        return f"{self.source}:{self.path}"


@dataclass(frozen=True)
class SessionCommand:
    """One source-native command relevant to a stored session."""

    label: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class SessionControls:
    """Lazy runtime state and native commands for one stored session."""

    commands: tuple[SessionCommand, ...] = ()
    runtime_id: str = ""
    runtime_kind: str = ""
    runtime_state: str = ""


@dataclass(frozen=True)
class ActionResult:
    destination: Path | None = None
    replacement: Session | None = None


def session_label(session: Session) -> str:
    """Keep an untitled marker for briefs, not the space-constrained list."""
    if session.task_path:
        return session.task_path.rstrip("/").rsplit("/", 1)[-1].replace("_", " ")
    return re.sub(r"^untitled\s+[—-]\s*", "", session.title, count=1) or session.title


def iter_jsonl(
    path: Path, limit: int | None = None, progress: ScanReporter | None = None
) -> Iterable[dict[str, object]]:
    """Yield valid JSON object records; changing transcripts remain harmless."""
    try:
        with open_transcript(path, "rt") as handle:
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
    """Label a generated preview, or use a compact unknown marker."""
    if first_request == "untitled":
        return "?"
    return f"untitled — {first_request[:1024]}"


def open_transcript(path: Path, mode: str):
    """Open one source-owned JSONL transcript."""
    return path.open(
        mode,
        **({"encoding": "utf-8", "errors": "replace"} if "t" in mode else {}),
    )


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
    return bool(text) and not lowered.startswith(ignored)


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
    with open_transcript(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(0)
        head = handle.read(limit)
        if size <= limit:
            return io.StringIO(head.decode("utf-8", "replace"))
        head = head.rsplit(b"\n", 1)[0] if b"\n" in head else b""
        handle.seek(max(limit, size - limit))
        tail = handle.read(limit).split(b"\n", 1)[1:]
    return io.StringIO((head + b"\n" + b"".join(tail)).decode("utf-8", "replace"))


def scan_paths(
    label: str,
    paths: Iterable[Path],
    inspect: Callable[[Path], Session | None],
    progress: ScanReporter,
) -> list[Session]:
    """Inspect paths while reporting bounded file and byte progress."""
    discovered = list(paths)
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
        sessions.append(session)
        progress.update(label, index, len(discovered), done_bytes, total_bytes)
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


def trash_session(session: Session) -> None:
    require_unchanged(session)
    move_to_trash(session.path)


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


def read_jsonl_brief(
    session: Session,
    read_users: Callable[[dict], Iterable[str]],
    read_assistant: Callable[[dict], Iterable[str]],
    clean_user_text: Callable[[str], str],
    poll=None,
    preview=False,
    enrich=None,
) -> BriefData:
    """Shared streaming and preview mechanics; formats belong to source readers."""
    data = BriefData(
        None, None, None, None, Counter(), [], session.task_path, session.forked_from
    )
    with (
        preview_transcript(session.path)
        if preview
        else open_transcript(session.path, "rt")
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
            found_user = False
            for text in read_users(item):
                clean = clean_user_text(text)
                if clean:
                    found_user = True
                    data.first_user = data.first_user or clean
                    data.latest_user = clean
            data.user_messages += found_user
            found_assistant = False
            for text in read_assistant(item):
                if text:
                    found_assistant = True
                    data.latest_reply = text
            data.assistant_messages += found_assistant
    return data
