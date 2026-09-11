#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["send2trash>=1.8.3"]
# ///
"""Command-line entry point for the local agent-session disk browser."""

from __future__ import annotations

import argparse
import curses
import os
import sys
from collections.abc import Callable
from pathlib import Path

import asdu_sessions as store
import asdu_sources as sources_api
import asdu_tui as ui
import asdu_views as view

__version__ = "0.4.1"


def default_root(variable: str, directory: str, leaf: str) -> Path:
    home = Path(os.environ.get(variable) or Path.home() / directory)
    return home.expanduser() / leaf


def main() -> int:
    try:
        return run_main()
    except curses.error as error:
        print(f"asdu: terminal error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        ui.clear_terminal()
        return 130


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="ncdu-style browser for local agent session storage."
    )
    result.add_argument("--version", action="version", version=f"asdu {__version__}")
    result.add_argument(
        "command",
        nargs="?",
        choices=("browse", "summary", "digest"),
        default="browse",
    )
    result.add_argument(
        "--codex-root",
        type=Path,
        default=default_root("CODEX_HOME", ".codex", "sessions"),
        help="Codex session storage directory",
    )
    result.add_argument(
        "--claude-root",
        type=Path,
        default=default_root("CLAUDE_CONFIG_DIR", ".claude", "projects"),
        help="Claude session storage directory",
    )
    result.add_argument(
        "--omp-root",
        type=Path,
        default=Path.home() / ".omp" / "agent" / "sessions",
        help="OMP session storage directory (read-only)",
    )
    result.add_argument(
        "--source",
        choices=("codex", "claude", "omp"),
        action="append",
        help="Repeat to select sources; default is all available",
    )
    result.add_argument(
        "--project",
        type=Path,
        help="Start in this directory instead of the current directory",
    )
    result.add_argument(
        "--group",
        choices=("folder", "source", "all"),
        default="folder",
        help="Browse by folder, source, or all sessions",
    )
    result.add_argument(
        "--sort", choices=("size", "updated", "count", "name"), default="size"
    )
    result.add_argument(
        "--session", help="With 'digest', an exact session ID or unique ID prefix"
    )
    result.add_argument(
        "--no-progress", action="store_true", help="Suppress scan progress"
    )
    result.add_argument(
        "--ascii",
        action="store_true",
        help="Use plain ASCII boxes, tree markers, and ncdu-style size bars",
    )
    result.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colors; retain bold, dim, and selection (also respects NO_COLOR)",
    )
    return result


def run_main() -> int:
    argument_parser = parser()
    args = argument_parser.parse_args()
    view.ASCII_UI = args.ascii
    args.group = {"folder": "cwd"}.get(args.group, args.group)
    args.sort = {"updated": "date"}.get(args.sort, args.sort)
    if args.command == "summary" and args.group == "all":
        argument_parser.error(
            "summary cannot be grouped by all sessions; use folder or source"
        )
    if args.group == "all" and args.sort == "count":
        argument_parser.error(
            "--sort count applies to folder and source groups, not sessions"
        )

    adapters = sources_api.source_adapters(
        args.codex_root, args.claude_root, args.omp_root
    )
    sources = tuple(dict.fromkeys(args.source or adapters))
    source_roots = {
        source: adapters[source].root
        for source in sources
        if adapters[source].root.exists()
    }
    if args.source:
        for source in sources:
            root = adapters[source].root
            if not root.exists():
                argument_parser.error(f"{source} session root does not exist: {root}")
    elif not source_roots:
        argument_parser.error(
            "no supported session roots found; pass --codex-root, --claude-root, or --omp-root"
        )
    start = (args.project or Path.cwd()).resolve()

    def scan_current(
        render: Callable[[str, int, int, int, int], None] | None = None,
    ) -> list[store.Session]:
        progress = ui.ScanProgress(
            render is not None
            or (
                not args.no_progress
                and args.command == "browse"
                and sys.stderr.isatty()
            ),
            render,
        )
        try:
            return sources_api.scan(sources, adapters, progress)
        finally:
            progress.finish()

    sessions = scan_current()
    if args.command == "summary":
        scoped = (
            sessions
            if start == Path("/")
            else [session for session in sessions if store.in_scope(session, start)]
        )
        view.print_summary(scoped, args.group, args.sort)
    elif args.command == "digest":
        if not args.session:
            argument_parser.error("digest requires --session SESSION_ID")
        matches = [
            session
            for session in sessions
            if session.session_id.startswith(args.session)
        ]
        if not matches:
            argument_parser.error(f"no session begins with: {args.session}")
        if len(matches) > 1:
            argument_parser.error(
                f"session prefix is ambiguous ({len(matches)} matches); provide more characters"
            )
        print(view.session_brief(matches[0], sessions))
    else:
        ui.tui(
            sessions,
            args.group,
            args.sort,
            start,
            scan_current,
            no_color=args.no_color,
            source_roots=source_roots,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
