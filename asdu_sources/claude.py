"""Claude JSONL discovery and message parsing."""

from pathlib import Path
from typing import Iterable

from asdu_sessions import (
    ContentCache,
    ScanReporter,
    Session,
    TagRule,
    iter_jsonl,
    message_texts,
    read_jsonl_brief,
    scan_paths,
    substantive_user_text,
    untitled_title,
)


def discover(
    root: Path,
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanReporter,
    cache: ContentCache,
    scope: Path | None,
) -> list[Session]:
    """Small native Claude reader: CWD + session id live in normal JSONL events."""

    def inspect(path: Path) -> Session | None:
        cwd, session_id, origin, parent_id = "(unknown)", path.stem, "unknown", None
        title = "untitled"
        fallback = ""
        recognized = False
        for item in iter_jsonl(path, 4096, progress):
            if isinstance(item.get("cwd"), str):
                cwd = item["cwd"]
            if isinstance(item.get("sessionId"), str):
                session_id = item["sessionId"]
                recognized = True
            if item.get("isSidechain") is False and origin != "sidechain":
                origin = "primary"
            if item.get("isSidechain") is True:
                # Claude exposes message parents, not a parent session.
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
            (),
        )

    return scan_paths(
        "claude",
        "Claude",
        root.rglob("*.jsonl"),
        inspect,
        rules,
        content_keywords,
        progress,
        cache,
        scope,
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


def load_brief(session, poll=None, preview=False):
    return read_jsonl_brief(session, poll, preview, enrich_brief)
