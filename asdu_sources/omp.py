"""Read-only OMP JSONL sessions. Message parentId is not a session parent."""

from dataclasses import replace
from pathlib import Path

from asdu_sessions import (
    Session,
    SessionCommand,
    SessionControls,
    content_text,
    iter_jsonl,
    message_texts,
    read_jsonl_brief,
    scan_paths,
    untitled_title,
)


def available_actions(_session: Session) -> frozenset[str]:
    return frozenset()


def session_controls(session: Session) -> SessionControls:
    return SessionControls(
        (SessionCommand("Resume", ("omp", "--resume", session.session_id)),)
    )


def user_texts(item):
    message = item.get("message") if item.get("type") == "message" else None
    if (
        isinstance(message, dict)
        and message.get("role") == "user"
        and message.get("attribution") != "agent"
    ):
        yield content_text(message.get("content"))


def clean_user_text(text):
    """Native role/attribution identifies requests, regardless of their length."""
    return " ".join(text.split())


def assistant_texts(item):
    message = item.get("message") if item.get("type") == "message" else None
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    if isinstance(message.get("content"), str):
        return [" ".join(message["content"].split())]
    return message_texts(message)


def discover(root, rules, content_keywords, progress, cache, scope):
    def inspect(path):
        metadata, title, first, reply = {}, "", "", ""
        origin = "primary"
        for index, item in enumerate(iter_jsonl(path, 4096, progress)):
            if item.get("type") == "session":
                metadata = item
                if isinstance(item.get("title"), str) and not title:
                    title = item["title"].strip()
            elif item.get("type") == "title" and isinstance(item.get("title"), str):
                title = item["title"].strip()
            elif (
                item.get("type") == "session_init"
                and isinstance(item.get("agent"), str)
                and item["agent"]
            ):
                origin = "subagent"
            if not first:
                first = next(
                    (
                        clean
                        for text in user_texts(item)
                        if (clean := clean_user_text(text))
                    ),
                    "",
                )
            for text in assistant_texts(item):
                reply = text
            # The title slot precedes the header; session_init follows it.
            if index >= 31 and metadata and (title or first):
                break
        identifier, cwd = metadata.get("id"), metadata.get("cwd")
        parent = metadata.get("parentSession")
        if not isinstance(identifier, str) or not identifier:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return Session(
            path,
            stat.st_size,
            stat.st_mtime,
            "omp",
            origin,
            cwd if isinstance(cwd, str) else "(unknown)",
            identifier,
            parent if isinstance(parent, str) and parent else None,
            title
            or untitled_title(first or (f"reply: {reply}" if reply else "untitled")),
            (),
        )

    sessions = scan_paths(
        "omp",
        "OMP",
        root.rglob("*.jsonl"),
        inspect,
        rules,
        content_keywords,
        progress,
        cache,
        scope,
        user_texts,
        clean_user_text,
    )
    return link_children(sessions)


def link_children(sessions):
    """Match successful task progress IDs, not titles or directory ancestry."""
    by_path = {entry.path: entry for entry in sessions}
    by_id = {entry.session_id: entry for entry in sessions}
    path_ids = {
        value: entry.session_id
        for entry in sessions
        for value in (str(entry.path), str(entry.path.resolve()))
    }
    parents = {}
    for entry in sessions:
        artifacts = (
            entry.path.parent
            if entry.origin == "subagent"
            else entry.path.with_suffix("")
        )
        for item in iter_jsonl(entry.path):
            message = item.get("message")
            if (
                not isinstance(message, dict)
                or message.get("role") != "toolResult"
                or message.get("toolName") != "task"
            ):
                continue
            details = message.get("details")
            progress = details.get("progress", []) if isinstance(details, dict) else []
            if not isinstance(progress, list):
                continue
            for task in progress:
                identifier = task.get("id") if isinstance(task, dict) else None
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or "/" in identifier
                    or "\\" in identifier
                ):
                    continue
                child = by_path.get(artifacts / f"{identifier}.jsonl")
                if (
                    child is not None
                    and child.origin == "subagent"
                    and child.key != entry.key
                ):
                    parents.setdefault(child.storage_key, set()).add(entry.session_id)
    result = []
    for entry in sessions:
        recorded = entry.parent_id
        if recorded in by_id:
            parent_id = recorded
        elif recorded:
            parent_id = path_ids.get(str(Path(recorded).expanduser()))
        else:
            parent_id = None
        inferred = parents.get(entry.storage_key, ())
        if len(inferred) == 1:
            parent_id = next(iter(inferred))
        result.append(replace(entry, parent_id=parent_id))
    return result


def enrich_brief(item, data):
    message = item.get("message") if item.get("type") == "message" else None
    if isinstance(message, dict) and message.get("role") == "assistant":
        provider = message.get("provider")
        model = message.get("model")
        if (
            isinstance(provider, str)
            and provider
            and (not data.providers or data.providers[-1] != provider)
        ):
            data.providers.append(provider)
        if isinstance(model, str) and model:
            data.latest_model = model
    if item.get("type") == "session_init":
        agent = item.get("agent")
        if isinstance(agent, str) and agent:
            data.recorded_via.append(f"OMP task agent: {agent}")


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
