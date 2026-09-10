"""Claude JSONL discovery and message parsing."""

import json
import subprocess
from collections.abc import Iterable
from pathlib import Path

from asdu_sessions import (
    ActionResult,
    ScanReporter,
    Session,
    SessionCommand,
    SessionControls,
    iter_jsonl,
    message_texts,
    preview_transcript,
    read_jsonl_brief,
    scan_paths,
    substantive_user_text,
    trash_session,
    untitled_title,
)


def available_actions(session: Session) -> frozenset[str]:
    return frozenset({"trash"})


def list_agents(include_completed: bool = False, required: bool = False) -> list[dict]:
    """Read Claude's native runtime registry with bounded, noninteractive I/O."""
    command = ["claude", "agents", "--json"]
    if include_completed:
        command.append("--all")
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        agents = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        if required:
            raise OSError("could not verify active Claude sessions") from error
        return []
    if not isinstance(agents, list):
        if required:
            raise OSError("could not verify active Claude sessions")
        return []
    return [agent for agent in agents if isinstance(agent, dict)]


def active_session_ids() -> set[str]:
    identities = set()
    for agent in list_agents(required=True):
        for field in ("id", "sessionId"):
            identity = agent.get(field)
            if isinstance(identity, str) and identity:
                identities.add(identity)
    return identities


def prepare_actions(sessions: list[Session], action: str) -> None:
    candidates = [session for session in sessions if action == "trash"]
    if not candidates:
        return
    active = active_session_ids()
    if any(session.session_id in active for session in candidates):
        raise OSError("refusing to change an active Claude session; stop it first")


def background_agent(session: Session) -> dict | None:
    """Read daemon state only when one session's brief requests it."""
    if session.origin in ("subagent", "sidechain"):
        return None
    agents = list_agents(include_completed=True)
    return next(
        (
            agent
            for agent in agents
            if isinstance(agent, dict) and agent.get("sessionId") == session.session_id
        ),
        None,
    )


def session_controls(session: Session) -> SessionControls:
    if session.origin in ("subagent", "sidechain"):
        return SessionControls()
    agent = background_agent(session)
    if agent is None:
        return SessionControls(
            (SessionCommand("Resume", ("claude", "--resume", session.session_id)),)
        )
    short_id = agent.get("id")
    short_id = short_id if isinstance(short_id, str) else session.session_id
    commands = tuple(
        SessionCommand(label, ("claude", verb, short_id))
        for label, verb in (
            ("Attach", "attach"),
            ("Logs", "logs"),
            ("Stop", "stop"),
            ("Remove", "rm"),
        )
    )
    return SessionControls(
        commands,
        short_id,
        agent.get("kind") if isinstance(agent.get("kind"), str) else "",
        agent.get("state") if isinstance(agent.get("state"), str) else "",
    )


def custom_title(path: Path) -> str:
    """Read Claude's latest sampled native title without scanning a large file."""
    title = ""
    priority = 0
    try:
        lines = preview_transcript(path)
    except OSError:
        return title
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate, candidate_priority = None, 0
        if isinstance(item, dict):
            if item.get("type") == "custom-title":
                candidate, candidate_priority = item.get("customTitle"), 3
            elif item.get("type") == "agent-name" or item.get("agentName"):
                candidate, candidate_priority = item.get("agentName"), 3
            elif item.get("type") == "ai-title":
                candidate, candidate_priority = item.get("aiTitle"), 2
        if (
            isinstance(candidate, str)
            and candidate.strip()
            and candidate_priority >= priority
        ):
            title = candidate.strip()
            priority = candidate_priority
    return title


def session_paths(root: Path) -> Iterable[Path]:
    """Yield project transcripts and their native subagent transcripts only."""
    patterns = (
        "*.jsonl",
        "*/*.jsonl",
        "*/subagents/*.jsonl",
        "*/*/subagents/*.jsonl",
    )
    for pattern in patterns:
        yield from root.glob(pattern)


def discover(root: Path, progress: ScanReporter) -> list[Session]:
    """Small native Claude reader: CWD + session id live in normal JSONL events."""

    def inspect(path: Path) -> Session | None:
        fallback_id = path.stem
        cwd, session_id, origin, parent_id = "(unknown)", fallback_id, "unknown", None
        renamed = custom_title(path)
        title = renamed or "untitled"
        fallback = ""
        recognized = False
        for item in iter_jsonl(path, 4096, progress):
            if isinstance(item.get("cwd"), str):
                cwd = item["cwd"]
            native_session_id = item.get("sessionId")
            agent_id = item.get("agentId")
            if isinstance(native_session_id, str):
                session_id = native_session_id
                recognized = True
            if item.get("isSidechain") is False and origin != "sidechain":
                origin = "primary"
            if item.get("isSidechain") is True:
                if isinstance(agent_id, str) and agent_id:
                    session_id = agent_id
                    parent_id = (
                        native_session_id
                        if isinstance(native_session_id, str)
                        else None
                    )
                    origin = "subagent"
                else:
                    origin = "sidechain"
            candidate = next(iter(user_texts(item)), "") if title == "untitled" else ""
            for text in assistant_texts(item):
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
        if not renamed:
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
            source_home=str(root),
        )

    return scan_paths(
        "Claude",
        session_paths(root),
        inspect,
        progress,
    )


clean_user_text = substantive_user_text


def user_texts(item: dict) -> Iterable[str]:
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
    elif item.get("type") == "queue-operation" and item.get("operation") == "enqueue":
        content = item.get("content")
        if isinstance(content, str):
            yield content
    return


def assistant_texts(item: dict) -> list[str]:
    message = item.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    content = message.get("content")
    if isinstance(content, str):
        clean = " ".join(content.split())
        return [clean] if clean else []
    return message_texts(message)


def enrich_brief(item, data):
    entrypoint = item.get("entrypoint")
    if entrypoint == "claude-vscode":
        data.recorded_via.append("VS Code")
    elif isinstance(entrypoint, str) and entrypoint:
        data.recorded_via.append(entrypoint)
    message = item.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    if isinstance(model, str) and model and model != "<synthetic>":
        data.latest_model = model


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


def perform_action(session, action):
    if action == "trash":
        trash_session(session)
        return ActionResult()
    raise OSError(f"Claude cannot {action} this session")
