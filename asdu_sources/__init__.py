"""Explicit registry and dispatch for built-in transcript sources."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from asdu_sessions import (
    ActionResult,
    BriefData,
    ScanReporter,
    Session,
    SessionControls,
)

from . import antigravity, claude, codex, gemini, hermes, kimi, omp, opencode

READERS = {
    "codex": codex,
    "claude": claude,
    "hermes": hermes,
    "gemini": gemini,
    "kimi": kimi,
    "omp": omp,
    "opencode": opencode,
    "agy": antigravity,
}


def available_actions(session: Session) -> frozenset[str]:
    actions = getattr(READERS[session.source], "available_actions", None)
    return actions(session) if callable(actions) else frozenset()


def action_scope(session: Session, action: str) -> str:
    """Return whether asdu or the source owns descendant traversal."""
    scope = getattr(READERS[session.source], "action_scope", None)
    return scope(session, action) if callable(scope) else "selectable-tree"


@dataclass(frozen=True)
class SourceAdapter:
    root: Path
    scan: Callable[..., list[Session]]


def source_adapters(roots: dict[str, Path]) -> dict[str, SourceAdapter]:
    """Bind configured storage locations to their native readers."""
    return {
        name: SourceAdapter(root, READERS[name].discover)
        for name, root in roots.items()
    }


def scan(
    sources: tuple[str, ...],
    adapters: dict[str, SourceAdapter],
    progress: ScanReporter,
) -> list[Session]:
    sessions: list[Session] = []
    for source in sources:
        adapter = adapters[source]
        if adapter.root.exists():
            sessions.extend(adapter.scan(adapter.root, progress))
    return sessions


def read_brief(session: Session, poll=None, preview: bool = False) -> BriefData:
    return READERS[session.source].load_brief(session, poll, preview)


def session_controls(session: Session) -> SessionControls:
    controls = getattr(READERS[session.source], "session_controls", None)
    return controls(session) if callable(controls) else SessionControls()


def perform_session_action(session: Session, action: str) -> ActionResult:
    """Delegate one reviewed mutation to the source that owns the format."""
    prepare_session_actions([session], action)
    return perform_prepared_action(session, action)


def prepare_session_actions(sessions: list[Session], action: str) -> None:
    """Validate a complete action scope before its first mutation."""
    if not sessions:
        return
    source = sessions[0].source
    if any(session.source != source for session in sessions):
        raise OSError("a session action cannot cross sources")
    validator = getattr(READERS[source], "prepare_actions", None)
    if callable(validator):
        validator(sessions, action)


def perform_prepared_action(session: Session, action: str) -> ActionResult:
    """Run one source action after its complete scope has been validated."""
    if action not in available_actions(session):
        raise OSError(f"{session.source} does not support {action}")
    handler = getattr(READERS[session.source], "perform_action", None)
    if not callable(handler):
        raise OSError(f"{session.source} does not support {action}")
    return handler(session, action)
