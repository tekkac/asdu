"""Explicit registry and dispatch for built-in transcript sources."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from asdu_sessions import (
    ActionResult,
    BriefData,
    ContentCache,
    ScanReporter,
    Session,
    SessionControls,
    TagRule,
    transcript_keywords as mine_transcript_keywords,
)

from . import claude, codex, omp

READERS = {"codex": codex, "claude": claude, "omp": omp}


def available_actions(session: Session) -> frozenset[str]:
    actions = getattr(READERS[session.source], "available_actions", None)
    return actions(session) if callable(actions) else frozenset()


@dataclass(frozen=True)
class SourceAdapter:
    root: Path
    scan: Callable[..., list[Session]]


def source_adapters(
    codex_root: Path, claude_root: Path, omp_root: Path | None = None
) -> dict[str, SourceAdapter]:
    roots = {"codex": codex_root, "claude": claude_root}
    if omp_root is not None:
        roots["omp"] = omp_root
    return {
        name: SourceAdapter(root, READERS[name].discover)
        for name, root in roots.items()
    }


def scan(
    sources: tuple[str, ...],
    adapters: dict[str, SourceAdapter],
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanReporter,
    cache: ContentCache,
    scope: Path | None,
) -> list[Session]:
    sessions: list[Session] = []
    for source in sources:
        adapter = adapters[source]
        if adapter.root.exists():
            sessions.extend(
                adapter.scan(
                    adapter.root, rules, content_keywords, progress, cache, scope
                )
            )
    cache.save()
    return sessions


def read_brief(session: Session, poll=None, preview: bool = False) -> BriefData:
    return READERS[session.source].load_brief(session, poll, preview)


def session_controls(session: Session) -> SessionControls:
    controls = getattr(READERS[session.source], "session_controls", None)
    return controls(session) if callable(controls) else SessionControls()


def transcript_keywords(
    path: Path,
    source: str,
    keywords: set[str],
    report_bytes: Callable[[int], None] | None = None,
) -> set[str]:
    reader = READERS[source]
    return mine_transcript_keywords(
        path,
        reader.user_texts,
        reader.clean_user_text,
        keywords,
        report_bytes,
    )


def perform_session_action(session: Session, action: str) -> ActionResult:
    """Delegate one reviewed mutation to the source that owns the format."""
    if action not in available_actions(session):
        raise OSError(f"{session.source} sessions are read-only for {action}")
    handler = getattr(READERS[session.source], "perform_action", None)
    if not callable(handler):
        raise OSError(f"{session.source} sessions are read-only for {action}")
    return handler(session, action)
