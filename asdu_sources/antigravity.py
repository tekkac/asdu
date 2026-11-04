"""Antigravity conversation discovery and guarded storage actions."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from asdu_sessions import (
    ActionResult,
    BriefData,
    ScanReporter,
    Session,
    SessionCommand,
    SessionControls,
    open_transcript,
    preview_transcript,
    substantive_user_text,
    untitled_title,
)

CONVERSATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
SUMMARY_COLUMNS = {
    "conversation_id",
    "title",
    "preview",
    "step_count",
    "last_modified_time",
    "workspace_uris",
    "parent_conversation_id",
    "agent_name",
}
CONVERSATION_TABLES = {"steps", "trajectory_meta"}
DELETE_COLUMNS = {"conversation_id", "last_modified_time", "not_fully_idle"}
USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


@dataclass(frozen=True)
class Summary:
    title: str = ""
    preview: str = ""
    step_count: int = 0
    modified: float = 0
    cwd: str = ""
    parent_id: str = ""
    agent_name: str = ""


@contextmanager
def connect(path: Path):
    """Open SQLite without locks, journals, migrations, or sidecar writes."""
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    database = sqlite3.connect(uri, uri=True, timeout=1)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only = ON")
    try:
        yield database
    finally:
        database.close()


def columns(database: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in database.execute(f'PRAGMA table_info("{table}")')}


def parse_time(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0


def nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def workspace_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        candidates = json.loads(value)
    except json.JSONDecodeError:
        candidates = [value]
    if not isinstance(candidates, list):
        return ""
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme != "file":
            return candidate if not parsed.scheme else ""
        path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc != "localhost":
            path = f"//{parsed.netloc}{path}"
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return path
    return ""


def explicit_user_text(content: str) -> str:
    if match := USER_REQUEST.search(content):
        content = match.group(1)
    return substantive_user_text(" ".join(content.split()))


def read_summaries(path: Path) -> dict[str, Summary]:
    if not path.is_file():
        return {}
    with connect(path) as database:
        missing = SUMMARY_COLUMNS - columns(database, "conversation_summaries")
        if missing:
            raise sqlite3.DatabaseError(
                "unsupported Antigravity summary database: missing "
                + ", ".join(sorted(missing))
            )
        rows = database.execute(
            """
            SELECT conversation_id, title, preview, step_count,
                   last_modified_time, workspace_uris,
                   parent_conversation_id, agent_name
            FROM conversation_summaries
            """
        )
        return {
            row["conversation_id"]: Summary(
                str(row["title"] or "").strip(),
                str(row["preview"] or "").strip(),
                nonnegative_int(row["step_count"]),
                parse_time(row["last_modified_time"]),
                workspace_path(row["workspace_uris"]),
                str(row["parent_conversation_id"] or "").strip(),
                str(row["agent_name"] or "").strip(),
            )
            for row in rows
            if isinstance(row["conversation_id"], str)
        }


def recent_workspaces(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        identifier: cwd
        for cwd, identifier in payload.items()
        if isinstance(cwd, str) and isinstance(identifier, str)
    }


def require_conversation_schema(path: Path) -> None:
    with connect(path) as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        missing = CONVERSATION_TABLES - tables
        if missing:
            raise sqlite3.DatabaseError(
                "unsupported Antigravity conversation database: missing "
                + ", ".join(sorted(missing))
            )


def owned_stats(root: Path, database: Path) -> tuple[int, float]:
    """Count one database, its sidecars, and its matching brain directory once."""
    paths = [database]
    paths.extend(
        candidate
        for suffix in ("-wal", "-shm")
        if (candidate := database.with_name(database.name + suffix)).is_file()
    )
    brain = root / "brain" / database.stem
    if brain.is_dir():
        for directory, _, filenames in os.walk(brain):
            paths.extend(Path(directory) / name for name in filenames)
    size = 0
    try:
        modified = database.stat().st_mtime
    except OSError:
        modified = 0.0
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        size += stat.st_size
    return size, modified


def discover(root: Path, progress: ScanReporter) -> list[Session]:
    progress.update("Antigravity", 0, 1, 0, 0)
    conversations = root / "conversations"
    paths = sorted(conversations.glob("*.db")) if conversations.is_dir() else []
    try:
        summaries = read_summaries(root / "conversation_summaries.db")
    except (OSError, sqlite3.Error):
        progress.invalid.add(root / "conversation_summaries.db")
        summaries = {}
    workspaces = recent_workspaces(root / "cache" / "last_conversations.json")
    stats = {path: owned_stats(root, path) for path in paths}
    total = sum(size for size, _ in stats.values())
    done = 0
    sessions: list[Session] = []
    for index, path in enumerate(paths, 1):
        size, file_modified = stats[path]
        done += size
        identifier = path.stem
        if not CONVERSATION_ID.fullmatch(identifier):
            progress.skipped.add(path)
            progress.update("Antigravity", index, len(paths), done, total)
            continue
        try:
            require_conversation_schema(path)
        except (OSError, sqlite3.Error):
            progress.invalid.add(path)
            progress.update("Antigravity", index, len(paths), done, total)
            continue
        summary = summaries.get(identifier, Summary())
        fallback = summary.preview or "untitled"
        title = summary.title or untitled_title(fallback)
        parent = summary.parent_id or None
        sessions.append(
            Session(
                path,
                size,
                max(file_modified, summary.modified),
                "agy",
                "subagent" if parent else "primary",
                summary.cwd or workspaces.get(identifier, "(unknown)"),
                identifier,
                parent,
                title,
                source_home=str(root),
            )
        )
        progress.update("Antigravity", index, len(paths), done, total)
    if not paths:
        progress.update("Antigravity", 0, 0, 0, 0)
    return sessions


def available_actions(session: Session) -> frozenset[str]:
    if session.source == "agy" and CONVERSATION_ID.fullmatch(session.session_id):
        return frozenset({"delete"})
    return frozenset()


def action_scope(_session: Session, action: str) -> str:
    return "selectable-tree" if action == "delete" else "single"


def delete_layout(session: Session) -> tuple[Path, Path, Path]:
    """Return the exact summary, conversation, and brain paths for one ID."""
    root = Path(session.source_home)
    if not CONVERSATION_ID.fullmatch(session.session_id):
        raise OSError("invalid Antigravity conversation ID; rescan before acting")
    conversation = root / "conversations" / f"{session.session_id}.db"
    brain = root / "brain" / session.session_id
    if session.path != conversation:
        raise OSError("Antigravity conversation path changed; rescan before acting")
    if (
        (root / "conversations").is_symlink()
        or (root / "brain").is_symlink()
        or conversation.is_symlink()
        or brain.is_symlink()
    ):
        raise OSError("refusing to delete a symlinked Antigravity conversation")
    return root / "conversation_summaries.db", conversation, brain


def read_delete_rows(sessions: list[Session]) -> dict[str, sqlite3.Row]:
    if not sessions:
        return {}
    roots = {session.source_home for session in sessions}
    if len(roots) != 1:
        raise OSError("Antigravity delete cannot cross storage roots")
    root = Path(next(iter(roots)))
    summary_path = root / "conversation_summaries.db"
    if not summary_path.is_file() or summary_path.is_symlink():
        raise OSError("Antigravity summary database is unavailable")
    try:
        database = sqlite3.connect(
            summary_path.absolute().as_uri() + "?mode=ro", uri=True, timeout=1
        )
        database.row_factory = sqlite3.Row
        missing = DELETE_COLUMNS - columns(database, "conversation_summaries")
        if missing:
            raise OSError(
                "Antigravity database cannot verify idle sessions; update "
                "Antigravity before deleting"
            )
        ids = tuple(dict.fromkeys(session.session_id for session in sessions))
        rows = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows.update(
                (row["conversation_id"], row)
                for row in database.execute(
                    "SELECT conversation_id, last_modified_time, not_fully_idle "
                    "FROM conversation_summaries "
                    f"WHERE conversation_id IN ({placeholders})",
                    chunk,
                )
            )
    except sqlite3.Error as error:
        raise OSError(f"could not verify Antigravity conversations: {error}") from error
    finally:
        if "database" in locals():
            database.close()
    return rows


def prepare_actions(sessions: list[Session], action: str) -> None:
    if action != "delete":
        raise OSError(f"Antigravity cannot {action} conversations")
    if any(session.source != "agy" for session in sessions):
        raise OSError("Antigravity delete cannot cross sources")
    rows = read_delete_rows(sessions)
    for session in sessions:
        _, conversation, _ = delete_layout(session)
        if session.source != "agy" or not conversation.is_file():
            raise OSError("Antigravity conversation is no longer present")
        row = rows.get(session.session_id)
        if row is None:
            raise OSError("Antigravity conversation is no longer indexed")
        try:
            active = int(row["not_fully_idle"] or 0)
        except (TypeError, ValueError) as error:
            raise OSError("Antigravity activity state is unreadable") from error
        if active:
            raise OSError("Antigravity session is active; close it before deleting")
        size, file_modified = owned_stats(Path(session.source_home), conversation)
        summary_modified = parse_time(row["last_modified_time"])
        if size != session.size or max(file_modified, summary_modified) != session.modified:
            raise OSError("Antigravity session changed since scan; rescan before acting")
        try:
            require_conversation_schema(conversation)
        except (OSError, sqlite3.Error) as error:
            raise OSError("Antigravity conversation database is unreadable") from error


def perform_action(session: Session, action: str) -> ActionResult:
    if action != "delete" or action not in available_actions(session):
        raise OSError(f"Antigravity cannot {action} this conversation")
    prepare_actions([session], action)
    summary_path, conversation, brain = delete_layout(session)
    try:
        with closing(sqlite3.connect(summary_path, timeout=5)) as database, database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT not_fully_idle, last_modified_time "
                "FROM conversation_summaries "
                "WHERE conversation_id = ?",
                (session.session_id,),
            ).fetchone()
            if row is None:
                raise OSError("Antigravity conversation is no longer indexed")
            try:
                active = int(row[0] or 0)
            except (TypeError, ValueError) as error:
                raise OSError("Antigravity activity state is unreadable") from error
            if active:
                raise OSError("Antigravity session is active; close it before deleting")
            size, file_modified = owned_stats(Path(session.source_home), conversation)
            if (
                size != session.size
                or max(file_modified, parse_time(row[1])) != session.modified
            ):
                raise OSError("Antigravity session changed since scan; rescan before acting")
            database.execute(
                "DELETE FROM conversation_summaries WHERE conversation_id = ?",
                (session.session_id,),
            )
    except sqlite3.Error as error:
        raise OSError(f"could not remove Antigravity summary: {error}") from error

    try:
        if brain.is_dir():
            shutil.rmtree(brain)
        for sidecar in (
            conversation.with_name(conversation.name + "-wal"),
            conversation.with_name(conversation.name + "-shm"),
        ):
            sidecar.unlink(missing_ok=True)
        conversation.unlink(missing_ok=True)
    except OSError as error:
        raise OSError(
            "Antigravity summary was removed, but some conversation files remain; "
            "rescan and retry"
        ) from error
    return ActionResult()


def load_brief(session: Session, poll=None, preview: bool = False) -> BriefData:
    try:
        summaries = read_summaries(
            Path(session.source_home) / "conversation_summaries.db"
        )
        summary = summaries.get(session.session_id, Summary())
        steps = summary.step_count
        if not steps:
            with connect(session.path) as database:
                steps = nonnegative_int(
                    database.execute("SELECT count(*) FROM steps").fetchone()[0]
                )
    except (OSError, sqlite3.Error) as error:
        raise OSError(f"could not read Antigravity session: {error}") from error
    data = BriefData(
        None,
        None,
        None,
        summary.preview or None,
        Counter({"step": steps}) if steps else Counter(),
        [summary.agent_name] if summary.agent_name else [],
        session.task_path,
        session.forked_from,
    )
    transcript = (
        Path(session.source_home)
        / "brain"
        / session.session_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    if not transcript.is_file():
        transcript = transcript.with_name("transcript_full.jsonl")
    if transcript.is_file():
        try:
            stream = (
                preview_transcript(transcript)
                if preview
                else open_transcript(transcript, "rt")
            )
            with stream:
                for index, line in enumerate(stream):
                    if poll is not None and index % 128 == 0:
                        poll()
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    content = record.get("content")
                    if not isinstance(content, str):
                        continue
                    text = " ".join(content.split())
                    source = record.get("source")
                    kind = record.get("type")
                    if source == "USER_EXPLICIT" and kind == "USER_INPUT":
                        text = explicit_user_text(content)
                        if text:
                            data.first_user = data.first_user or text
                            data.latest_user = text
                            data.user_messages += 1
                    elif source == "MODEL" and kind in {
                        "GENERIC",
                        "PLANNER_RESPONSE",
                    }:
                        if text:
                            data.latest_reply = text
                            data.assistant_messages += 1
        except OSError as error:
            raise OSError(f"could not read Antigravity transcript: {error}") from error
    if data.first_user:
        data.first_objective = None
    data.turns = data.user_messages
    if data.user_messages or data.assistant_messages:
        requests = "request" if data.user_messages == 1 else "requests"
        replies = "reply" if data.assistant_messages == 1 else "replies"
        data.activity_summary = (
            f"{steps:,} steps; {data.user_messages:,} {requests}, "
            f"{data.assistant_messages:,} {replies}."
        )
    else:
        data.activity_summary = f"{steps:,} steps recorded."
    return data


def session_controls(session: Session) -> SessionControls:
    if not CONVERSATION_ID.fullmatch(session.session_id):
        return SessionControls()
    return SessionControls(
        (SessionCommand("Resume", ("agy", "--conversation", session.session_id)),)
    )
