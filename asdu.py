#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["send2trash>=1.8.3"]
# ///
"""Disk-oriented browser for local agent-session transcripts.

Scanning and browsing are read-only.  Archive and Trash are deliberate,
interactive actions for individual transcripts.
"""

from __future__ import annotations

import argparse
import curses
import gzip
import json
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
import tomllib
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable


ASCII_UI = False

DEFAULT_TAGS = [
    {
        "name": "research",
        "keywords": ["literature review", "research question", "proof sketch", "arxiv"],
    },
    {
        "name": "tooling",
        "keywords": ["mcp server", "agent skill", "plugin", "command line tool"],
    },
    {
        "name": "operations",
        "keywords": ["deployment", "docker", "kubernetes", "terraform"],
    },
    {
        "name": "security",
        "keywords": [
            "security audit",
            "vulnerability",
            "threat model",
            "cryptographic",
        ],
    },
    {
        "name": "development",
        "keywords": ["implement", "test failure", "debug", "code review"],
    },
    {
        "name": "data",
        "keywords": [
            "sql query",
            "data pipeline",
            "dataset",
            "jupyter notebook",
            "dataframe",
        ],
    },
    {
        "name": "documentation",
        "keywords": [
            "write documentation",
            "update readme",
            "release notes",
            "documentation guide",
            "api reference",
        ],
    },
]
ALL_SESSIONS = "__asdu_all_sessions__"


@dataclass(frozen=True)
class TagRule:
    name: str
    paths: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Session:
    path: Path
    size: int
    modified: float
    source: str
    origin: str
    cwd: str
    session_id: str
    parent_id: str | None
    title: str
    tags: tuple[str, ...]
    task_path: str = ""
    forked_from: str = ""


class ScanProgress:
    """Startup splash and progress for local session scans."""

    def __init__(
        self,
        enabled: bool,
        render: Callable[[str, int, int, int, int], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.render = render
        self.interactive = enabled and sys.stderr.isatty()
        self.last_percent = -1
        self.frame_rows = 0
        self.last_frame = None
        self.skipped: set[Path] = set()
        self.invalid: set[Path] = set()
        if self.interactive and render is None:
            self.paint("", 0, 0, 0)

    def paint(self, source: str, current: int, done: int, total: int) -> None:
        try:
            width = os.get_terminal_size(sys.stderr.fileno()).columns - 1
        except OSError:
            width = 79
        lines = indexing_lines(width, source, current, done, total)
        if lines == self.last_frame:
            return
        prefix = f"\033[{self.frame_rows}A" if self.frame_rows else ""
        sys.stderr.write(prefix + "\r\033[J\033[1m" + "\n".join(lines) + "\033[0m\n")
        sys.stderr.flush()
        self.frame_rows = len(lines)
        self.last_frame = lines

    def update(
        self, source: str, current: int, total: int, done_bytes: int, total_bytes: int
    ) -> None:
        if not self.enabled or total == 0:
            return
        # Disk usage is the reason for the scan, so the bar tracks bytes
        # rather than treating a thousand tiny rollouts like one huge one.
        done_bytes = min(done_bytes, total_bytes)
        percent = (
            int(done_bytes * 100 / total_bytes)
            if total_bytes
            else int(current * 100 / total)
        )
        if self.render is not None:
            self.render(source, current, total, done_bytes, total_bytes)
            return
        if self.interactive:
            self.paint(source, current, done_bytes, total_bytes)
            return
        # Do not flood redirected output; a terminal gets smooth updates.
        if not self.interactive and percent < 100 and percent == self.last_percent:
            return
        self.last_percent = percent
        try:
            columns = os.get_terminal_size(sys.stderr.fileno()).columns
        except OSError:
            columns = 80
        suffix = f" {percent:3d}% {source} {current:,}/{total:,} {human_size(done_bytes)}/{human_size(total_bytes)}"
        bar_width = max(0, min(26, columns - len(suffix) - 3))
        if bar_width >= 8:
            line = f"{progress_bar(percent, bar_width)}{suffix}"
        else:
            line = suffix.strip()
        line = line[: max(1, columns - 1)]
        print(line, file=sys.stderr, flush=True)

    def finish(self) -> None:
        if self.interactive:
            sys.stderr.flush()

    def notice(self) -> str:
        parts = []
        if self.skipped:
            parts.append(f"{len(self.skipped)} files skipped")
        if damaged := self.invalid - self.skipped:
            parts.append(f"{len(damaged)} files contain invalid records")
        return "; ".join(parts)


class ContentCache:
    """Cache source-aware user-message keyword results outside session stores."""

    EXTRACTOR_VERSION = 2

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.path = cache_home / "asdu" / "content-keywords-v2.json"
        self.entries: dict[str, dict[str, object]] = {}
        if not enabled:
            return
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            self.entries = data.get("entries", {}) if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass

    def get(self, path: Path, source: str, keywords: set[str]) -> set[str] | None:
        if not self.enabled:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        entry = self.entries.get(str(path))
        if (
            not isinstance(entry, dict)
            or entry.get("size") != stat.st_size
            or entry.get("mtime_ns") != stat.st_mtime_ns
        ):
            return None
        if (
            entry.get("source") != source
            or entry.get("extractor_version") != self.EXTRACTOR_VERSION
            or entry.get("keywords") != sorted(keywords)
            or not isinstance(entry.get("matches"), list)
        ):
            return None
        return {value for value in entry["matches"] if isinstance(value, str)}

    def put(
        self, path: Path, source: str, keywords: set[str], matches: set[str]
    ) -> None:
        if not self.enabled:
            return
        try:
            stat = path.stat()
        except OSError:
            return
        self.entries[str(path)] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "source": source,
            "extractor_version": self.EXTRACTOR_VERSION,
            "keywords": sorted(keywords),
            "matches": sorted(matches),
        }

    def save(self) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"entries": self.entries}, handle, separators=(",", ":"))
            temporary.replace(self.path)
        except OSError:
            pass


def human_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    raise AssertionError("unreachable")


def session_date(session: Session) -> str:
    seconds = max(0, int(time.time() - session.modified))
    if seconds < 10:
        return "now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 30 * 86400:
        return f"{seconds // 86400}d ago"
    if seconds < 365 * 86400:
        return f"{seconds // (30 * 86400)}mo ago"
    return f"{seconds // (365 * 86400)}y ago"


def ascii_ui() -> bool:
    try:
        "╭█".encode(sys.stderr.encoding or "utf-8")
    except UnicodeEncodeError:
        return True
    return ASCII_UI


def terminal_art(text: str) -> str:
    """Keep content intact while replacing terminal decoration in ASCII mode."""
    if ascii_ui():
        return text.translate(
            str.maketrans(
                {
                    **dict.fromkeys("╭╮╰╯├┌└┐┘┤╔╗╚╝╠╣┏┓┗┛┣┫", "+"),
                    **dict.fromkeys("─━═", "-"),
                    **dict.fromkeys("│┃║", "|"),
                    "↑": "^",
                    "↓": "v",
                    "←": "<",
                    "→": ">",
                    "▸": ">",
                    "›": ">",
                    "▾": "v",
                    "…": "~",
                    "·": "/",
                }
            )
        )
    return text


def size_bar(size: int, largest: int, width: int = 12) -> str:
    """Fixed-cell relative usage, with eighth-cell precision for small items."""
    width = max(0, width)
    units = (
        min(width * 8, max(1, size * width * 8 // largest))
        if size > 0 and largest > 0 and width
        else 0
    )
    filled, fraction = divmod(units, 8)
    bar = "█" * filled + ("▏▎▍▌▋▊▉"[fraction - 1] if fraction else "")
    bar += "░" * (width - len(bar))
    if ascii_ui():
        filled = min(width, filled + bool(fraction))
        return "[" + "#" * filled + " " * (width - filled) + "]"
    return bar


def progress_bar(percent: int, width: int) -> str:
    filled = min(width, max(0, width * percent // 100))
    return "[" + ("#" if ascii_ui() else "=") * filled + " " * (width - filled) + "]"


def splash_lines(width: int) -> list[str]:
    if width < 32:
        return []
    return [
        terminal_art(line)
        for line in (
            "╭──────────────────────────────╮",
            "│    __ _  ___  __| | _  _     │",
            "│   / _\x60 |(_-< / _\x60 || || |    │",
            "╰───\\__,_|/__/ \\__,_| \\_,_|────╯",
            "    agent session disk usage    ",
        )
    ]


def indexing_lines(
    width: int, source: str, current: int, done: int, total: int
) -> list[str]:
    percent = min(100, max(0, done * 100 // total)) if total else 0
    if width < 32:
        return [compact_text(f"indexing {source.lower()} {percent}%", max(0, width))]
    count = f"{current:,} sessions"
    status = source.lower() or "discovering"
    row = lambda text: terminal_art(
        "│ " + pad_display(compact_text(text, 28), 28) + " │"
    )
    return [
        *splash_lines(width),
        " " * 32,
        terminal_art("╭─ indexing ───────────────────╮"),
        row(
            pad_display(
                compact_text(status, max(0, 27 - len(count))), max(0, 28 - len(count))
            )
            + count
        ),
        row(f"{progress_bar(percent, 20)}  {percent:3d}%"),
        row(f"{human_size(done)} / {human_size(total)}"),
        terminal_art("╰──────────────────────────────╯"),
    ]


def compact_path(path: str, width: int) -> str:
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1) :]


def compact_text(text: str, width: int) -> str:
    """Truncate prose from the right; its opening words carry the context."""
    if width <= 0:
        return ""
    text = "".join(c if c.isprintable() else " " for c in text)
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for character in text:
        character_width = display_width(character)
        if used + character_width > max(0, width - 1):
            break
        kept.append(character)
        used += character_width
    return "".join(kept) + "…"


def display_width(text: str) -> int:
    """Approximate terminal cell width without a runtime dependency."""
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def session_label(session: Session) -> str:
    """Keep an untitled marker for briefs, not the space-constrained list."""
    if session.task_path:
        return session.task_path.rstrip("/").rsplit("/", 1)[-1].replace("_", " ")
    return re.sub(r"^untitled\s+[—-]\s*", "", session.title, count=1) or session.title


def origin_label(origin: str) -> str:
    return {
        "primary": "main",
        "subagent": "child",
        "sidechain": "side",
        "automation": "auto",
        "user": "user",
        "review": "review",
        "ide": "ide",
    }.get(origin, origin)


def load_tag_rules(config: Path | None) -> list[TagRule]:
    # Built-ins are small, portable, and high-confidence.  A TOML file
    # deliberately replaces them with stable, user-owned rules.
    raw_rules: list[dict] = DEFAULT_TAGS
    if config is not None:
        with config.open("rb") as handle:
            data = tomllib.load(handle)
        raw_rules = data.get("tag", [])
        if not isinstance(raw_rules, list):
            raise ValueError("config key 'tag' must be an array of tables")

    rules: list[TagRule] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            raise ValueError("each tag must be a table")
        for field in ("paths", "keywords"):
            values = item.get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise ValueError(f"tag {field} must be a list of strings")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError("every tag needs a non-empty name")
        rules.append(
            TagRule(
                name=name,
                paths=tuple(str(value).lower() for value in item.get("paths", [])),
                keywords=tuple(
                    str(value).lower() for value in item.get("keywords", [])
                ),
            )
        )
    return rules


def load_titles(codex_home: Path) -> dict[str, str]:
    index = codex_home / "session_index.jsonl"
    titles: dict[str, str] = {}
    for item in iter_jsonl(index):
        session_id = item.get("id")
        title = item.get("thread_name")
        if isinstance(session_id, str) and isinstance(title, str) and title.strip():
            titles[session_id] = title.strip()
    return titles


def iter_jsonl(
    path: Path, limit: int | None = None, progress: ScanProgress | None = None
) -> Iterable[dict[str, object]]:
    """Yield valid JSON object records; changing transcripts remain harmless."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if limit is not None and index >= limit:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    if progress is not None:
                        progress.invalid.add(path)
                    continue
                if isinstance(item, dict):
                    yield item
                elif progress is not None:
                    progress.invalid.add(path)
    except OSError:
        return


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
    path: Path, details: dict | None = None, progress: ScanProgress | None = None
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
    """Use the first real user request when session_index lacks a title.

    Codex serializes environment, skill, and AGENTS.md material as user-role
    messages too, so those preambles are deliberately skipped.  The bounded
    scan keeps an inventory fast even when a rollout is hundreds of megabytes.
    """
    fallback = ""
    for item in iter_jsonl(path, 4096):
        for text in user_texts("codex", item):
            title = substantive_user_text(text)
            if title:
                return title[:90]
        payload = item.get("payload")
        if (
            item.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("role") == "assistant"
        ):
            texts = message_texts(payload)
            if texts:
                fallback = texts[-1]
    return f"reply: {fallback}" if fallback else "untitled"


def untitled_title(first_request: str) -> str:
    """Label a generated preview without pretending the session was titled."""
    if first_request == "untitled":
        return first_request
    return f"untitled — {first_request}"[:90]


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict)
        )
    return ""


def message_texts(payload: dict) -> list[str]:
    content = payload.get("content", [])
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {
            "input_text",
            "output_text",
            "text",
        }:
            continue
        text = block.get("text")
        if isinstance(text, str):
            clean = " ".join(text.split())
            if clean:
                texts.append(clean)
    return texts


def is_real_user_text(text: str) -> bool:
    lowered = text.lower()
    ignored = (
        "<",
        "# agents",
        "here is a list of plugins",
        "you are codex",
        "each workspace has a .context",
        "do not rename the current branch",
        "by default, the user will only see",
        "respond directly to the user's prompt",
    )
    return len(text) >= 12 and not lowered.startswith(ignored)


def substantive_user_text(text: str) -> str:
    """Remove common agent-environment wrappers before using a user request."""
    text = re.sub(
        r"<system_instruction>.*?</system_instruction>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    clean = " ".join(text.split())
    return clean if is_real_user_text(clean) else ""


def digest(session: Session, poll: Callable[[], None] | None = None) -> str:
    try:
        return read_digest(session, poll)
    except OSError as error:
        return f"Transcript unavailable: {error.strerror or error}. Press r in the list to rescan."


def read_digest(session: Session, poll: Callable[[], None] | None = None) -> str:
    """Create a compact plain-text brief without calling a model."""
    first_user: str | None = None
    latest_user: str | None = None
    latest_reply: str | None = None
    first_objective: str | None = None
    event_counts: Counter[str] = Counter()
    recorded_via: list[str] = []
    task_path, forked_from = session.task_path, session.forked_from

    def remember_user(text: str) -> None:
        nonlocal first_user, latest_user
        if not text:
            return
        first_user = first_user or text
        latest_user = text

    def remember_reply(text: str) -> None:
        nonlocal latest_reply
        if text:
            latest_reply = text

    with session.path.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if poll is not None and index % 128 == 0:
                poll()
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            event_counts[str(item.get("type", "unknown"))] += 1
            if session.source == "codex" and item.get("type") == "session_meta":
                payload = item.get("payload")
                if isinstance(payload, dict):
                    task_path, forked_from = task_metadata(payload)
                    source = payload.get("source")
                    originator = payload.get("originator")
                    provider = payload.get("model_provider")
                    if source == "vscode" or originator == "codex_vscode":
                        recorded_via.append("VS Code")
                    elif source == "exec":
                        recorded_via.append("Codex Exec")
                    if originator == "codex_sdk_ts":
                        recorded_via.append("Codex SDK (TypeScript)")
                    elif originator == "codex_exec":
                        recorded_via.append("Codex Exec")
                    if isinstance(provider, str) and provider:
                        recorded_via.append(f"provider: {provider}")
            elif session.source == "claude":
                entrypoint = item.get("entrypoint")
                if entrypoint == "claude-vscode":
                    recorded_via.append("VS Code")
                elif isinstance(entrypoint, str) and entrypoint:
                    recorded_via.append(entrypoint)
            if item.get("type") == "event_msg":
                payload = item.get("payload")
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "thread_goal_updated"
                ):
                    goal = payload.get("goal")
                    if isinstance(goal, dict) and isinstance(
                        goal.get("objective"), str
                    ):
                        first_objective = first_objective or " ".join(
                            goal["objective"].split()
                        )
            for text in user_texts(session.source, item):
                remember_user(substantive_user_text(text))
            if session.source == "claude":
                message = item.get("message")
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                content = message.get("content")
                if isinstance(content, str):
                    texts = [" ".join(content.split())]
                elif isinstance(content, list):
                    texts = [
                        " ".join(str(block.get("text", "")).split())
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                else:
                    texts = []
                if role == "assistant":
                    for text in texts:
                        remember_reply(text)
                continue
            if item.get("type") != "response_item":
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            role = payload.get("role")
            texts = message_texts(payload)
            if role == "assistant":
                for text in texts:
                    remember_reply(text)

    def excerpts(items: list[str], limit: int, prefix: str) -> list[str]:
        chosen = items[:limit] if limit > 0 else items
        return [f"{prefix}{item[:500]}" for item in chosen] or [f"{prefix}none found"]

    events = sum(event_counts.values())
    turns = event_counts["turn_context"]
    messages = sum(event_counts[kind] for kind in ("message", "user", "assistant"))
    compactions = event_counts["compacted"] + event_counts["context_compacted"]
    activity = f"{turns:,} turns" if turns else f"{messages:,} messages"
    initial = (
        [first_objective] if first_objective else [first_user] if first_user else []
    )
    initial_label = "Initial objective" if first_objective else "First request"
    if forked_from:
        initial_label += " (may be inherited)"
    provenance = list(dict.fromkeys(recorded_via))
    metadata = [f"Recorded via: {' · '.join(provenance)}"] if provenance else []
    if task_path:
        metadata.append(f"Task: {task_path}")
    if forked_from:
        metadata.append(f"Forked from: {forked_from}")
    if session.parent_id and session.parent_id != forked_from:
        # The stored parent link supports tree navigation.  It is not evidence
        # that this was created using a conversation-fork operation.
        metadata.append(f"Parent session: {session.parent_id}")
    resume = resume_command(session)
    lines = [
        f"{activity} across {events:,} events; {compactions:,} compactions.",
        f"ID: {session.session_id}",
        f"Session type: {origin_label(session.origin)}",
        *metadata,
        "",
        f"╭ {initial_label}",
        *excerpts(initial, 0, "│ "),
        "",
        "├ Latest request",
        *excerpts([latest_user] if latest_user else [], 0, "│ "),
        "",
        "├ Last reply",
        *excerpts([latest_reply] if latest_reply else [], 0, "│ "),
        "╰",
        "",
        f"Folder: {session.cwd}",
        f"Tags: {', '.join(session.tags)}",
        *([f"Resume: {resume}"] if resume else []),
    ]
    return "\n".join(lines)


def resume_command(session: Session) -> str | None:
    """Return a copyable native resume command; never launch another agent."""
    command = {"codex": "codex resume", "claude": "claude --resume"}.get(session.source)
    if command is None:
        return None
    return f"{command} {shlex.quote(session.session_id)}"


def user_texts(source: str, item: dict[str, object]) -> Iterable[str]:
    """Emit searchable user text, never metadata, tools, or injected context."""
    if source == "codex":
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
        return

    if source == "claude":
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
        elif (
            item.get("type") == "queue-operation" and item.get("operation") == "enqueue"
        ):
            content = item.get("content")
            if isinstance(content, str):
                yield content
        return


def transcript_keywords(
    path: Path,
    source: str,
    keywords: set[str],
    report_bytes: Callable[[int], None] | None = None,
) -> set[str]:
    """Mine only source-recognized user messages, streaming one JSONL file."""
    remaining = {keyword.lower() for keyword in keywords if keyword}
    found: set[str] = set()
    if not remaining:
        return found
    matcher = re.compile(
        "|".join(
            re.escape(keyword) for keyword in sorted(remaining, key=len, reverse=True)
        ),
        re.IGNORECASE,
    )
    byte_keywords = tuple(keyword.encode("utf-8").lower() for keyword in remaining)
    try:
        with path.open("rb") as handle:
            scanned, next_report = 0, 1024 * 1024
            for raw_line in handle:
                scanned += len(raw_line)
                if report_bytes is not None and scanned >= next_report:
                    report_bytes(scanned)
                    next_report = scanned + 1024 * 1024
                # Regex filtering is cheap and means metadata/tool records are
                # normally never decoded.  Extraction below remains the safety
                # boundary: a raw match alone can never create a tag.
                lowered = raw_line.lower()
                if not any(keyword in lowered for keyword in byte_keywords):
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                for text in user_texts(source, item):
                    clean = substantive_user_text(text)
                    matched = {
                        match.group(0).lower() for match in matcher.finditer(clean)
                    }
                    found.update(matched)
                    remaining.difference_update(matched)
                if not remaining:
                    break
            if report_bytes is not None:
                report_bytes(scanned)
    except OSError:
        pass
    return found


def cached_transcript_keywords(
    path: Path,
    source: str,
    keywords: set[str],
    cache: ContentCache,
    report_bytes: Callable[[int], None] | None = None,
) -> set[str]:
    cached = cache.get(path, source, keywords)
    if cached is not None:
        return cached
    matches = transcript_keywords(path, source, keywords, report_bytes)
    cache.put(path, source, keywords, matches)
    return matches


def classify(
    session: Session, rules: list[TagRule], content_matches: set[str]
) -> tuple[str, ...]:
    haystack = f"{session.cwd} {session.title}".lower()
    tags: list[str] = []
    for rule in rules:
        path_match = any(value in session.cwd.lower() for value in rule.paths)
        keyword_match = any(value in haystack for value in rule.keywords)
        if not keyword_match:
            keyword_match = any(value in content_matches for value in rule.keywords)
        if path_match or keyword_match:
            tags.append(rule.name)
    return tuple(tags) or ("untagged",)


# This is linguistic cleanup, not a topic/category taxonomy.  These words
# occur in requests across unrelated work and therefore cannot name a folder.
INFERENCE_NOISE = frozenset(
    "a about after again all also an and any are as at be been before between by "
    "can check code codex could do does directly for from get give go had has have "
    "help how if in into is it its just let like make me more need no not now of on "
    "only or our out please prompt read really reply respond review see session should "
    "so some task that the their then there these this to too try untitle untitled use "
    "user users using want was we what when where which who why will with work would "
    "you your assistant agent context following implement information message request "
    "system tool tools".split()
)
INFERRED_TAG_LIMIT = 16
INFERRED_MIN_FRACTION = 1 / 80


def inferred_terms(title: str) -> set[tuple[str, ...]]:
    """Return title-derived topic candidates without a category vocabulary."""
    clean = title.lower()
    clean = re.sub(r"^untitled\s+[—-]\s*", "", clean)
    clean = re.sub(r"^reply:\s*", "", clean)
    words = [
        word
        for word in re.findall(r"[^\W\d_][\w-]{2,}", clean, flags=re.UNICODE)
        if word not in INFERENCE_NOISE
        and not any(character.isdigit() for character in word)
    ]
    # A repeated two-word phrase is substantially less likely than a lone verb
    # to be an incidental instruction such as "find" or "check".
    return {tuple(words[index : index + 2]) for index in range(len(words) - 1)}


def inferred_path_terms(cwd: str) -> set[str]:
    """Use repeated project-directory components without assuming any layout."""
    if cwd == "(unknown)":
        return set()
    return {
        component.lower()
        for component in Path(cwd).parts
        if len(component) >= 3
        and not any(character.isdigit() for character in component)
    }


def infer_tags(sessions: list[Session]) -> list[Session]:
    """Cluster repeated title phrases; singleton topics intentionally stay bare."""
    document_terms = [inferred_terms(session.title) for session in sessions]
    document_paths = [inferred_path_terms(session.cwd) for session in sessions]
    counts: Counter[tuple[str, ...]] = Counter(
        term for terms in document_terms for term in terms
    )
    path_counts: Counter[str] = Counter(
        term for terms in document_paths for term in terms
    )
    total = max(1, len(sessions))
    # Phrases need to recur, but terms found across a large share of every
    # conversation are boilerplate rather than a useful topic.  Both bounds
    # are corpus-relative and contain no domain/category vocabulary.
    maximum = max(2, total // 12)
    candidates = {
        term: count for term, count in counts.items() if 2 <= count <= maximum
    }
    path_candidates = {
        term: count for term, count in path_counts.items() if 2 <= count <= total // 8
    }
    provisional: list[Session] = []
    for session, terms, paths in zip(
        sessions, document_terms, document_paths, strict=True
    ):
        # Stable rules are explicit, high-confidence classifications.  Inferred
        # topics fill only the unclassified remainder rather than competing.
        if session.tags != ("untagged",):
            provisional.append(session)
            continue
        available_paths = [term for term in paths if term in path_candidates]
        if available_paths:
            best_path = max(
                available_paths,
                key=lambda term: (
                    path_candidates[term]
                    * math.log((total + 1) / (path_candidates[term] + 1))
                ),
            )
            provisional.append(replace(session, tags=(best_path,)))
            continue
        available = [term for term in terms if term in candidates]
        if not available:
            provisional.append(replace(session, tags=("untagged",)))
            continue
        best = max(
            available,
            key=lambda term: (
                len(term)
                * candidates[term]
                * math.log((total + 1) / (candidates[term] + 1)),
                len(term),
                " ".join(term),
            ),
        )
        provisional.append(replace(session, tags=("-".join(best),)))

    # A tag browser needs a compact set of useful folders, not a folder for
    # every incidental phrase.  Rank only recurring clusters by the disk they
    # organize, then leave the long tail honestly untagged.  The limit is a UI
    # constraint, not a vocabulary of topic names.
    tag_counts = Counter(session.tags[0] for session in provisional)
    tag_sizes = Counter()
    for session in provisional:
        tag_sizes[session.tags[0]] += session.size
    minimum_count = max(6, math.ceil(total * INFERRED_MIN_FRACTION))
    allowed = {
        tag
        for tag, _ in sorted(
            (
                (tag, size)
                for tag, size in tag_sizes.items()
                if tag != "untagged" and tag_counts[tag] >= minimum_count
            ),
            key=lambda item: (-item[1], item[0]),
        )[:INFERRED_TAG_LIMIT]
    }
    return [
        session if session.tags[0] in allowed else replace(session, tags=("untagged",))
        for session in provisional
    ]


def scan_paths(
    source: str,
    label: str,
    paths: Iterable[Path],
    inspect: Callable[[Path], Session | None],
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanProgress,
    cache: ContentCache,
    scope: Path | None,
) -> list[Session]:
    """Apply the same scope, cache, progress, and classification rules to every source."""
    discovered = list(paths)
    keywords = {keyword for rule in rules for keyword in rule.keywords}
    sizes: list[int] = []
    for path in discovered:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            sizes.append(0)
    total_bytes = sum(sizes)
    sessions: list[Session] = []
    done_bytes = 0
    for index, (path, size) in enumerate(zip(discovered, sizes), 1):
        session = inspect(path)
        done_bytes += size
        if session is None:
            progress.skipped.add(path)
            progress.update(label, index, len(discovered), done_bytes, total_bytes)
            continue
        if not in_scope(session, scope):
            if session.cwd == "(unknown)":
                progress.skipped.add(path)
            progress.update(label, index, len(discovered), done_bytes, total_bytes)
            continue
        matches = (
            cached_transcript_keywords(
                path,
                source,
                keywords,
                cache,
                lambda scanned: progress.update(
                    label,
                    index,
                    len(discovered),
                    done_bytes - size + scanned,
                    total_bytes,
                ),
            )
            if content_keywords
            else set()
        )
        sessions.append(replace(session, tags=classify(session, rules, matches)))
        progress.update(label, index, len(discovered), done_bytes, total_bytes)
    return sessions


def scan_codex(
    root: Path,
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanProgress,
    cache: ContentCache,
    scope: Path | None,
) -> list[Session]:
    # The normal root is ~/.codex/sessions, whose direct parent owns
    # session_index.jsonl. Custom roots simply use their direct parent too.
    titles = load_titles(root.parent)

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
        )
        if not in_scope(session, scope):
            return session
        title = (
            session_label(session)
            if session.task_path
            else titles.get(session_id) or untitled_title(derive_title(path))
        )
        return replace(session, title=title)

    return scan_paths(
        "codex",
        "Codex",
        root.rglob("rollout-*.jsonl"),
        inspect,
        rules,
        content_keywords,
        progress,
        cache,
        scope,
    )


def scan_claude(
    root: Path,
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanProgress,
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
            message = item.get("message")
            candidate = (
                next(iter(user_texts("claude", item)), "")
                if title == "untitled"
                else ""
            )
            if isinstance(message, dict) and message.get("role") == "assistant":
                fallback = content_text(message.get("content")) or fallback
            if candidate:
                clean = substantive_user_text(candidate)
                if clean:
                    title = clean[:90]
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


@dataclass(frozen=True)
class SourceAdapter:
    """One local transcript format and the reader that understands it."""

    root: Path
    scan: Callable[..., list[Session]]


def source_adapters(codex_root: Path, claude_root: Path) -> dict[str, SourceAdapter]:
    return {
        "codex": SourceAdapter(codex_root, scan_codex),
        "claude": SourceAdapter(claude_root, scan_claude),
    }


def scan(
    sources: tuple[str, ...],
    adapters: dict[str, SourceAdapter],
    rules: list[TagRule],
    content_keywords: bool,
    progress: ScanProgress,
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


def primary_tag(session: Session) -> str:
    return session.tags[0]


def group_sessions(sessions: Iterable[Session], mode: str) -> dict[str, list[Session]]:
    groups: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        if mode == "tag":
            key = primary_tag(session)
        elif mode == "source":
            key = session.source
        elif mode == "origin":
            key = session.origin
        else:
            key = session.cwd
        groups[key].append(session)
    return groups


def browser_visible_sessions(
    sessions: list[Session], source_filter: str, mode: str, cwd_node: Path
) -> tuple[list[Session], list[Session]]:
    """Apply the TUI's source filter and virtual-folder scope in one place."""
    visible = (
        sessions
        if source_filter == "all"
        else [session for session in sessions if session.source == source_filter]
    )
    return visible, visible if mode == "cwd" else [
        session for session in visible if in_scope(session, cwd_node)
    ]


def item_key(item: tuple[str, str, list[Session]]) -> str:
    kind, name, entries = item
    return row_id(entries[0]) if kind == "session" else f"{kind}:{name}"


def browser_group_items(
    sessions: list[Session], mode: str, sort_by: str
) -> list[tuple[str, str, list[Session]]]:
    """Build virtual group rows; /all sessions is navigation, never a tag."""
    items = [
        ("group", name, entries)
        for name, entries in ordered_groups(group_sessions(sessions, mode), sort_by)
    ]
    if mode == "tag" and sessions:
        items.insert(0, ("group", ALL_SESSIONS, sessions))
    return items


def group_label(name: str, mode: str) -> str:
    """Render virtual and provenance groups without exposing internal keys."""
    if name == ALL_SESSIONS:
        return "all sessions"
    return origin_label(name) if mode == "origin" else name


def sort_label(sort_by: str) -> str:
    return {"size": "size↓", "date": "updated↓", "count": "count↓", "name": "name↑"}[
        sort_by
    ]


def in_scope(session: Session, scope: Path | None) -> bool:
    if scope is None:
        return True
    if session.cwd == "(unknown)":
        return False
    try:
        Path(session.cwd).resolve().relative_to(scope)
        return True
    except ValueError:
        return False


def ordered_groups(
    groups: dict[str, list[Session]], sort_by: str
) -> list[tuple[str, list[Session]]]:
    if sort_by == "name":
        return sorted(groups.items(), key=lambda item: item[0].lower())
    if sort_by == "date":
        return sorted(
            groups.items(),
            key=lambda item: (
                -max(session.modified for session in item[1]),
                item[0].lower(),
            ),
        )
    if sort_by == "count":
        return sorted(groups.items(), key=lambda item: (-len(item[1]), item[0].lower()))
    return sorted(
        groups.items(),
        key=lambda item: (-sum(s.size for s in item[1]), item[0].lower()),
    )


def cwd_listing(
    sessions: Iterable[Session], directory: Path, sort_by: str
) -> list[tuple[str, str, list[Session]]]:
    """Folder rows plus direct session rows, like an ncdu directory view."""
    folders: dict[str, list[Session]] = defaultdict(list)
    direct: list[Session] = []
    for session in sessions:
        if session.cwd == "(unknown)":
            continue
        try:
            relative = Path(session.cwd).resolve().relative_to(directory)
        except ValueError:
            continue
        if relative == Path("."):
            direct.append(session)
        else:
            folders[relative.parts[0]].append(session)
    result = [
        ("folder", name, entries) for name, entries in ordered_groups(folders, sort_by)
    ]
    if sort_by == "name":
        direct.sort(key=lambda session: session_label(session).casefold())
    elif sort_by == "date":
        direct.sort(key=lambda session: session.modified, reverse=True)
    else:
        direct.sort(key=lambda session: session.size, reverse=True)
    result.extend(("session", session.title, [session]) for session in direct)
    return result


def relative_folder(directory: Path, root: Path) -> str:
    try:
        value = directory.relative_to(root)
    except ValueError:
        return directory.name
    return str(value) if str(value) != "." else "."


def print_summary(sessions: list[Session], mode: str, sort_by: str) -> None:
    groups = ordered_groups(group_sessions(sessions, mode), sort_by)
    width = max((len(name) for name, _ in groups), default=10)
    label = {"cwd": "folder", "origin": "session type"}.get(mode, mode)
    print(
        f"{len(sessions):,} sessions  {human_size(sum(s.size for s in sessions))}  grouped by {label}\n"
    )
    print(f"{'group':<{width}}  sessions       size")
    print(f"{'-' * width}  --------  ---------")
    for name, entries in groups:
        print(
            f"{name:<{width}}  {len(entries):>8,}  {human_size(sum(s.size for s in entries)):>10}"
        )


def print_sessions(sessions: list[Session], sort_by: str) -> None:
    ordered = ordered_sessions(sessions, sort_by)
    for session in ordered:
        print(
            f"{human_size(session.size):>10}  {session.source:<6}  {origin_label(session.origin):<6}  {session_label(session)[:30]:<30}  {session_date(session):>8}  "
            f"[{', '.join(session.tags)}]\n"
            f"{'':>11}{session.session_id[:18]}  {session.cwd}\n"
        )


def draw_line(
    window: curses.window,
    row: int,
    text: str,
    selected: bool = False,
    color: int = 0,
    bold: bool = False,
    invert: bool = False,
    striped: bool = False,
    pointer: bool = False,
) -> None:
    height, width = window.getmaxyx()
    if row >= height or width < 2:
        return
    if selected and pointer:
        text = "›" + text[1:]
    text = compact_text(terminal_art(text), width - 1)
    if invert or selected or striped:
        text = pad_display(text, width - 1)
    attr = (
        curses.A_REVERSE
        if selected
        else (
            curses.color_pair(color + 16)
            if striped
            else curses.color_pair(color)
            if color
            else curses.A_NORMAL
        )
    )
    if bold:
        attr |= curses.A_BOLD
    if invert:
        attr |= curses.A_REVERSE
    window.addnstr(row, 0, text, max(0, width - 1), attr)
    for match in re.finditer("░+", text):
        window.addnstr(
            row,
            display_width(text[: match.start()]),
            match.group(),
            len(match.group()),
            attr | curses.A_DIM,
        )


def origin_color(origin: str) -> int:
    return {
        "user": 1,
        "primary": 2,
        "subagent": 3,
        "sidechain": 3,
        "review": 4,
        "automation": 5,
        "ide": 6,
    }.get(origin, 0)


def source_color(source: str) -> int:
    return {"codex": 7, "claude": 8}.get(source, 0)


def draw_session_line(
    window: curses.window,
    row: int,
    session: Session,
    selected: bool,
    branch: str = "",
    stats: tuple[int, int] | None = None,
    striped: bool = False,
) -> None:
    """Keep source identity visible without sacrificing selection contrast."""
    _, width = window.getmaxyx()
    size, descendants = stats if stats is not None else (session.size, 0)
    prefix = f"  {human_size(size):>10}  "
    source = f"{session.source:<6}"
    kind = f"  {origin_label(session.origin):<6}  {terminal_art(branch)}"
    date = f"  {session_date(session):>8}"
    title_width = max(0, width - 1 - display_width(prefix + source + kind + date))
    count = f" (+{descendants})" if descendants else ""
    title = pad_display(
        compact_text(session_label(session), max(0, title_width - len(count))) + count,
        title_width,
    )
    suffix = kind + title + date
    if selected:
        draw_line(window, row, prefix + source + suffix, selected=True, pointer=True)
        return
    _, width = window.getmaxyx()
    limit = max(0, width - 1)
    if striped:
        draw_line(window, row, "", striped=True)
    column = 0
    for text, color in (
        (prefix, 0),
        (source, source_color(session.source)),
        (suffix, origin_color(session.origin)),
    ):
        if column >= limit:
            break
        attr = (
            curses.color_pair(color + 16)
            if striped
            else curses.color_pair(color)
            if color
            else curses.A_NORMAL
        )
        if text == source:
            attr |= curses.A_BOLD
        text = compact_text(text, limit - column)
        window.addnstr(row, column, text, len(text), attr)
        column += display_width(text)


def ordered_sessions(sessions: Iterable[Session], sort_by: str) -> list[Session]:
    if sort_by == "name":
        return sorted(sessions, key=lambda session: session_label(session).casefold())
    if sort_by == "date":
        return sorted(sessions, key=lambda session: session.modified, reverse=True)
    return sorted(sessions, key=lambda session: session.size, reverse=True)


def move_to_trash(path: Path) -> None:
    """Move one transcript to the operating system's recoverable Trash."""
    try:
        from send2trash import send2trash
    except ImportError as error:
        raise OSError("send2trash is required for recoverable deletion") from error
    send2trash(str(path))


def require_unchanged(session: Session) -> None:
    """Refuse actions when a transcript changed since the browser scanned it."""
    try:
        current = session.path.stat()
    except OSError as error:
        raise OSError("session is no longer readable") from error
    if current.st_size != session.size or current.st_mtime != session.modified:
        raise OSError("session changed since scan; rescan before acting")


def archive_session(session: Session) -> Path:
    """Compress a transcript into a dated, user-owned archive and remove it."""
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    if session.source not in {"codex", "claude"}:
        raise OSError("Unsupported archive source")
    directory = data_home / "asdu" / "archive" / day / session.source
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="session-", suffix=".jsonl.gz", dir=directory)
    os.close(fd)
    destination = Path(name)
    try:
        require_unchanged(session)
        with (
            session.path.open("rb") as source,
            gzip.open(destination, "wb") as archived,
        ):
            shutil.copyfileobj(source, archived, length=1024 * 1024)
        require_unchanged(session)
        session.path.unlink()
        return destination
    except OSError:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def action_log_path() -> Path:
    """Return the local, append-only record of successful storage actions."""
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    )
    return state_home / "asdu" / "actions.jsonl"


def record_action(
    action: str, session: Session, destination: Path | None = None
) -> None:
    """Record only action metadata, never transcript content, outside the UI."""
    path = action_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "action": action,
        "source": session.source,
        "session_id": session.session_id,
        "path": str(session.path),
        "size": session.size,
    }
    if destination is not None:
        event["archive"] = str(destination)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, separators=(",", ":")) + "\n")


def delete_settings_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "asdu" / "settings.json"


def skip_delete_confirmation() -> bool:
    try:
        with delete_settings_path().open(encoding="utf-8") as handle:
            return bool(json.load(handle).get("skip_delete_confirmation"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def disable_delete_confirmation() -> None:
    try:
        path = delete_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump({"skip_delete_confirmation": True}, handle)
    except OSError:
        pass


def action_dialog(window, title, body, options, default, shortcuts):
    """Use identical keys and confirmation defaults at every terminal size."""
    selected = options.index(default)
    while True:
        height, width = window.getmaxyx()
        boxed = width >= 44 and height >= len(body) + len(options) + 5
        box_width = min(72, width - 4) if boxed else max(1, width - 1)
        left = (width - box_width) // 2 if boxed else 0
        top = (height - len(body) - len(options) - 4) // 2 if boxed else 0
        if not boxed:
            window.erase()
        inner = box_width - 2
        lines = [title, *body, "", *options]
        for index, line in enumerate(lines):
            row = top + index + int(boxed)
            if row >= height:
                break
            is_option = index >= len(body) + 2
            active = is_option and index - len(body) - 2 == selected
            text = (
                pad_display(compact_text(" " + line, max(0, inner)), max(0, inner))
                if boxed
                else compact_text(line, box_width)
            )
            if boxed:
                text = terminal_art("│" + text + "│")
            window.addnstr(
                row,
                left,
                text,
                box_width,
                curses.A_REVERSE if active else curses.A_NORMAL,
            )
        if boxed:
            window.addnstr(top, left, terminal_art("╭" + "─" * inner + "╮"), box_width)
            window.addnstr(
                top + len(lines) + 1,
                left,
                terminal_art("╰" + "─" * inner + "╯"),
                box_width,
            )
        window.refresh()
        key = window.getch()
        if key in (27, ord("q"), ord("n"), ord("N")):
            return None
        if key in shortcuts:
            return shortcuts[key]
        if key in (curses.KEY_ENTER, 10, 13):
            choice = options[selected]
            return None if choice in ("cancel", "no") else choice
        if key in (curses.KEY_UP, curses.KEY_LEFT, ord("k"), ord("h")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, curses.KEY_RIGHT, ord("j"), ord("l")):
            selected = min(len(options) - 1, selected + 1)


def confirm_session_action(window: curses.window, session: Session) -> str | None:
    return action_dialog(
        window,
        "Session action",
        [session_label(session), "Archive compresses; Trash is recoverable."],
        ["archive", "trash", "cancel"],
        "archive",
        {
            ord(key): choice
            for keys, choice in (("aA", "archive"), ("tT", "trash"))
            for key in keys
        },
    )


def confirm_trash(window: curses.window) -> str | None:
    return action_dialog(
        window,
        "Confirm Trash",
        ["Move this session to system Trash?"],
        ["yes", "no", "don't ask again"],
        "no",
        {ord("y"): "yes", ord("Y"): "yes"},
    )


def row_id(entry: Session) -> str:
    return f"{entry.source}:{entry.path}"


def parent_links(entries: Iterable[Session]) -> dict[str, str]:
    entries = list(entries)
    candidates: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for entry in entries:
        candidates[entry.source, entry.session_id].append(entry)
    links = {}
    for entry in entries:
        parents = candidates.get((entry.source, entry.parent_id), [])
        if len(parents) == 1 and row_id(parents[0]) != row_id(entry):
            links[row_id(entry)] = row_id(parents[0])
    for start in list(links):
        seen = set()
        node = start
        while node in links:
            if node in seen:
                del links[node]
                break
            seen.add(node)
            node = links[node]
    return links


def subtree_stats(entries: Iterable[Session]) -> dict[str, tuple[int, int]]:
    """Physical bytes and descendant counts; shared history is not deduplicated."""
    unique = {row_id(entry): entry for entry in entries}
    links = parent_links(unique.values())
    totals = {key: [entry.size, 0] for key, entry in unique.items()}
    for key, entry in unique.items():
        while key in links:
            key = links[key]
            totals[key][0] += entry.size
            totals[key][1] += 1
    return {key: (size, count) for key, (size, count) in totals.items()}


def brief_sizes(session: Session, entries: Iterable[Session]) -> str:
    total, descendants = subtree_stats(entries).get(row_id(session), (session.size, 0))
    sizes = f"File: {human_size(session.size)}"
    if descendants:
        sizes += f"\nTree: {human_size(total)} including {descendants} descendants in this view"
    return sizes


def session_brief(session: Session, entries: Iterable[Session]) -> str:
    return brief_sizes(session, entries) + "\n\n" + digest(session)


class BriefCancelled(Exception):
    """Return to the browser without finishing the transcript scan."""


def open_brief(
    window: curses.window, session: Session, entries: Iterable[Session]
) -> None:
    """Draw known metadata first; poll for cancellation during the full scan."""
    sizes = brief_sizes(session, entries)
    known = [sizes, f"ID: {session.session_id}", f"Folder: {session.cwd}"]
    if session.task_path:
        known.append(f"Task: {session.task_path}")
    if session.forked_from:
        known.append(f"Forked from: {session.forked_from}")
    if command := resume_command(session):
        known.append(f"Resume: {command}")
    window.erase()
    draw_line(window, 0, session_label(session), bold=True)
    draw_line(window, 1, "Reading conversation…  Backspace return")
    for row, line in enumerate("\n".join(known).splitlines(), 3):
        draw_line(window, row, line)
    window.refresh()

    def poll() -> None:
        if window.getch() in (127, 8, curses.KEY_BACKSPACE, 27, ord("q"), 10, 13):
            raise BriefCancelled

    window.nodelay(True)
    try:
        poll()
        body = digest(session, poll)
        poll()
    except BriefCancelled:
        return
    finally:
        window.nodelay(False)
    text_view(window, session_label(session), sizes + "\n\n" + body)


def session_tree(
    entries: list[Session], sort_by: str, collapsed: set[str] | None = None
) -> list[tuple[Session, str]]:
    """Return a stable, forest-shaped view using native parent thread IDs."""
    collapsed = collapsed or set()
    links = parent_links(entries)
    stats = subtree_stats(entries)
    children: dict[str, list[Session]] = defaultdict(list)
    roots: list[Session] = []
    for entry in entries:
        if row_id(entry) in links:
            children[links[row_id(entry)]].append(entry)
        else:
            roots.append(entry)
    if sort_by == "name":
        key = lambda entry: (session_label(entry).casefold(),)
    elif sort_by == "date":
        key = lambda entry: (-entry.modified, session_label(entry).casefold())
    else:
        key = lambda entry: (-stats[row_id(entry)][0], session_label(entry).casefold())
    roots.sort(key=key)
    for nodes in children.values():
        nodes.sort(key=key)
    result: list[tuple[Session, str]] = []

    def visit(entry: Session, prefix: str, branch: str, seen: set[str]) -> None:
        nodes = children.get(row_id(entry), [])
        marker = "▸ " if nodes and row_id(entry) in collapsed else "▾ " if nodes else ""
        result.append((entry, prefix + branch + marker))
        if row_id(entry) in seen or row_id(entry) in collapsed:
            return
        next_seen = seen | {row_id(entry)}
        child_prefix = (
            prefix
            + ("   " if branch == "└─ " else "│  " if branch == "├─ " else "")
            + ("  " if nodes else "")
        )
        for index, child in enumerate(nodes):
            last = index == len(nodes) - 1
            visit(child, child_prefix, "└─ " if last else "├─ ", next_seen)

    for root in roots:
        visit(root, "", "", set())
    return result


def tree_with_ancestors(
    entries: list[Session], candidates: Iterable[Session]
) -> list[Session]:
    """Add native parents needed to render a selected group as a real tree.

    A tag group is a view, not a conversation boundary: a parent can reasonably
    classify as ``tooling`` while its workers classify as a project tag.  The
    added sessions are structural context only; callers keep ``entries`` for
    group totals and membership.
    """
    candidate_lists: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for entry in candidates:
        candidate_lists[(entry.source, entry.session_id)].append(entry)
    # Session IDs are unique for Codex, but Claude can emit several transcript
    # files for one session.  Only a unique ID is safe to use as a parent link.
    known = {
        key: values[0] for key, values in candidate_lists.items() if len(values) == 1
    }
    result = {(entry.source, str(entry.path)): entry for entry in entries}
    pending = list(entries)
    while pending:
        entry = pending.pop()
        if not entry.parent_id:
            continue
        parent = known.get((entry.source, entry.parent_id))
        key = (parent.source, str(parent.path)) if parent else None
        if parent is not None and key not in result:
            result[key] = parent
            pending.append(parent)
    return list(result.values())


def tree_rows(
    entries: list[Session],
    candidates: Iterable[Session],
    sort_by: str,
    enabled: bool,
    collapsed: set[str],
) -> list[tuple[Session, str]]:
    """Build display rows without curses; useful to both the UI and tests."""
    if not enabled:
        return [(entry, "") for entry in ordered_sessions(entries, sort_by)]
    return session_tree(tree_with_ancestors(entries, candidates), sort_by, collapsed)


def clamp_view(
    selected: int, offset: int, total: int, page_size: int
) -> tuple[int, int]:
    """Keep a selection visible, including empty and shrinking views."""
    selected = min(max(0, total - 1), selected)
    offset = min(offset, max(0, total - page_size))
    if selected < offset:
        offset = selected
    elif selected >= offset + page_size:
        offset = selected - page_size + 1
    return selected, offset


def folded_tree_nodes(entries: Iterable[Session]) -> set[str]:
    """Return parent IDs to fold when a tree view is first opened."""
    return set(parent_links(entries).values())


@dataclass
class BrowserState:
    """Ephemeral per-run UI state, intentionally never serialized."""

    tree_modes: set[tuple[str, str, str, str]]
    tree_folds: dict[tuple[str, str, str, str], set[str]]
    detail_key: tuple[str, str, str, str] | None = None

    @classmethod
    def create(cls) -> BrowserState:
        return cls(set(), {})

    def open_group(
        self,
        mode: str,
        name: str,
        source_filter: str,
        cwd: Path,
        entries: Iterable[Session],
    ) -> tuple[bool, set[str]]:
        key = (mode, name, source_filter, str(cwd))
        self.detail_key = key
        enabled = key in self.tree_modes
        folds = (
            self.tree_folds.setdefault(key, folded_tree_nodes(entries))
            if enabled
            else set()
        )
        return enabled, folds

    def toggle_tree(self, entries: Iterable[Session]) -> tuple[bool, set[str]]:
        if self.detail_key is None:
            return False, set()
        if self.detail_key in self.tree_modes:
            self.tree_modes.remove(self.detail_key)
            return False, set()
        self.tree_modes.add(self.detail_key)
        return True, self.tree_folds.setdefault(
            self.detail_key, folded_tree_nodes(entries)
        )

    def close_group(self) -> None:
        self.detail_key = None


def read_navigation(window: curses.window, first: bool, last: bool) -> int:
    """Ignore repeated boundary keys without rebuilding or redrawing the view."""
    while True:
        key = window.getch()
        if first and key in (
            curses.KEY_UP,
            ord("k"),
            curses.KEY_PPAGE,
            curses.KEY_HOME,
        ):
            continue
        if last and key in (
            curses.KEY_DOWN,
            ord("j"),
            curses.KEY_NPAGE,
            curses.KEY_END,
        ):
            continue
        return key


def find_match(labels: list[str], query: str, start: int, step: int = 1) -> int:
    if query:
        for distance in range(1, len(labels) + 1):
            index = (start + step * distance) % len(labels)
            if query.casefold() in labels[index].casefold():
                return index
    return start


def drain_navigation(window, key, selected, positions):
    """Apply queued arrows in order before drawing; retain the next command."""
    directions = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1}
    if key not in directions or not positions:
        return selected, key
    index = positions.index(selected)
    window.nodelay(True)
    try:
        # Bound a batch so sustained input still gets regular screen updates.
        for _ in range(256):
            index = min(len(positions) - 1, max(0, index + directions[key]))
            key = window.getch()
            if key not in directions:
                return positions[index], key
        curses.ungetch(key)
        return positions[index], -1
    finally:
        window.nodelay(False)


def search_prompt(window: curses.window) -> str:
    height, width = window.getmaxyx()
    draw_line(window, height - 1, "/".ljust(width - 1))
    try:
        curses.echo()
        return window.getstr(height - 1, 1, max(1, width - 3)).decode(
            "utf-8", "replace"
        )
    finally:
        curses.noecho()


def wrap_cells(raw: str, width: int) -> list[str]:
    prefix = "│ " if raw.startswith("│ ") and width > 2 else ""
    text = raw[len(prefix) :]
    lines = []
    line = prefix
    for word in text.split():
        for character in (" " if line != prefix else "") + word:
            if display_width(line + character) > width:
                lines.append(line)
                line = prefix
            if display_width(line + character) <= width:
                line += character
    return lines + [line]


def text_view(window: curses.window, title: str, body: str) -> None:
    """Scrollable pane for a selected session's structural summary."""
    offset = 0
    query = ""
    match_index = -1
    while True:
        window.erase()
        height, width = window.getmaxyx()
        lines: list[str] = []
        for raw in body.splitlines():
            lines.extend(wrap_cells(raw, max(1, width - 1)))
        offset = min(offset, max(0, len(lines) - max(1, height - 2)))
        draw_line(window, 0, title, bold=True)
        for row, line in enumerate(lines[offset : offset + height - 2], 2):
            draw_line(
                window, row, terminal_art(line), bold=line.startswith(("╭ ", "├ ", "╰"))
            )
        window.refresh()
        maximum = max(0, len(lines) - max(1, height - 2))
        key = read_navigation(window, offset == 0, offset >= maximum)
        offset, key = drain_navigation(window, key, offset, range(maximum + 1))
        if key == -1:
            continue
        if key in (
            ord("q"),
            27,
            curses.KEY_BACKSPACE,
            127,
            8,
            curses.KEY_ENTER,
            10,
            13,
        ):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            offset = min(maximum, offset + 1)
        elif key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key == curses.KEY_NPAGE:
            offset = min(maximum, offset + max(1, height - 4))
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - max(1, height - 4))
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = maximum
        elif key in (ord("/"), ord("n"), ord("N")):
            if key == ord("/"):
                query = search_prompt(window)
                match_index = offset - 1
            match_index = find_match(
                lines, query, match_index, -1 if key == ord("N") else 1
            )
            offset = min(maximum, max(0, match_index))


def choose(
    window: curses.window, title: str, options: list[str], current: str
) -> str | None:
    """A small htop-like keyboard chooser for a field with few values."""
    selected = options.index(current) if current in options else 0
    labels = {"cwd": "folder", "origin": "session type", "date": "updated"}
    while True:
        window.erase()
        height, width = window.getmaxyx()
        boxed = width >= 38 and height >= len(options) + 5
        inner = min(48, width - 3)
        if boxed:
            label = " " + compact_text(title, inner - 4) + " "
            draw_line(
                window,
                0,
                terminal_art(
                    "╭─" + label + "─" * (inner - 1 - display_width(label)) + "╮"
                ),
                bold=True,
            )
            draw_line(
                window,
                1,
                terminal_art(
                    "│" + " ↑↓ choose  Enter apply  Esc cancel".ljust(inner) + "│"
                ),
            )
            draw_line(window, 2, terminal_art("│" + " " * inner + "│"))
        else:
            draw_line(window, 0, title, bold=True, invert=True)
            draw_line(window, 1, "↑↓ choose  Enter apply  Esc cancel")
        for row, option in enumerate(options, 3):
            label = labels.get(option, option)
            if boxed:
                text = pad_display(compact_text(" " + label, inner), inner)
                draw_line(window, row, terminal_art("│" + text + "│"))
                if row - 3 == selected:
                    window.addnstr(row, 1, text, len(text), curses.A_REVERSE)
            else:
                draw_line(window, row, label, selected=row - 3 == selected)
        if boxed:
            draw_line(window, len(options) + 3, terminal_art("╰" + "─" * inner + "╯"))
        window.refresh()
        key = window.getch()
        if key in (27, ord("q")):
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            return options[selected]
        if key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(options) - 1, selected + 1)
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)


def tui(
    sessions: list[Session],
    initial_mode: str,
    initial_sort: str,
    scope: Path | None,
    ask_before_delete: bool,
    rescan: Callable[[Callable[[str, int, int, int, int], None]], list[Session]],
    scan_notice: Callable[[], str] = lambda: "",
) -> None:
    """A small ncdu-like drill-down UI with explicit archive/Trash actions."""

    def run(window: curses.window) -> None:
        stripes = False
        curses.curs_set(0)
        # ncdu likewise leaves touchpad/mouse handling to the terminal.
        # Curses' mouse reports differ between terminal emulators and can turn
        # a two-finger gesture into erratic selection movement.
        try:
            curses.mousemask(0)
            curses.mouseinterval(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)  # explicit user
            curses.init_pair(2, curses.COLOR_YELLOW, -1)  # primary
            curses.init_pair(3, curses.COLOR_MAGENTA, -1)  # subagent
            curses.init_pair(4, curses.COLOR_CYAN, -1)  # review
            curses.init_pair(5, curses.COLOR_RED, -1)  # automation
            curses.init_pair(6, curses.COLOR_BLUE, -1)  # IDE-created
            if curses.COLORS >= 256:
                curses.init_pair(7, 33, -1)  # Codex: bright blue
                curses.init_pair(8, 208, -1)  # Claude: orange
            else:
                curses.init_pair(7, curses.COLOR_CYAN, -1)
                curses.init_pair(8, curses.COLOR_YELLOW, -1)
            if curses.COLORS >= 256 and curses.COLOR_PAIRS > 24:
                try:
                    for color in range(9):
                        foreground, _ = curses.pair_content(color)
                        curses.init_pair(color + 16, foreground, 235)
                    stripes = True
                except curses.error:
                    pass
        mode, sort_by, selected, detail, source_filter, tree_mode = (
            initial_mode,
            initial_sort,
            0,
            None,
            "all",
            False,
        )
        detail_return: tuple[str, int] | None = None
        browser = BrowserState.create()
        collapsed_nodes: set[str] = set()
        cwd_root = scope or Path("/")
        cwd_node = cwd_root
        confirm_deletes = ask_before_delete
        offset = 0
        pending_anchor = None
        folder_positions = {}
        query = ""
        status = ""

        def refresh_view() -> None:
            nonlocal \
                detail, \
                detail_return, \
                selected, \
                offset, \
                tree_mode, \
                collapsed_nodes, \
                pending_anchor
            _, current = browser_visible_sessions(
                sessions, source_filter, mode, cwd_node
            )
            if detail is None:
                return
            anchor = row_id(display_entries[selected][0]) if display_entries else None
            name = detail[0]
            entries = (
                [s for s in current if Path(s.cwd).resolve() == cwd_node]
                if mode == "cwd"
                else next(
                    (
                        es
                        for _, key, es in browser_group_items(current, mode, sort_by)
                        if key == name
                    ),
                    [],
                )
            )
            if not entries:
                detail = None
                pending_anchor, offset = detail_return or (None, 0)
                selected = 0
                detail_return = None
                browser.close_group()
                return
            detail = (name, entries)
            tree_mode, collapsed_nodes = browser.open_group(
                mode,
                name,
                source_filter,
                cwd_node,
                tree_with_ancestors(entries, current),
            )
            rows = tree_rows(entries, current, sort_by, tree_mode, collapsed_nodes)
            selected = next(
                (i for i, (entry, _) in enumerate(rows) if row_id(entry) == anchor),
                selected,
            )
            selected, offset = clamp_view(selected, offset, len(rows), page_size)

        while True:
            window.erase()
            height, width = window.getmaxyx()
            page_size = max(1, height - 4)
            visible, grouped_visible = browser_visible_sessions(
                sessions, source_filter, mode, cwd_node
            )
            if detail is not None and not detail[1]:
                detail = None
                pending_anchor, offset = detail_return or (None, 0)
                selected = 0
                detail_return = None
                continue
            if detail is None:
                if mode == "cwd":
                    items = cwd_listing(visible, cwd_node, sort_by)
                    direct = [es[0] for kind, _, es in items if kind == "session"]
                    tree_entries = tree_with_ancestors(direct, visible)
                    tree_mode, collapsed_nodes = browser.open_group(
                        mode, str(cwd_node), source_filter, cwd_node, tree_entries
                    )
                    folder_rows = [item for item in items if item[0] != "session"]
                    session_rows = tree_rows(
                        direct, visible, sort_by, tree_mode, collapsed_nodes
                    )
                    branches = {row_id(entry): branch for entry, branch in session_rows}
                    items = folder_rows + [
                        ("session", entry.title, [entry]) for entry, _ in session_rows
                    ]
                    if pending_anchor is not None and tree_mode:
                        links = parent_links(tree_entries)
                        shown = {item_key(item) for item in items}
                        while pending_anchor not in shown and pending_anchor in links:
                            pending_anchor = links[pending_anchor]
                else:
                    tree_mode = False
                    items = browser_group_items(grouped_visible, mode, sort_by)
                if pending_anchor is not None:
                    selected = next(
                        (
                            i
                            for i, item in enumerate(items)
                            if item_key(item) == pending_anchor
                        ),
                        selected,
                    )
                    pending_anchor = None
                selected, offset = clamp_view(selected, offset, len(items), page_size)
                if mode == "cwd":
                    location = relative_folder(cwd_node, cwd_root)
                else:
                    location = {
                        "tag": "tags",
                        "source": "sources",
                        "origin": "session types",
                    }.get(mode, mode)
                stats = (
                    subtree_stats(tree_entries) if mode == "cwd" and tree_mode else {}
                )
                gap = next(
                    (
                        i
                        for i in range(1, len(items))
                        if items[i][0] == "session" and items[i - 1][0] != "session"
                    ),
                    0,
                )
                if selected - offset + int(offset < gap <= selected) >= page_size:
                    offset += 1
                largest_group = max(
                    (sum(item.size for item in entries) for _, _, entries in items),
                    default=0,
                )
                for index, (kind, name, entries) in enumerate(
                    items[offset : offset + page_size], offset
                ):
                    row = index - offset + 2 + int(offset < gap <= index)
                    if row >= height - 2:
                        break
                    display_name = (
                        "/" + name if kind == "folder" else group_label(name, mode)
                    )
                    group_size = sum(item.size for item in entries)
                    if kind == "session":
                        draw_session_line(
                            window,
                            row,
                            entries[0],
                            index == selected,
                            branches.get(row_id(entries[0]), "")
                            if mode == "cwd"
                            else "",
                            stats.get(row_id(entries[0])),
                            striped=stripes and bool(index % 2),
                        )
                        continue
                    else:
                        name_width = max(1, width - 24 - len(size_bar(0, 1)))
                        display = compact_text(display_name, name_width)
                        text = f"{human_size(group_size):>10}  {size_bar(group_size, largest_group)}  {len(entries):>5}  {pad_display(display, name_width)}"
                        color = origin_color(name) if mode == "origin" else 0
                    draw_line(
                        window,
                        row,
                        "  " + text,
                        index == selected,
                        color,
                        striped=stripes and bool(index % 2),
                        pointer=True,
                    )
                total = len(items)
            else:
                name, entries = detail
                tree_entries = (
                    tree_with_ancestors(entries, grouped_visible)
                    if tree_mode
                    else entries
                )
                display_entries = tree_rows(
                    entries, grouped_visible, sort_by, tree_mode, collapsed_nodes
                )
                if pending_anchor is not None:
                    selected = next(
                        (
                            i
                            for i, (entry, _) in enumerate(display_entries)
                            if row_id(entry) == pending_anchor
                        ),
                        selected,
                    )
                    pending_anchor = None
                # Keep a hierarchy alongside the flattened rendering so tree
                # mode can move between siblings and parent/child nodes.
                display_index = {
                    row_id(entry): index
                    for index, (entry, _) in enumerate(display_entries)
                }
                links = parent_links(tree_entries)
                entry_ids = set(display_index)
                entry_children: dict[str, list[str]] = defaultdict(list)
                for entry in tree_entries:
                    parent_id = links.get(row_id(entry))
                    if parent_id in entry_ids:
                        entry_children[parent_id].append(row_id(entry))
                tree_parents: dict[int, int | None] = {}
                tree_children: dict[int | None, list[int]] = defaultdict(list)
                for index, (entry, _) in enumerate(display_entries):
                    parent = (
                        display_index.get(links.get(row_id(entry)))
                        if tree_mode
                        else None
                    )
                    tree_parents[index] = parent
                    tree_children[parent].append(index)
                selected, offset = clamp_view(
                    selected, offset, len(display_entries), page_size
                )
                location = group_label(name, mode)
                stats = subtree_stats(tree_entries) if tree_mode else {}
                for index, (session, branch) in enumerate(
                    display_entries[offset : offset + page_size], offset
                ):
                    draw_session_line(
                        window,
                        index - offset + 2,
                        session,
                        index == selected,
                        branch,
                        stats.get(row_id(session)),
                        striped=stripes and bool(index % 2),
                    )
                total = len(display_entries)

            if detail is None and mode == "cwd":
                display_index = {
                    item_key(item): i
                    for i, item in enumerate(items)
                    if item[0] == "session"
                }
                links = parent_links(tree_entries) if tree_mode else {}
                entry_children = defaultdict(list)
                for entry in tree_entries:
                    parent_id = links.get(row_id(entry))
                    if parent_id in display_index:
                        entry_children[parent_id].append(row_id(entry))
                tree_parents = {
                    i: display_index.get(links.get(item_key(item)))
                    for i, item in enumerate(items)
                }
                tree_children = defaultdict(list)
                for index, parent in tree_parents.items():
                    tree_children[parent].append(index)

            footer_sessions = (
                detail[1]
                if detail is not None
                else [entry for entry in grouped_visible if in_scope(entry, cwd_node)]
            )
            footer = (
                f"Transcripts: {human_size(sum(session.size for session in footer_sessions))}  "
                f"{len(footer_sessions):,} sessions"
            )
            if warning := scan_notice():
                footer += "  " + warning
            label = f" asdu  {location}"
            if source_filter != "all":
                label += f"  [{source_filter}]"
            ordering = ("tree " if tree_mode else "") + sort_label(sort_by) + " "
            available = max(0, width - 1 - display_width(ordering))
            draw_line(
                window,
                0,
                pad_display(compact_text(label, available), available) + ordering,
                bold=True,
                invert=True,
            )
            selected_session = detail is not None or (
                bool(items) and items[selected][0] == "session"
            )
            has_sessions = detail is not None or any(
                kind == "session" for kind, _, _ in items
            )
            commands = ["Enter open"]
            if detail is not None or (mode == "cwd" and cwd_node != cwd_root):
                commands.append("Backspace back")
            if detail is None:
                commands.append("g group")
            commands.extend(["f filter", "s sort"])
            if has_sessions:
                commands.append("t tree")
            if selected_session:
                commands.append("a action")
            commands.append("? help")
            draw_line(window, height - 2, " " + (status or footer))
            draw_line(window, height - 1, " " + "  ".join(commands), invert=True)

            window.refresh()
            siblings = (
                tree_children.get(tree_parents.get(selected), []) if tree_mode else []
            )
            first = selected == (siblings[0] if siblings else 0)
            last = selected == (siblings[-1] if siblings else max(0, total - 1))
            key = read_navigation(window, first, last)
            selected, key = drain_navigation(
                window, key, selected, siblings or range(total)
            )
            if key == -1:
                continue
            selected_entry = (
                display_entries[selected][0]
                if detail is not None and display_entries
                else items[selected][2][0]
                if detail is None and items and items[selected][0] == "session"
                else None
            )
            if detail is None and mode == "cwd" and selected_entry is None:
                if key in (curses.KEY_RIGHT, ord("l")):
                    key = 10
                elif key in (curses.KEY_LEFT, ord("h")):
                    key = 127
            status = ""
            if key in (ord("/"), ord("n"), ord("N")):
                if key == ord("/"):
                    query = search_prompt(window)
                labels = (
                    [session_label(entry) for entry, _ in display_entries]
                    if detail
                    else [
                        session_label(es[0])
                        if kind == "session"
                        else group_label(name, mode)
                        for kind, name, es in items
                    ]
                )
                found = find_match(
                    labels, query, selected, -1 if key == ord("N") else 1
                )
                status = (
                    f"/{query}"
                    if any(query.casefold() in label.casefold() for label in labels)
                    else f"No match: {query}"
                )
                selected = found
                continue
            if key in (curses.KEY_HOME, curses.KEY_END):
                selected = 0 if key == curses.KEY_HOME else max(0, total - 1)
                continue
            if key in (ord("q"), 27):
                return
            if key == ord("?"):
                text_view(
                    window,
                    "Help",
                    "Sizes: saved conversation bytes, not project files. Updated: file modification time.\n"
                    "Tag groups: primary tag only; briefs list every matching tag.\n\n"
                    "Enter: open folder or session brief\n/: find text; n/N: next/previous match\nHome/End: first/last\nBackspace: parent folder or previous list\nr: rescan local session roots\nf: filter by source\ns: choose sort\ng: group by folder, tag, source, or session type\nt: toggle session tree (↑↓ siblings, ←→ parent/child, Space fold, z all)\na: archive or move selected session to Trash\ni: open a session brief\nq: quit",
                )
                continue
            if key == ord("r"):
                if detail is None and items:
                    pending_anchor = item_key(items[selected])

                def show_rescan(
                    source: str,
                    current: int,
                    total: int,
                    done_bytes: int,
                    total_bytes: int,
                ) -> None:
                    window.erase()
                    _, progress_width = window.getmaxyx()
                    progress_height, _ = window.getmaxyx()
                    lines = indexing_lines(
                        min(progress_width - 1, 31)
                        if progress_height < 12
                        else progress_width - 1,
                        source,
                        current,
                        done_bytes,
                        total_bytes,
                    )
                    for row, line in enumerate(lines):
                        draw_line(window, row, line, bold=row < 5)
                    window.refresh()

                window.erase()
                draw_line(
                    window,
                    0,
                    " asdu  |  rescanning local sessions ",
                    bold=True,
                    invert=True,
                )
                draw_line(window, 2, "  Collecting local transcripts…")
                window.refresh()
                try:
                    sessions[:] = rescan(show_rescan)
                except OSError as error:
                    status = f"Rescan failed: {error}"
                else:
                    refresh_view()
                continue
            if (
                selected_entry is not None
                and tree_mode
                and key
                in (
                    curses.KEY_DOWN,
                    ord("j"),
                    curses.KEY_UP,
                    ord("k"),
                    curses.KEY_LEFT,
                    ord("h"),
                    curses.KEY_RIGHT,
                    ord("l"),
                )
            ):
                entry = selected_entry
                parent = tree_parents.get(selected)
                siblings = tree_children.get(parent, [])
                sibling_position = (
                    siblings.index(selected) if selected in siblings else 0
                )
                if key in (curses.KEY_DOWN, ord("j")) and sibling_position + 1 < len(
                    siblings
                ):
                    selected = siblings[sibling_position + 1]
                elif key in (curses.KEY_UP, ord("k")) and sibling_position > 0:
                    selected = siblings[sibling_position - 1]
                elif key in (curses.KEY_RIGHT, ord("l")):
                    if row_id(entry) in collapsed_nodes:
                        collapsed_nodes.remove(row_id(entry))
                    elif tree_children.get(selected):
                        selected = tree_children[selected][0]
                elif key in (curses.KEY_LEFT, ord("h")):
                    if (
                        entry_children.get(row_id(entry))
                        and row_id(entry) not in collapsed_nodes
                    ):
                        collapsed_nodes.add(row_id(entry))
                    elif parent is not None:
                        selected = parent
            elif selected_entry is not None and tree_mode and key == ord(" "):
                entry = selected_entry
                if entry_children.get(row_id(entry)):
                    if row_id(entry) in collapsed_nodes:
                        collapsed_nodes.remove(row_id(entry))
                    else:
                        collapsed_nodes.add(row_id(entry))
            elif tree_mode and key == ord("z"):
                if detail is None and items:
                    pending_anchor = item_key(items[selected])
                nodes_with_children = set(entry_children)
                if nodes_with_children.issubset(collapsed_nodes):
                    collapsed_nodes.clear()
                else:
                    collapsed_nodes.update(nodes_with_children)
            elif key in (curses.KEY_DOWN, ord("j")):
                if selected < total - 1:
                    selected += 1
            elif key in (curses.KEY_UP, ord("k")):
                if selected > 0:
                    selected -= 1
            elif key == curses.KEY_NPAGE:
                if selected < total - 1:
                    selected = min(total - 1, selected + page_size)
            elif key == curses.KEY_PPAGE:
                if selected > 0:
                    selected = max(0, selected - page_size)
            elif detail is None and key == ord("g"):
                choice = choose(
                    window,
                    "Group sessions by",
                    ["cwd", "tag", "source", "origin"],
                    mode,
                )
                if choice is not None:
                    mode = choice
                    selected, offset = 0, 0
            elif detail is None and mode == "cwd" and key == ord("t"):
                pending_anchor = item_key(items[selected]) if items else None
                tree_mode, collapsed_nodes = browser.toggle_tree(tree_entries)
            elif detail is not None and key == ord("t"):
                # Build the same contextual tree used for rendering.  A
                # parent outside this tag would otherwise be added only on
                # the next frame and escape the initial folded set.
                contextual_tree = tree_with_ancestors(detail[1], grouped_visible)
                tree_mode, collapsed_nodes = browser.toggle_tree(contextual_tree)
                selected, offset = 0, 0
            elif key == ord("a") and (
                detail is not None or (items and items[selected][0] == "session")
            ):
                entry = (
                    display_entries[selected][0]
                    if detail is not None
                    else items[selected][2][0]
                )
                choice = confirm_session_action(window, entry)
                if choice == "trash" and confirm_deletes:
                    confirmation = confirm_trash(window)
                    if confirmation is None:
                        choice = None
                    elif confirmation == "don't ask again":
                        disable_delete_confirmation()
                        confirm_deletes = False
                if choice is not None:
                    try:
                        if choice == "archive":
                            destination = archive_session(entry)
                            try:
                                record_action("archive", entry, destination)
                            except OSError as error:
                                status = f"Archived; action log unavailable: {error}"
                        else:
                            require_unchanged(entry)
                            move_to_trash(entry.path)
                            try:
                                record_action("trash", entry)
                            except OSError as error:
                                status = f"Trashed; action log unavailable: {error}"
                    except OSError as error:
                        status = str(error)
                    else:
                        sessions.remove(entry)
                        if detail is not None and entry in detail[1]:
                            detail[1].remove(entry)
            elif key == ord("f"):
                available = ["all", *sorted({session.source for session in sessions})]
                choice = choose(window, "Source", available, source_filter)
                if choice is not None:
                    if detail is None and items:
                        pending_anchor = item_key(items[selected])
                    source_filter = choice
                    refresh_view()
            elif detail is not None and key == ord("s"):
                choice = choose(
                    window, "Sort sessions", ["size", "date", "name"], sort_by
                )
                if choice is not None:
                    pending_anchor = (
                        row_id(display_entries[selected][0])
                        if display_entries
                        else None
                    )
                    sort_by = choice
            elif detail is None and key == ord("s"):
                choice = choose(
                    window, "Sort", ["size", "date", "count", "name"], sort_by
                )
                if choice is not None:
                    pending_anchor = item_key(items[selected]) if items else None
                    sort_by = choice
            elif detail is None and key in (curses.KEY_ENTER, 10, 13):
                if mode == "cwd":
                    if not items:
                        continue
                    kind, name, entries = items[selected]
                    if kind == "folder":
                        folder_positions[cwd_node] = (item_key(items[selected]), offset)
                        cwd_node = cwd_node / name
                        pending_anchor, offset = folder_positions.get(
                            cwd_node, (None, 0)
                        )
                        selected = 0
                        continue
                    else:
                        open_brief(
                            window, entries[0], tree_entries if tree_mode else visible
                        )
                        continue
                else:
                    items = browser_group_items(grouped_visible, mode, sort_by)
                    if not items:
                        continue
                    _, name, entries = items[selected]
                    detail = (name, entries)
                    detail_return = (item_key(items[selected]), offset)
                    tree_entries = tree_with_ancestors(entries, grouped_visible)
                    tree_mode, collapsed_nodes = browser.open_group(
                        mode, name, source_filter, cwd_node, tree_entries
                    )
                selected, offset = 0, 0
            elif (
                detail is None
                and mode == "cwd"
                and key in (curses.KEY_BACKSPACE, 127, 8)
            ):
                if cwd_node != cwd_root:
                    folder_positions[cwd_node] = (
                        (item_key(items[selected]), offset) if items else (None, 0)
                    )
                    cwd_node = cwd_node.parent
                    pending_anchor, offset = folder_positions.get(cwd_node, (None, 0))
                    selected = 0
            elif detail is not None and key in (curses.KEY_BACKSPACE, 127, 8):
                detail = None
                pending_anchor, offset = detail_return or (None, 0)
                selected = 0
                detail_return = None
                browser.close_group()
            elif detail is not None and key in (ord("i"), curses.KEY_ENTER, 10, 13):
                entry = display_entries[selected][0]
                open_brief(window, entry, tree_entries if tree_mode else visible)

    try:
        curses.wrapper(run)
    except (KeyboardInterrupt, curses.error):
        # wrapper restores cooked mode before control reaches the shell.  A
        # terminal can emit a partial escape sequence during touchpad/mouse
        # tracking or resize; treat it like a quiet quit rather than a trace.
        return


def main() -> int:
    global ASCII_UI
    parser = argparse.ArgumentParser(
        description="ncdu-style browser for local agent session storage."
    )
    parser.add_argument("--version", action="version", version="asdu 0.1.0")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("browse", "summary", "sessions", "digest"),
        default="browse",
    )
    parser.add_argument(
        "--codex-root",
        type=Path,
        default=Path.home() / ".codex" / "sessions",
        help="Codex session storage directory",
    )
    parser.add_argument(
        "--claude-root",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="Claude session storage directory",
    )
    parser.add_argument(
        "--source",
        choices=("codex", "claude"),
        action="append",
        help="Repeat to select sources; default is all available",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show all selected-source sessions, not only the current directory",
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="Scope to this project directory instead of the current directory",
    )
    parser.add_argument(
        "--config", type=Path, help="TOML tag rules; replaces the built-in rules"
    )
    parser.add_argument(
        "--group",
        choices=("tag", "folder", "source", "type"),
        default="tag",
        help="Group by folder, tag, source, or session type",
    )
    parser.add_argument(
        "--sort", choices=("size", "updated", "count", "name"), default="size"
    )
    parser.add_argument("--tag", help="With 'sessions', show only this primary tag")
    parser.add_argument(
        "--session", help="With 'digest', an exact session ID or unique ID prefix"
    )
    parser.add_argument(
        "--content-keywords",
        "--context-keywords",
        dest="content_keywords",
        action="store_true",
        help="Search recognized user-message text for keyword rules (slower)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress the startup splash and scan progress",
    )
    parser.add_argument(
        "--no-content-cache",
        action="store_true",
        help="Rescan every transcript instead of using the keyword cache",
    )
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Ask before deletion even when confirmation was disabled",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="Use plain ASCII boxes, tree markers, and ncdu-style size bars",
    )
    args = parser.parse_args()
    ASCII_UI = args.ascii
    args.group = {"folder": "cwd", "type": "origin"}.get(args.group, args.group)
    args.sort = {"updated": "date"}.get(args.sort, args.sort)

    if args.command == "sessions" and args.sort == "count":
        parser.error("--sort count applies to groups, not individual sessions")

    try:
        rules = load_tag_rules(args.config)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        parser.error(str(error))
    sources = tuple(dict.fromkeys(args.source or ("codex", "claude")))
    adapters = source_adapters(args.codex_root, args.claude_root)
    source_roots = {source: adapter.root for source, adapter in adapters.items()}
    if args.source:
        for source in sources:
            if not source_roots[source].exists():
                parser.error(
                    f"{source} session root does not exist: {source_roots[source]}"
                )
    elif not any(root.exists() for root in source_roots.values()):
        parser.error(
            "no supported session roots found; pass --codex-root or --claude-root"
        )
    scope = None if args.all else (args.project or Path.cwd()).resolve()

    scan_warning = ""

    def scan_current(
        show_progress: bool,
        render: Callable[[str, int, int, int, int], None] | None = None,
    ) -> list[Session]:
        nonlocal scan_warning
        progress = ScanProgress(
            show_progress
            and (
                render is not None
                or (
                    not args.no_progress
                    and (
                        args.content_keywords
                        or (args.command == "browse" and sys.stderr.isatty())
                    )
                )
            ),
            render,
        )
        cache = ContentCache(args.content_keywords and not args.no_content_cache)
        found = scan(
            sources, adapters, rules, args.content_keywords, progress, cache, scope
        )
        progress.finish()
        scan_warning = progress.notice()
        found = [session for session in found if in_scope(session, scope)]
        return infer_tags(found) if args.config is None else found

    sessions = scan_current(True)
    if scan_warning and args.command != "browse":
        print(scan_warning, file=sys.stderr)

    if args.command == "summary":
        print_summary(sessions, args.group, args.sort)
    elif args.command == "sessions":
        if args.tag:
            sessions = [
                session for session in sessions if primary_tag(session) == args.tag
            ]
        print_sessions(sessions, args.sort)
    elif args.command == "digest":
        if not args.session:
            parser.error("digest requires --session SESSION_ID")
        matches = [
            session
            for session in sessions
            if session.session_id.startswith(args.session)
        ]
        if not matches:
            parser.error(f"no session begins with: {args.session}")
        if len(matches) > 1:
            parser.error(
                f"session prefix is ambiguous ({len(matches)} matches); provide more characters"
            )
        print(session_brief(matches[0], sessions))
    else:
        tui(
            sessions,
            args.group,
            args.sort,
            scope,
            args.confirm_delete or not skip_delete_confirmation(),
            lambda render: scan_current(True, render),
            lambda: scan_warning,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
