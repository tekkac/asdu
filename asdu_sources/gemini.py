"""Read-only Gemini CLI session discovery (legacy JSON and current JSONL)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from asdu_sessions import (
    BriefData,
    ScanReporter,
    Session,
    SessionCommand,
    SessionControls,
    content_text,
    open_transcript,
    path_tree_size,
    preview_transcript,
    substantive_user_text,
    untitled_title,
)

SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
IGNORED_USER_PREFIXES = ("/", "?", "<session_context>", "<hook_context>")


@dataclass
class Conversation:
    metadata: dict
    messages: list[dict]
    rewinds: int = 0

    @property
    def session_id(self) -> str:
        value = self.metadata.get("sessionId")
        return value if isinstance(value, str) else ""


def parse_time(value: object, fallback: float = 0) -> float:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return fallback


def clean_text(value: object) -> str:
    return " ".join(content_text(value).split())


def user_text(message: dict) -> str:
    if message.get("type") != "user":
        return ""
    text = clean_text(message.get("content"))
    if text.startswith(IGNORED_USER_PREFIXES):
        return ""
    return substantive_user_text(text)


def reply_text(message: dict) -> str:
    return clean_text(message.get("content")) if message.get("type") == "gemini" else ""


def resumable(message: dict) -> bool:
    if user_text(message):
        return True
    return message.get("type") == "gemini" and bool(
        reply_text(message) or message.get("toolCalls") or message.get("thoughts")
    )


def apply_record(
    record: dict, metadata: dict, messages: dict[str, dict]
) -> int:
    rewind = record.get("$rewindTo")
    if isinstance(rewind, str):
        keys = list(messages)
        try:
            start = keys.index(rewind)
        except ValueError:
            messages.clear()
        else:
            for identifier in keys[start:]:
                messages.pop(identifier, None)
        return 1

    update = record.get("$set")
    if isinstance(update, dict):
        replacement = update.get("messages")
        if isinstance(replacement, list):
            messages.clear()
            for message in replacement:
                if isinstance(message, dict) and isinstance(message.get("id"), str):
                    messages[message["id"]] = message
        metadata.update(update)
        return 0

    identifier = record.get("id")
    if isinstance(identifier, str):
        messages[identifier] = record
        return 0

    if isinstance(record.get("sessionId"), str):
        metadata.update(record)
        embedded = record.get("messages")
        if isinstance(embedded, list):
            for message in embedded:
                if isinstance(message, dict) and isinstance(message.get("id"), str):
                    messages[message["id"]] = message
    return 0


def load_conversation(path: Path, preview: bool = False) -> Conversation | None:
    try:
        if path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            messages = {
                item["id"]: item
                for item in payload.get("messages", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            return Conversation(payload, list(messages.values()))

        metadata: dict = {}
        messages: dict[str, dict] = {}
        rewinds = 0
        context = preview_transcript(path) if preview else open_transcript(path, "rt")
        with context as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    rewinds += apply_record(record, metadata, messages)
        if not metadata:
            return None
        return Conversation(metadata, list(messages.values()), rewinds)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def safe_id(identifier: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", identifier)


def project_directory(path: Path) -> Path | None:
    for parent in path.parents:
        if parent.name == "chats":
            return parent.parent
    return None


def project_paths(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    registry = root / "projects.json"
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if isinstance(projects, dict):
        for cwd, slug in projects.items():
            if not isinstance(cwd, str) or not isinstance(slug, str):
                continue
            result[slug] = cwd
            result[hashlib.sha256(cwd.encode()).hexdigest()] = cwd

    temp = root / "tmp"
    for directory in temp.iterdir() if temp.is_dir() else ():
        marker = directory / ".project_root"
        try:
            cwd = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if cwd:
            result[directory.name] = cwd
            result[hashlib.sha256(cwd.encode()).hexdigest()] = cwd
    return result


def session_cwd(conversation: Conversation, directory: Path, paths: dict[str, str]) -> str:
    if cwd := paths.get(directory.name):
        return cwd
    project_hash = conversation.metadata.get("projectHash")
    if isinstance(project_hash, str) and (cwd := paths.get(project_hash)):
        return cwd
    directories = conversation.metadata.get("directories")
    if isinstance(directories, list):
        for cwd in directories:
            if isinstance(cwd, str) and cwd:
                return cwd
    return "(unknown)"


def conversation_title(conversation: Conversation) -> str:
    summary = conversation.metadata.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    scratchpad = conversation.metadata.get("memoryScratchpad")
    workflow = scratchpad.get("workflowSummary") if isinstance(scratchpad, dict) else None
    if isinstance(workflow, str) and workflow.strip():
        return workflow.strip()
    first = next((text for message in conversation.messages if (text := user_text(message))), "")
    return untitled_title(first or "untitled")


def owned_artifact_size(directory: Path, identifier: str) -> int:
    safe = safe_id(identifier)
    return sum(
        path_tree_size(path)
        for path in (
            directory / "logs" / f"session-{safe}.jsonl",
            directory / "tool-outputs" / f"session-{safe}",
            directory / safe,
        )
    )


def discover(root: Path, progress: ScanReporter) -> list[Session]:
    temp = root / "tmp"
    files = sorted(temp.glob("*/chats/**/*.json")) + sorted(
        temp.glob("*/chats/**/*.jsonl")
    )
    sizes = [path_tree_size(path) for path in files]
    total = sum(sizes)
    paths = project_paths(root)
    grouped: dict[str, list[tuple[Path, Conversation, int]]] = {}
    done = 0
    for index, (path, size) in enumerate(zip(files, sizes), 1):
        conversation = load_conversation(path)
        if conversation is None or not conversation.session_id:
            progress.invalid.add(path)
        else:
            grouped.setdefault(conversation.session_id, []).append(
                (path, conversation, size)
            )
        done += size
        progress.update("Gemini", index, len(files), done, total)

    sessions: list[Session] = []
    for identifier, records in grouped.items():
        path, conversation, _ = max(
            records,
            key=lambda record: parse_time(
                record[1].metadata.get("lastUpdated"), record[0].stat().st_mtime
            ),
        )
        directory = project_directory(path)
        if directory is None:
            progress.invalid.add(path)
            continue
        transcript_size = sum(size for _, _, size in records)
        artifact_directories = {
            directory
            for candidate, _, _ in records
            if (directory := project_directory(candidate)) is not None
        }
        size = transcript_size + sum(
            owned_artifact_size(candidate, identifier)
            for candidate in artifact_directories
        )
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0
        modified = parse_time(conversation.metadata.get("lastUpdated"), modified)
        kind = conversation.metadata.get("kind")
        parent = path.parent.name if path.parent.parent.name == "chats" else None
        is_child = kind == "subagent" or parent is not None
        sessions.append(
            Session(
                path,
                size,
                modified,
                "gemini",
                "subagent" if is_child else "primary",
                session_cwd(conversation, directory, paths),
                identifier,
                parent if is_child else None,
                conversation_title(conversation),
                source_home=str(root),
            )
        )
    if not files:
        progress.update("Gemini", 0, 0, 0, 0)
    return sessions


def load_brief(session: Session, poll=None, preview: bool = False) -> BriefData:
    conversation = load_conversation(session.path, preview)
    if conversation is None:
        raise OSError("Gemini session is no longer readable")
    users: list[str] = []
    replies: list[str] = []
    providers: list[str] = []
    latest_model = ""
    counts: Counter[str] = Counter()
    for index, message in enumerate(conversation.messages):
        if poll is not None and index % 128 == 0:
            poll()
        kind = str(message.get("type", "unknown"))
        counts[kind] += 1
        if text := user_text(message):
            users.append(text)
        if text := reply_text(message):
            replies.append(text)
        tool_calls = message.get("toolCalls")
        thoughts = message.get("thoughts")
        counts["tool"] += len(tool_calls) if isinstance(tool_calls, list) else 0
        counts["thought"] += len(thoughts) if isinstance(thoughts, list) else 0
        model = message.get("model")
        if isinstance(model, str) and model:
            latest_model = model
        provider = message.get("provider")
        if isinstance(provider, str) and provider and provider not in providers:
            providers.append(provider)
    if conversation.rewinds:
        counts["rewind"] = conversation.rewinds
    return BriefData(
        users[0] if users else None,
        users[-1] if users else None,
        replies[-1] if replies else None,
        None,
        counts,
        ["Gemini CLI"],
        session.task_path,
        session.forked_from,
        providers=providers,
        latest_model=latest_model,
        user_messages=len(users),
        assistant_messages=sum(
            message.get("type") == "gemini" for message in conversation.messages
        ),
        turns=len(users),
        compactions=0,
    )


def session_controls(session: Session) -> SessionControls:
    if session.origin != "primary" or not SESSION_ID.fullmatch(session.session_id):
        return SessionControls()
    conversation = load_conversation(session.path)
    if conversation is None or not any(resumable(item) for item in conversation.messages):
        return SessionControls()
    return SessionControls(
        (SessionCommand("Resume", ("gemini", "--resume", session.session_id)),)
    )
