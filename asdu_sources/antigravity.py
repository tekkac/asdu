"""Read-only discovery for Google Antigravity conversation stores."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from asdu_sessions import (
    BriefData,
    ScanReporter,
    Session,
    SessionCommand,
    SessionControls,
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


def load_brief(session: Session, poll=None, preview: bool = False) -> BriefData:
    del poll, preview
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
    return BriefData(
        None,
        None,
        None,
        summary.preview or None,
        Counter({"step": steps}) if steps else Counter(),
        [summary.agent_name] if summary.agent_name else [],
        session.task_path,
        session.forked_from,
        activity_summary=f"{steps:,} steps recorded.",
    )


def session_controls(session: Session) -> SessionControls:
    if not CONVERSATION_ID.fullmatch(session.session_id):
        return SessionControls()
    return SessionControls(
        (SessionCommand("Resume", ("agy", "--conversation", session.session_id)),)
    )
