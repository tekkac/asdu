"""OpenCode SQLite discovery and native session controls."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from asdu_sessions import (
    ActionResult,
    BriefData,
    ScanReporter,
    Session,
    SessionCommand,
    SessionControls,
    substantive_user_text,
    untitled_title,
)

REQUIRED_COLUMNS = {
    "session": {
        "id",
        "parent_id",
        "directory",
        "title",
        "time_updated",
        "time_archived",
    },
    "message": {"id", "session_id", "time_created", "data"},
    "part": {"id", "message_id", "session_id", "time_created", "data"},
}
GENERATED_TITLE = re.compile(r"^New session(?:\s+-\s+.*)?$", re.IGNORECASE)
SESSION_ID = re.compile(r"^ses_[A-Za-z0-9]+$")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a live SQLite store read-only, including its current WAL."""
    uri = path.resolve().as_uri() + "?mode=ro"
    database = sqlite3.connect(uri, uri=True, timeout=1)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only = ON")
    try:
        yield database
    finally:
        database.close()


def columns(database: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in database.execute(f'PRAGMA table_info("{table}")')}


def require_schema(database: sqlite3.Connection) -> None:
    for table, required in REQUIRED_COLUMNS.items():
        missing = required - columns(database, table)
        if missing:
            raise sqlite3.DatabaseError(
                f"unsupported OpenCode database: {table} lacks {', '.join(sorted(missing))}"
            )


def byte_sum(columns_: tuple[str, ...]) -> str:
    return " + ".join(
        f'coalesce(length(cast("{column}" as blob)), 0)' for column in columns_
    )


def logical_sizes(
    database: sqlite3.Connection, progress: ScanReporter | None = None
) -> dict[str, int]:
    """Estimate session-owned bytes while streaming measurable DB progress."""
    session_columns = (
        "id",
        "project_id",
        "parent_id",
        "slug",
        "directory",
        "title",
        "version",
        "share_url",
        "summary_diffs",
        "revert",
        "permission",
        "workspace_id",
        "path",
        "agent",
        "model",
        "metadata",
    )
    present = columns(database, "session")
    session_measured = tuple(
        column for column in session_columns if column in present
    )
    owned_tables = (
        ("message", "session_id", ("id", "session_id", "data"), 32),
        ("part", "session_id", ("id", "message_id", "session_id", "data"), 32),
        ("session_input", "session_id", ("id", "session_id", "prompt", "delivery"), 40),
        ("session_message", "session_id", ("id", "session_id", "type", "data"), 40),
        (
            "session_context_epoch",
            "session_id",
            ("session_id", "baseline", "snapshot"),
            32,
        ),
        ("todo", "session_id", ("session_id", "content", "status", "priority"), 40),
        ("session_share", "session_id", ("session_id", "id", "secret", "url"), 40),
        ("event", "aggregate_id", ("id", "aggregate_id", "type", "data"), 32),
        ("event_sequence", "aggregate_id", ("aggregate_id", "owner_id"), 24),
    )
    table_specs = []
    for table, owner, candidates, overhead in owned_tables:
        present = columns(database, table)
        measured = tuple(column for column in candidates if column in present)
        if owner not in present or not measured:
            continue
        table_specs.append((table, owner, measured, overhead))

    total_records = database.execute('SELECT count(*) FROM "session"').fetchone()[0]
    total_records += sum(
        database.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        for table, _, _, _ in table_specs
    )
    processed = 0

    def report(force: bool = False) -> None:
        if progress is not None and (force or processed % 1024 == 0):
            progress.update(
                "OpenCode",
                processed,
                total_records,
                processed,
                total_records,
                "records",
            )

    sizes = {}
    for row in database.execute(
        f'SELECT id, {byte_sum(session_measured)} AS bytes FROM "session"'
    ):
        sizes[row["id"]] = int(row["bytes"] or 0) + 64
        processed += 1
        report()

    for table, owner, measured, overhead in table_specs:
        query = (
            f'SELECT "{owner}" AS owner, '
            f"{byte_sum(measured)} + {overhead} AS bytes "
            f'FROM "{table}"'
        )
        for row in database.execute(query):
            if row["owner"] in sizes:
                sizes[row["owner"]] += int(row["bytes"] or 0)
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


def text_part(value: object) -> str:
    item = decoded(value)
    if (
        item.get("type") != "text"
        or item.get("synthetic") is True
        or item.get("ignored") is True
    ):
        return ""
    text = item.get("text")
    return " ".join(text.split()) if isinstance(text, str) else ""


def first_user_texts(database: sqlite3.Connection) -> dict[str, str]:
    result: dict[str, str] = {}
    query = """
        SELECT message.session_id, part.data
        FROM message JOIN part ON part.message_id = message.id
        WHERE json_valid(message.data)
          AND json_extract(message.data, '$.role') = 'user'
          AND json_valid(part.data)
          AND json_extract(part.data, '$.type') = 'text'
          AND coalesce(json_extract(part.data, '$.synthetic'), 0) = 0
          AND coalesce(json_extract(part.data, '$.ignored'), 0) = 0
        ORDER BY message.time_created, part.time_created, part.id
    """
    for row in database.execute(query):
        if row["session_id"] in result:
            continue
        if text := substantive_user_text(text_part(row["data"])):
            result[row["session_id"]] = text
    return result


def discover(path: Path, progress: ScanReporter) -> list[Session]:
    # SQLite aggregation happens before rows can be emitted. Announce the
    # source first so the startup panel does not appear stuck on its predecessor.
    progress.update("OpenCode", 0, 1, 0, 0)
    try:
        with connect(path) as database:
            require_schema(database)
            sizes = logical_sizes(database, progress)
            fallbacks = first_user_texts(database)
            rows = list(
                database.execute(
                    """
                    SELECT id, parent_id, directory, title, time_updated,
                           time_archived
                    FROM session
                    ORDER BY time_updated DESC, id
                    """
                )
            )
    except (OSError, sqlite3.Error):
        progress.invalid.add(path)
        return []

    total = sum(sizes.values())
    sessions: list[Session] = []
    for index, row in enumerate(rows, 1):
        identifier = row["id"]
        if not isinstance(identifier, str) or not identifier:
            progress.skipped.add(path)
            continue
        title = row["title"] if isinstance(row["title"], str) else ""
        if not title.strip() or GENERATED_TITLE.fullmatch(title.strip()):
            title = untitled_title(fallbacks.get(identifier, "untitled"))
        size = sizes.get(identifier, 0)
        updated = row["time_updated"]
        sessions.append(
            Session(
                path,
                size,
                float(updated) / 1000 if isinstance(updated, (int, float)) else 0,
                "opencode",
                "subagent" if row["parent_id"] else "primary",
                row["directory"] if isinstance(row["directory"], str) else "(unknown)",
                identifier,
                row["parent_id"] if isinstance(row["parent_id"], str) else None,
                title.strip(),
                archived=row["time_archived"] is not None,
                source_home=str(path.parent),
                size_is_logical=True,
            )
        )
    progress.update("OpenCode", len(sessions), len(rows), total, total)
    return sessions


def load_brief(session: Session, poll=None, preview: bool = False) -> BriefData:
    del preview
    data = BriefData(
        None,
        None,
        None,
        None,
        Counter(),
        [],
        session.task_path,
        session.forked_from,
    )
    try:
        with connect(session.path) as database:
            require_schema(database)
            rows = database.execute(
                """
                SELECT message.id AS message_id, message.data AS message_data,
                       part.data AS part_data
                FROM message LEFT JOIN part ON part.message_id = message.id
                WHERE message.session_id = ?
                ORDER BY message.time_created, message.id,
                         part.time_created, part.id
                """,
                (session.session_id,),
            )
            seen_messages: set[str] = set()
            for index, row in enumerate(rows):
                if poll is not None and index % 128 == 0:
                    poll()
                message = decoded(row["message_data"])
                role = message.get("role")
                if row["message_id"] not in seen_messages:
                    seen_messages.add(row["message_id"])
                    data.event_counts["message"] += 1
                    if role == "user":
                        data.user_messages += 1
                    elif role == "assistant":
                        data.assistant_messages += 1
                    model = message.get("model")
                    provider = (
                        model.get("providerID")
                        if isinstance(model, dict)
                        else message.get("providerID")
                    )
                    model_id = (
                        model.get("modelID")
                        if isinstance(model, dict)
                        else message.get("modelID")
                    )
                    if (
                        isinstance(provider, str)
                        and provider
                        and (not data.providers or data.providers[-1] != provider)
                    ):
                        data.providers.append(provider)
                    if isinstance(model_id, str) and model_id:
                        data.latest_model = model_id
                part = decoded(row["part_data"])
                part_type = part.get("type")
                if isinstance(part_type, str):
                    data.event_counts[part_type] += 1
                text = text_part(row["part_data"])
                if not text:
                    continue
                if role == "user":
                    clean = substantive_user_text(text)
                    if clean:
                        data.first_user = data.first_user or clean
                        data.latest_user = clean
                elif role == "assistant":
                    data.latest_reply = text
    except sqlite3.Error as error:
        raise OSError(f"could not read OpenCode session: {error}") from error
    data.turns = data.user_messages
    data.compactions = data.event_counts["compaction"]
    return data


def available_actions(_session: Session) -> frozenset[str]:
    return frozenset({"delete"})


def action_scope(_session: Session, action: str) -> str:
    return "native-tree" if action == "delete" else "single"


def valid_session_id(identifier: str) -> bool:
    return bool(SESSION_ID.fullmatch(identifier))


def session_controls(session: Session) -> SessionControls:
    if session.archived or not valid_session_id(session.session_id):
        return SessionControls()
    return SessionControls(
        (
            SessionCommand(
                "Resume",
                ("opencode", session.cwd, "--session", session.session_id),
            ),
        )
    )


def prepare_actions(sessions: list[Session], action: str) -> None:
    if action != "delete":
        return
    if not sessions or any(session.path != sessions[0].path for session in sessions):
        raise OSError("OpenCode delete scope must use one session database")
    if any(not valid_session_id(session.session_id) for session in sessions):
        raise OSError("invalid OpenCode session ID; rescan before acting")
    try:
        with connect(sessions[0].path) as database:
            placeholders = ",".join("?" for _ in sessions)
            current = {
                row["id"]: row["time_updated"]
                for row in database.execute(
                    f"SELECT id, time_updated FROM session WHERE id IN ({placeholders})",
                    tuple(session.session_id for session in sessions),
                )
            }
    except sqlite3.Error as error:
        raise OSError(f"could not verify OpenCode sessions: {error}") from error
    for session in sessions:
        if current.get(session.session_id) != round(session.modified * 1000):
            raise OSError("OpenCode session changed since scan; rescan before acting")


def descendant_ids(database: sqlite3.Connection, identifier: str) -> list[str]:
    return [
        row[0]
        for row in database.execute(
            """
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM session WHERE id = ?
                UNION
                SELECT session.id FROM session JOIN tree
                  ON session.parent_id = tree.id
            )
            SELECT id FROM tree
            """,
            (identifier,),
        )
    ]


def perform_action(session: Session, action: str) -> ActionResult:
    if action != "delete":
        raise OSError(f"OpenCode cannot {action} this session")
    if not valid_session_id(session.session_id):
        raise OSError("invalid OpenCode session ID; rescan before acting")
    try:
        with connect(session.path) as database:
            affected = descendant_ids(database, session.session_id)
    except sqlite3.Error as error:
        raise OSError(f"could not verify OpenCode delete: {error}") from error
    if not affected:
        raise OSError("OpenCode session is no longer present; rescan before acting")

    environment = os.environ.copy()
    environment["OPENCODE_DB"] = str(session.path)
    environment["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
    try:
        result = subprocess.run(
            ["opencode", "--pure", "session", "delete", session.session_id],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=environment,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("OpenCode delete timed out") from error
    except OSError as error:
        raise OSError(f"Could not run OpenCode: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise OSError(
            "OpenCode delete failed"
            + (f": {detail}" if detail else f" (exit {result.returncode})")
        )

    try:
        with connect(session.path) as database:
            remaining = []
            for start in range(0, len(affected), 500):
                batch = affected[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                remaining.extend(
                    row[0]
                    for row in database.execute(
                        f"SELECT id FROM session WHERE id IN ({placeholders})", batch
                    )
                )
    except sqlite3.Error as error:
        raise OSError(f"could not verify OpenCode delete: {error}") from error
    if remaining:
        raise OSError("OpenCode reported success but the session tree still exists")
    return ActionResult()
