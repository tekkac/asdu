"""Read-only Kimi Code session bundles (state format 2)."""

from __future__ import annotations

import json
from pathlib import Path

from asdu_sessions import (
    BriefData,
    ScanReporter,
    Session,
    SessionCommand,
    SessionControls,
    content_text,
    iter_jsonl,
    read_jsonl_brief,
    substantive_user_text,
    untitled_title,
)


def available_actions(_session: Session) -> frozenset[str]:
    return frozenset()


def session_controls(session: Session) -> SessionControls:
    if session.origin != "primary" or session.archived:
        return SessionControls()
    return SessionControls(
        (SessionCommand("Resume", ("kimi", "--session", session.session_id)),)
    )


def path_size(path: Path) -> int:
    """Count each owned filesystem entry once without following symlinks."""
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
    except OSError:
        return 0
    total = 0
    try:
        items = path.rglob("*")
        for item in items:
            try:
                if item.is_symlink() or item.is_file():
                    total += item.lstat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def prompt_texts(item: dict, origin_kind: str):
    if item.get("type") != "context.append_message":
        return
    message = item.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return
    origin = message.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != origin_kind:
        return
    text = content_text(message.get("content"))
    if text:
        yield text


def user_texts(item: dict):
    yield from prompt_texts(item, "user")


def assistant_texts(item: dict) -> list[str]:
    event = item.get("event") if item.get("type") == "context.append_loop_event" else None
    part = event.get("part") if isinstance(event, dict) else None
    if (
        isinstance(event, dict)
        and event.get("type") == "content.part"
        and isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ):
        text = " ".join(part["text"].split())
        return [text] if text else []
    return []


def enrich_brief(item: dict, data: BriefData) -> None:
    if data.first_objective is None:
        objective = next(iter(prompt_texts(item, "system_trigger")), "")
        objective = substantive_user_text(objective)
        data.first_objective = objective or None
    if item.get("type") != "llm.request":
        return
    provider = item.get("provider")
    model = item.get("model") or item.get("modelAlias")
    if (
        isinstance(provider, str)
        and provider
        and (not data.providers or data.providers[-1] != provider)
    ):
        data.providers.append(provider)
    if isinstance(model, str) and model:
        data.latest_model = model


def load_brief(session: Session, poll=None, preview=False) -> BriefData:
    return read_jsonl_brief(
        session,
        user_texts,
        assistant_texts,
        substantive_user_text,
        poll,
        preview,
        enrich_brief,
        turn_events=("turn.ended",),
        compaction_events=("context.apply_compaction",),
    )


def first_prompt(path: Path, origin_kind: str) -> str:
    for item in iter_jsonl(path, 4096):
        text = next(iter(prompt_texts(item, origin_kind)), "")
        if clean := substantive_user_text(text):
            return clean
    return ""


def timestamp(value: object, fallback: float) -> float:
    if not isinstance(value, (int, float)):
        return fallback
    return value / 1000 if value > 10_000_000_000 else float(value)


def agent_reference(session_id: str, agent_id: str) -> str:
    return session_id if agent_id == "main" else f"{session_id}:{agent_id}"


def safe_agent_id(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        return None
    return value


def inspect_bundle(
    state_path: Path, progress: ScanReporter, bundle_size: int | None = None
) -> list[Session]:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        progress.invalid.add(state_path)
        return []
    if not isinstance(state, dict) or state.get("version") not in (None, 2):
        progress.invalid.add(state_path)
        return []
    session_id = state.get("id")
    if not isinstance(session_id, str) or not session_id:
        progress.invalid.add(state_path)
        return []
    bundle = state_path.parent
    main_wire = bundle / "agents" / "main" / "wire.jsonl"
    if not main_wire.is_file():
        progress.invalid.add(state_path)
        return []
    try:
        state_stat = state_path.stat()
        main_stat = main_wire.stat()
    except OSError:
        return []
    cwd = state.get("cwd") if isinstance(state.get("cwd"), str) else "(unknown)"
    archived = state.get("archived") is True
    agents = state.get("agents")
    agents = agents if isinstance(agents, dict) else {}
    child_rows: list[Session] = []
    child_bytes = 0
    for raw_agent_id, raw_metadata in agents.items():
        agent_id = safe_agent_id(raw_agent_id)
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        if agent_id in (None, "main"):
            continue
        agent_dir = bundle / "agents" / agent_id
        wire = agent_dir / "wire.jsonl"
        if not wire.is_file():
            continue
        size = path_size(agent_dir)
        child_bytes += size
        parent_agent = safe_agent_id(metadata.get("parentAgentId"))
        forked_agent = safe_agent_id(metadata.get("forkedFrom"))
        objective = first_prompt(wire, "system_trigger")
        native_label = metadata.get("swarmItem")
        title = (
            objective
            or (native_label.strip() if isinstance(native_label, str) else "")
            or agent_id
        )
        try:
            modified = wire.stat().st_mtime
        except OSError:
            modified = state_stat.st_mtime
        child_rows.append(
            Session(
                wire,
                size,
                modified,
                "kimi",
                "subagent",
                cwd,
                agent_reference(session_id, agent_id),
                agent_reference(session_id, parent_agent) if parent_agent else None,
                title,
                forked_from=(
                    agent_reference(session_id, forked_agent) if forked_agent else ""
                ),
                archived=archived,
                source_home=str(bundle.parents[2]),
            )
        )
    total = path_size(bundle) if bundle_size is None else bundle_size
    title = state.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        fallback = first_prompt(main_wire, "user")
        last_prompt = state.get("lastPrompt")
        fallback = fallback or (
            substantive_user_text(last_prompt) if isinstance(last_prompt, str) else ""
        )
        title = untitled_title(fallback or "untitled")
    forked_from = state.get("forkedFrom")
    root = Session(
        main_wire,
        max(0, total - child_bytes),
        timestamp(state.get("updatedAt"), main_stat.st_mtime),
        "kimi",
        "primary",
        cwd,
        session_id,
        None,
        title,
        forked_from=forked_from if isinstance(forked_from, str) else "",
        archived=archived,
        source_home=str(bundle.parents[2]),
    )
    return [root, *child_rows]


def discover(root: Path, progress: ScanReporter) -> list[Session]:
    state_paths = sorted(root.glob("*/*/state.json"))
    sizes = [path_size(path.parent) for path in state_paths]
    total_bytes = sum(sizes)
    done_bytes = 0
    sessions: list[Session] = []
    for index, (state_path, size) in enumerate(zip(state_paths, sizes), 1):
        found = inspect_bundle(state_path, progress, size)
        if not found:
            progress.skipped.add(state_path)
        sessions.extend(found)
        done_bytes += size
        progress.update("Kimi", index, len(state_paths), done_bytes, total_bytes)
    return sessions
