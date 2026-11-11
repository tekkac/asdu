"""Pi JSONL sessions and recoverable transcript removal."""

import json
from dataclasses import replace
from pathlib import Path

from asdu_sessions import (
    ActionResult,
    Session,
    SessionCommand,
    SessionControls,
    absolute_path,
    iter_jsonl,
    read_jsonl_brief,
    require_unchanged,
    scan_paths,
    substantive_user_text,
    trash_session,
    untitled_title,
)

from . import omp


def available_actions(session: Session) -> frozenset[str]:
    return frozenset({"trash"}) if session.source == "pi" else frozenset()


def session_controls(session: Session) -> SessionControls:
    return SessionControls(
        (SessionCommand("Resume", ("pi", "--session", str(session.path))),)
    )


def discover(root: Path, progress) -> list[Session]:
    def inspect(path: Path) -> Session | None:
        if path.is_symlink():
            return None
        try:
            with path.open(encoding="utf-8") as stream:
                header = json.loads(stream.readline())
            if not isinstance(header, dict) or header.get("type") != "session":
                return None
            identifier = header.get("id")
            if not isinstance(identifier, str) or not identifier:
                return None
            if header.get("version", 1) not in (1, 2, 3):
                return None
            stat = path.stat()
        except (OSError, ValueError, UnicodeError):
            return None
        title = first = reply = ""
        for item in iter_jsonl(path, progress=progress):
            if item.get("type") == "session_info" and isinstance(item.get("name"), str):
                title = item["name"].strip()
            if not first:
                first = next(
                    (
                        substantive_user_text(text)
                        for text in omp.user_texts(item)
                        if substantive_user_text(text)
                    ),
                    "",
                )
            for text in omp.assistant_texts(item):
                reply = text
        cwd = header.get("cwd")
        parent = header.get("parentSession")
        return Session(
            path,
            stat.st_size,
            stat.st_mtime,
            "pi",
            "primary",
            cwd if isinstance(cwd, str) else "(unknown)",
            identifier,
            None,
            title
            or untitled_title(first or (f"reply: {reply}" if reply else "untitled")),
            forked_from=parent if isinstance(parent, str) else "",
            source_home=str(root),
        )

    sessions = scan_paths("Pi", root.rglob("*.jsonl"), inspect, progress)
    by_path = {str(entry.path.absolute()): entry.session_id for entry in sessions}
    return [
        replace(
            entry,
            forked_from=by_path.get(
                str(Path(entry.forked_from).expanduser().absolute()), entry.forked_from
            ),
        )
        if entry.forked_from
        else entry
        for entry in sessions
    ]


def prepare_actions(sessions: list[Session], action: str) -> None:
    if action != "trash":
        raise OSError(f"Pi cannot {action} this session")
    for session in sessions:
        if not session.source_home:
            raise OSError("Pi session root is unknown")
        root = absolute_path(Path(session.source_home))
        path = absolute_path(session.path)
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise OSError("Pi session is outside its configured root") from error
        if session.source != "pi" or path.suffix != ".jsonl" or not relative.parts:
            raise OSError("invalid Pi session path")
        current = root
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise OSError("refusing to trash a symlinked Pi session")
        require_unchanged(session)
        try:
            with path.open(encoding="utf-8") as stream:
                header = json.loads(stream.readline())
        except (OSError, ValueError, UnicodeError) as error:
            raise OSError("could not verify Pi session") from error
        if (
            not isinstance(header, dict)
            or header.get("type") != "session"
            or header.get("id") != session.session_id
        ):
            raise OSError("Pi session changed since scan; rescan before acting")


def perform_action(session: Session, action: str) -> ActionResult:
    prepare_actions([session], action)
    trash_session(session)
    return ActionResult()


def enrich_brief(item, data):
    message = item.get("message") if item.get("type") == "message" else None
    if isinstance(message, dict) and message.get("role") == "assistant":
        provider = message.get("provider")
        model = message.get("model")
        if isinstance(provider, str) and provider and provider not in data.providers:
            data.providers.append(provider)
        if isinstance(model, str) and model:
            data.latest_model = model


def load_brief(session: Session, poll=None, preview=False):
    data = read_jsonl_brief(
        session,
        omp.user_texts,
        omp.assistant_texts,
        omp.clean_user_text,
        poll,
        preview,
        enrich_brief,
        compaction_events=("compaction",),
    )
    data.turns = data.user_messages
    return data
