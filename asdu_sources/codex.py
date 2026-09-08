"""Codex JSONL discovery and message parsing."""

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from asdu_sessions import (
    ActionResult,
    ContentCache,
    ScanReporter,
    Session,
    SessionCommand,
    SessionControls,
    TagRule,
    in_scope,
    iter_jsonl,
    message_texts,
    read_jsonl_brief,
    scan_paths,
    session_label,
    substantive_user_text,
    untitled_title,
)


def available_actions(session: Session) -> frozenset[str]:
    if session.archived:
        return frozenset({"unarchive", "delete"})
    return frozenset({"archive", "delete"})


def session_controls(session: Session) -> SessionControls:
    commands = (
        ()
        if session.archived
        else (SessionCommand("Resume", ("codex", "resume", session.session_id)),)
    )
    return SessionControls(commands)


def perform_action(session: Session, action: str) -> ActionResult:
    """Delegate lifecycle changes to the Codex store owner."""
    if action not in available_actions(session):
        raise OSError(f"Codex cannot {action} this session")
    command = ["codex", action, session.session_id]
    if action == "delete":
        command.insert(2, "--force")
    environment = os.environ.copy()
    if session.source_home:
        environment["CODEX_HOME"] = session.source_home
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
    except OSError as error:
        raise OSError(f"Could not run Codex: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise OSError(
            f"Codex {action} failed"
            + (f": {detail}" if detail else f" (exit {result.returncode})")
        )
    if action == "delete":
        return ActionResult()
    home = Path(session.source_home) if session.source_home else session.path.parent
    root = home / ("archived_sessions" if action == "archive" else "sessions")
    path = next(root.rglob(f"*{session.session_id}*.jsonl"), session.path)
    replacement = replace(session, path=path, archived=action == "archive")
    return ActionResult(replacement=replacement)


def load_titles(codex_home: Path) -> dict[str, str]:
    index = codex_home / "session_index.jsonl"
    titles: dict[str, str] = {}
    for item in iter_jsonl(index):
        session_id = item.get("id")
        title = item.get("thread_name")
        if isinstance(session_id, str) and isinstance(title, str) and title.strip():
            titles[session_id] = title.strip()
    return titles


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
    """Use the native goal or first real request when no title was recorded.

    Codex serializes environment, skill, and AGENTS.md material as user-role
    messages too, so those preambles are deliberately skipped.  The bounded
    scan keeps an inventory fast even when a rollout is hundreds of megabytes.
    """
    fallback = ""
    for item in iter_jsonl(path, 4096):
        if objective := goal_objective(item):
            return objective
        for text in user_texts(item):
            title = substantive_user_text(text)
            if title:
                return title
        for text in assistant_texts(item):
            fallback = text
    return f"reply: {fallback}" if fallback else "untitled"


def discover(
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
    archived_root = root.parent / "archived_sessions"

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
            archived=path.is_relative_to(archived_root),
            source_home=str(root.parent),
        )
        if not in_scope(session, scope):
            return session
        title = (
            session_label(session)
            if session.task_path
            else titles.get(session_id) or untitled_title(derive_title(path))
        )
        return replace(session, title=title)

    paths = list(root.rglob("rollout-*.jsonl"))
    if archived_root != root and archived_root.exists():
        paths.extend(archived_root.rglob("rollout-*.jsonl"))
    return scan_paths(
        "codex",
        "Codex",
        paths,
        inspect,
        rules,
        content_keywords,
        progress,
        cache,
        scope,
        user_texts,
        clean_user_text,
    )


clean_user_text = substantive_user_text


def user_texts(item: dict) -> Iterable[str]:
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


def assistant_texts(item: dict) -> list[str]:
    payload = item.get("payload") if item.get("type") == "response_item" else None
    if isinstance(payload, dict) and payload.get("role") == "assistant":
        return message_texts(payload)
    return []


def goal_objective(item: dict) -> str:
    payload = item.get("payload")
    if not (
        item.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "thread_goal_updated"
    ):
        return ""
    goal = payload.get("goal")
    objective = goal.get("objective") if isinstance(goal, dict) else None
    return " ".join(objective.split()) if isinstance(objective, str) else ""


def enrich_brief(item, data):
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return
    if item.get("type") == "session_meta":
        data.task_path, data.forked_from = task_metadata(payload)
        source, originator = payload.get("source"), payload.get("originator")
        if source == "vscode" or originator == "codex_vscode":
            data.recorded_via.append("VS Code")
        elif source == "exec":
            data.recorded_via.append("Codex Exec")
        if originator == "codex_sdk_ts":
            data.recorded_via.append("Codex SDK (TypeScript)")
        elif originator == "codex_exec":
            data.recorded_via.append("Codex Exec")
        provider = payload.get("model_provider")
        if (
            isinstance(provider, str)
            and provider
            and (not data.providers or data.providers[-1] != provider)
        ):
            data.providers.append(provider)
    elif item.get("type") == "turn_context":
        model = payload.get("model")
        if isinstance(model, str) and model:
            data.latest_model = model
    elif objective := goal_objective(item):
        data.first_objective = data.first_objective or objective


def load_brief(session, poll=None, preview=False):
    return read_jsonl_brief(
        session,
        user_texts,
        assistant_texts,
        clean_user_text,
        poll,
        preview,
        enrich_brief,
    )
