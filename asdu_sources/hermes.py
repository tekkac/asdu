"""Read-only Hermes shared-database discovery and native resume guidance."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from asdu_sessions import (
    BriefData,
    ScanReporter,
    Session,
    SessionCommand,
    SessionControls,
    content_text,
    substantive_user_text,
    untitled_title,
)

REQUIRED_COLUMNS = {
    "sessions": {
        "id",
        "source",
        "model",
        "model_config",
        "parent_session_id",
        "started_at",
        "ended_at",
        "end_reason",
        "cwd",
        "git_repo_root",
        "billing_provider",
        "title",
        "last_activity_at",
        "archived",
    },
    "messages": {
        "id",
        "session_id",
        "role",
        "content",
        "timestamp",
        "active",
        "compacted",
        "_compressed_summary",
    },
}
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the live WAL-backed database without granting write access."""
    uri = path.resolve().as_uri() + "?mode=ro"
    database = sqlite3.connect(uri, uri=True, timeout=1)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only = ON")
    try:
        yield database
    finally:
        database.close()


def columns(database: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in database.execute(f'PRAGMA table_info("{table}")'))


def require_schema(database: sqlite3.Connection) -> None:
    for table, required in REQUIRED_COLUMNS.items():
        missing = required - set(columns(database, table))
        if missing:
            raise sqlite3.DatabaseError(
                f"unsupported Hermes database: {table} lacks "
                f"{', '.join(sorted(missing))}"
            )


def byte_sum(columns_: tuple[str, ...]) -> str:
    return " + ".join(
        f'coalesce(length(cast("{column}" as blob)), 0)' for column in columns_
    )


def logical_sizes(
    database: sqlite3.Connection, progress: ScanReporter | None = None
) -> dict[str, int]:
    """Estimate bytes owned by each session, excluding shared DB structures."""
    tables = [("sessions", "id", 64), ("messages", "session_id", 32)]
    if "session_id" in columns(database, "session_model_usage"):
        tables.append(("session_model_usage", "session_id", 32))
    specs = [(table, owner, columns(database, table), overhead) for table, owner, overhead in tables]
    total_records = sum(
        database.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        for table, _, _, _ in specs
    )
    sizes: dict[str, int] = {}
    processed = 0

    def report(force: bool = False) -> None:
        if progress is not None and (force or processed % 1024 == 0):
            progress.update(
                "Hermes", processed, total_records, processed, total_records, "records"
            )

    for table, owner, measured, overhead in specs:
        query = (
            f'SELECT "{owner}" AS owner, {byte_sum(measured)} + {overhead} AS bytes '
            f'FROM "{table}"'
        )
        for row in database.execute(query):
            identifier = row["owner"]
            if not isinstance(identifier, str) or not identifier:
                continue
            if table == "sessions":
                sizes[identifier] = int(row["bytes"] or 0)
            elif identifier in sizes:
                sizes[identifier] += int(row["bytes"] or 0)
            processed += 1
            report()
        report(force=True)
    return sizes


def decoded(value: object) -> dict:
    if not isinstance(value, str):
        return {}
    try:
        item = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return item if isinstance(item, dict) else {}


def message_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        decoded_content = json.loads(value)
    except json.JSONDecodeError:
        decoded_content = value
    text = content_text(decoded_content)
    return " ".join(text.split())


def first_user_texts(database: sqlite3.Connection) -> dict[str, str]:
    result: dict[str, str] = {}
    rows = database.execute(
        """
        SELECT session_id, content
        FROM messages
        WHERE role = 'user' AND coalesce(active, 1) = 1
        ORDER BY timestamp, id
        """
    )
    for row in rows:
        if row["session_id"] in result:
            continue
        if text := substantive_user_text(message_text(row["content"])):
            result[row["session_id"]] = text
    return result


def discover(path: Path, progress: ScanReporter) -> list[Session]:
    progress.update("Hermes", 0, 1, 0, 0)
    try:
        with connect(path) as database:
            require_schema(database)
            sizes = logical_sizes(database, progress)
            fallbacks = first_user_texts(database)
            activity = {
                row["session_id"]: row["latest"]
                for row in database.execute(
                    """
                    SELECT session_id, max(timestamp) AS latest
                    FROM messages
                    WHERE coalesce(active, 1) = 1
                    GROUP BY session_id
                    """
                )
            }
            rows = list(
                database.execute(
                    """
                    SELECT id, source, model_config, parent_session_id,
                           started_at, ended_at, end_reason, cwd, git_repo_root,
                           title, last_activity_at, archived
                    FROM sessions
                    ORDER BY coalesce(last_activity_at, ended_at, started_at) DESC, id
                    """
                )
            )
    except (OSError, sqlite3.Error):
        progress.invalid.add(path)
        return []

    sessions: list[Session] = []
    total = sum(sizes.values())
    for row in rows:
        identifier = row["id"]
        if not isinstance(identifier, str) or not identifier:
            progress.skipped.add(path)
            continue
        parent = row["parent_session_id"]
        parent = parent if isinstance(parent, str) and parent else None
        config = decoded(row["model_config"])
        delegate = config.get("_delegate_from")
        branch = config.get("_branched_from")
        delegate = delegate if isinstance(delegate, str) and delegate else None
        branch = branch if isinstance(branch, str) and branch else None
        is_branch = branch is not None and (parent is None or branch == parent)
        is_delegate = delegate is not None and (parent is None or delegate == parent)
        title = row["title"] if isinstance(row["title"], str) else ""
        if not title.strip():
            title = untitled_title(fallbacks.get(identifier, "untitled"))
        modified = max(
            value
            for value in (
                activity.get(identifier),
                row["last_activity_at"],
                row["ended_at"],
                row["started_at"],
                0,
            )
            if isinstance(value, (int, float))
        )
        sessions.append(
            Session(
                path=path,
                size=sizes.get(identifier, 0),
                modified=float(modified),
                source="hermes",
                origin="subagent" if is_delegate else "primary",
                cwd=(row["git_repo_root"] or row["cwd"] or "(unknown)"),
                session_id=identifier,
                parent_id=None if is_branch else parent,
                title=title.strip(),
                forked_from=branch or "",
                archived=bool(row["archived"]),
                source_home=str(path.parent),
                size_is_logical=True,
            )
        )
    progress.update("Hermes", len(sessions), len(rows), total, total)
    return sessions


def scalar(database: sqlite3.Connection, query: str, identifier: str) -> object:
    row = database.execute(query, (identifier,)).fetchone()
    return row[0] if row is not None else None


def endpoint(
    database: sqlite3.Connection, identifier: str, role: str, direction: str
) -> str | None:
    rows = database.execute(
        f"""
        SELECT content FROM messages
        WHERE session_id = ? AND role = ? AND coalesce(active, 1) = 1
        ORDER BY timestamp {direction}, id {direction}
        """,
        (identifier, role),
    )
    for row in rows:
        text = message_text(row["content"])
        if role == "user":
            text = substantive_user_text(text)
        if text:
            return text
    return None


def load_brief(session: Session, poll=None, preview: bool = False) -> BriefData:
    del poll, preview
    try:
        with connect(session.path) as database:
            require_schema(database)
            metadata = database.execute(
                """
                SELECT source, model, billing_provider
                FROM sessions WHERE id = ?
                """,
                (session.session_id,),
            ).fetchone()
            if metadata is None:
                raise OSError("session is no longer present")
            counts = database.execute(
                """
                SELECT
                  sum(CASE WHEN active = 1 AND role = 'user' THEN 1 ELSE 0 END),
                  sum(CASE WHEN active = 1 AND role = 'assistant' THEN 1 ELSE 0 END),
                  sum(CASE WHEN compacted = 1 OR _compressed_summary = 1
                           THEN 1 ELSE 0 END)
                FROM messages WHERE session_id = ?
                """,
                (session.session_id,),
            ).fetchone()
            users, assistants, compactions = (int(value or 0) for value in counts)
            first_user = endpoint(database, session.session_id, "user", "ASC")
            latest_user = endpoint(database, session.session_id, "user", "DESC")
            latest_reply = endpoint(database, session.session_id, "assistant", "DESC")
    except sqlite3.Error as error:
        raise OSError(f"could not read Hermes session: {error}") from error

    provider = metadata["billing_provider"]
    model = metadata["model"]
    recorded_via = metadata["source"]
    return BriefData(
        first_user,
        latest_user,
        latest_reply,
        None,
        Counter({"message": users + assistants}),
        [recorded_via] if isinstance(recorded_via, str) and recorded_via else [],
        session.task_path,
        session.forked_from,
        providers=[provider] if isinstance(provider, str) and provider else [],
        latest_model=model if isinstance(model, str) else "",
        user_messages=users,
        assistant_messages=assistants,
        turns=users,
        compactions=compactions,
    )


def session_controls(session: Session) -> SessionControls:
    if session.archived or not SESSION_ID.fullmatch(session.session_id):
        return SessionControls()
    return SessionControls(
        (
            SessionCommand(
                "Resume", ("hermes", "--tui", "--resume", session.session_id)
            ),
        )
    )
