"""Small regression tests for the standalone asdu script.

Run with: python3 -m unittest tests/test_asdu.py
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "asdu.py"
FIXTURES = Path(__file__).parent / "fixtures" / "asdu"
SPEC = importlib.util.spec_from_file_location("asdu", SCRIPT)
assert SPEC and SPEC.loader
asdu = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = asdu
SPEC.loader.exec_module(asdu)


def session(identifier: str, parent: str | None = None) -> object:
    return asdu.Session(
        Path(f"/{identifier}.jsonl"),
        10,
        1.0,
        "codex",
        "primary",
        "/project",
        identifier,
        parent,
        identifier,
        ("untagged",),
    )


class AsduTests(unittest.TestCase):
    def test_keyword_prefilter_preserves_literals_and_user_only_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-test.jsonl"
            records = [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Please investigate SQL QUERY and c++ café support",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "security audit"}],
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
            )
            self.assertEqual(
                asdu.transcript_keywords(
                    path, "codex", {"sql query", "c++", "café", "security audit"}
                ),
                {"sql query", "c++", "café"},
            )

    def test_scoped_discovery_skips_unrelated_title_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-test.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"cwd": "/elsewhere", "id": "other"},
                    }
                )
            )
            with patch.object(
                asdu, "derive_title", side_effect=AssertionError("unneeded read")
            ):
                self.assertEqual(
                    asdu.scan_codex(
                        Path(directory),
                        [],
                        False,
                        asdu.ScanProgress(False),
                        asdu.ContentCache(False),
                        Path("/project"),
                    ),
                    [],
                )

    def test_navigation_burst_preserves_reversals_boundaries_and_commands(self):
        class Input:
            def __init__(self, keys):
                self.keys = iter(keys)
                self.blocking = True

            def nodelay(self, enabled):
                self.blocking = not enabled

            def getch(self):
                return next(self.keys, -1)

        up, down = asdu.curses.KEY_UP, asdu.curses.KEY_DOWN
        window = Input([up] * 30 + [down] * 10 + [10])
        self.assertEqual(asdu.drain_navigation(window, up, 2, range(20)), (10, 10))
        self.assertTrue(window.blocking)
        window = Input([down] * 30 + [up] * 4 + [ord("a")])
        self.assertEqual(
            asdu.drain_navigation(window, down, 0, range(10)), (5, ord("a"))
        )
        window = Input([down, up, -1])
        self.assertEqual(asdu.drain_navigation(window, down, 0, [0, 4, 9]), (4, -1))

    def test_tree_keeps_cycles_duplicates_and_sources_separate(self) -> None:
        a, b = session("a", "b"), session("b", "a")
        duplicate = asdu.replace(a, path=Path("/duplicate"))
        foreign = asdu.replace(a, source="claude", parent_id=None)
        for entries in ([a, b], [a, duplicate, b], [foreign, b]):
            rows = asdu.session_tree(entries, "size")
            self.assertCountEqual(
                [asdu.row_id(e) for e, _ in rows], [asdu.row_id(e) for e in entries]
            )
        self.assertEqual(asdu.parent_links([foreign, b]), {})

    def test_brief_handles_disappeared_and_non_object_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session"
            entry = asdu.replace(session("test"), path=path)
            self.assertIn("Transcript unavailable", asdu.digest(entry))
            path.write_text("[]\nnull\ninvalid\n", encoding="utf-8")
            self.assertIn("0 events", asdu.digest(entry))

    def test_archive_unique_names_and_failed_copy_cleanup(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(asdu.os.environ, {"XDG_DATA_HOME": directory}),
        ):
            path = Path(directory) / "transcript"
            path.write_text("original", encoding="utf-8")
            stat = path.stat()
            entry = asdu.replace(
                session("../../escape"),
                path=path,
                size=stat.st_size,
                modified=stat.st_mtime,
            )
            with patch.object(
                asdu.shutil, "copyfileobj", side_effect=OSError("failed")
            ):
                with self.assertRaises(OSError):
                    asdu.archive_session(entry)
            self.assertTrue(path.exists())
            self.assertEqual(list(Path(directory).rglob("*.gz")), [])
            destination = asdu.archive_session(entry)
            self.assertTrue(
                destination.is_relative_to(Path(directory) / "asdu" / "archive")
            )
            with asdu.gzip.open(destination, "rt") as archived:
                self.assertEqual(archived.read(), "original")

    def test_search_wraps_and_preserves_position_on_miss(self) -> None:
        labels = ["Alpha", "Beta", "alphabet"]
        self.assertEqual(asdu.find_match(labels, "ALPHA", 0), 2)
        self.assertEqual(asdu.find_match(labels, "alpha", 0, -1), 2)
        self.assertEqual(asdu.find_match(labels, "absent", 1), 1)
        self.assertTrue(
            all(
                asdu.display_width(line) <= 8
                for line in asdu.wrap_cells("│ ＡＢＣ long sentence", 8)
            )
        )

    def test_navigation_replay_does_not_redraw_boundaries_or_reset_brief(self) -> None:
        class Window:
            def __init__(self, keys):
                self.keys = iter(keys)
                self.frames = []
                self.frame = []

            def getmaxyx(self):
                return 24, 100

            def erase(self):
                self.frame = []

            def addnstr(self, *args):
                self.frame.append(args)

            def refresh(self):
                self.frames.append(self.frame[:])

            def getch(self):
                if getattr(self, "nonblocking", False):
                    return -1
                return next(self.keys)

            def nodelay(self, enabled):
                self.nonblocking = enabled

        entries = [session("first"), session("second")]
        keys = [asdu.curses.KEY_UP] * 100 + [
            asdu.curses.KEY_DOWN,
            10,
            asdu.curses.KEY_END,
            asdu.curses.KEY_DOWN,
            ord("q"),
        ]
        window = Window(keys)
        with ExitStack() as stack:
            for name in ("curs_set", "mousemask", "mouseinterval"):
                stack.enter_context(patch.object(asdu.curses, name))
            stack.enter_context(
                patch.object(asdu.curses, "has_colors", return_value=False)
            )
            stack.enter_context(patch.object(asdu.curses, "color_pair", return_value=0))
            stack.enter_context(
                patch.object(asdu.curses, "wrapper", lambda fn: fn(window))
            )
            stack.enter_context(patch.object(asdu, "digest", return_value="brief"))
            stack.enter_context(patch.object(asdu, "text_view"))
            asdu.tui(entries, "cwd", "name", Path("/project"), True, lambda _: entries)
            original_window = window
            window = Window([10, asdu.curses.KEY_DOWN, ord("r"), ord("f"), ord("q")])
            stack.enter_context(patch.object(asdu, "choose", return_value="codex"))
            refreshed = [session("earlier"), *entries]
            asdu.tui(
                entries[:], "tag", "name", Path("/project"), True, lambda _: refreshed
            )
            selected_after_refresh = [
                args[2]
                for args in window.frames[-1]
                if len(args) > 4 and args[4] & asdu.curses.A_REVERSE
            ]
            self.assertTrue(any("second" in line for line in selected_after_refresh))
            window = original_window
        self.assertEqual(len(window.frames), 3)
        selected = [
            args[2]
            for args in window.frames[-1]
            if len(args) > 4 and args[4] & asdu.curses.A_REVERSE
        ]
        self.assertTrue(any("second" in line for line in selected))

    def test_compact_text_keeps_opening_words(self) -> None:
        self.assertEqual(asdu.compact_text("abcdef", 4), "abc…")
        self.assertEqual(asdu.compact_text("ＡＢＣ", 4), "Ａ…")
        self.assertEqual(asdu.pad_display("Ａ", 3), "Ａ ")
        self.assertEqual(asdu.sort_label("size"), "size↓")

    def test_tree_honors_collapsed_parent(self) -> None:
        root = session("root")
        child = session("child", "root")
        self.assertEqual(
            [
                item.session_id
                for item, _ in asdu.session_tree(
                    [root, child], "size", {asdu.row_id(root)}
                )
            ],
            ["root"],
        )

    def test_tree_adds_parent_outside_the_current_group(self) -> None:
        parent = session("parent")
        child = session("child", "parent")
        tree_entries = asdu.tree_with_ancestors([child], [parent, child])
        self.assertEqual(
            [item.session_id for item, _ in asdu.session_tree(tree_entries, "size")],
            ["parent", "child"],
        )
        self.assertEqual(
            [
                item.session_id
                for item, _ in asdu.session_tree(
                    tree_entries, "size", asdu.folded_tree_nodes(tree_entries)
                )
            ],
            ["parent"],
        )

    def test_tree_state_lives_for_one_app_run_and_one_group(self) -> None:
        parent = session("parent")
        child = session("child", "parent")
        entries = [parent, child]
        state = asdu.BrowserState.create()
        enabled, folds = state.open_group(
            "tag", "work", "all", Path("/project"), entries
        )
        self.assertFalse(enabled)
        enabled, folds = state.toggle_tree(entries)
        self.assertTrue(enabled)
        self.assertEqual(folds, {asdu.row_id(parent)})
        folds.clear()  # user expanded all
        state.close_group()
        enabled, restored = state.open_group(
            "tag", "work", "all", Path("/project"), entries
        )
        self.assertTrue(enabled)
        self.assertEqual(restored, set())
        enabled, _ = state.open_group("tag", "other", "all", Path("/project"), entries)
        self.assertFalse(enabled)

    def test_tree_rows_and_selection_are_pure_and_bounded(self) -> None:
        parent = session("parent")
        child = session("child", "parent")
        self.assertEqual(
            [
                item.session_id
                for item, _ in asdu.tree_rows(
                    [child], [parent, child], "size", True, {asdu.row_id(parent)}
                )
            ],
            ["parent"],
        )
        self.assertEqual(asdu.clamp_view(9, 0, 3, 2), (2, 1))
        self.assertEqual(asdu.clamp_view(0, 4, 0, 2), (0, 0))

    def test_tree_keeps_claude_transcript_files_with_shared_session_id(self) -> None:
        first = asdu.Session(
            Path("/one.jsonl"),
            1,
            1.0,
            "claude",
            "sidechain",
            "/project",
            "shared",
            None,
            "one",
            ("untagged",),
        )
        second = asdu.Session(
            Path("/two.jsonl"),
            1,
            1.0,
            "claude",
            "sidechain",
            "/project",
            "shared",
            None,
            "two",
            ("untagged",),
        )
        rows = asdu.tree_rows([first, second], [first, second], "size", True, set())
        self.assertEqual(
            [entry.path for entry, _ in rows], [Path("/one.jsonl"), Path("/two.jsonl")]
        )

    def test_synthetic_source_fixtures(self) -> None:
        rules: list[object] = []
        progress = asdu.ScanProgress(False)
        cache = asdu.ContentCache(False)
        scanners = [
            ("rollout-codex.jsonl", asdu.scan_codex, "codex-test-001", "codex"),
            ("claude-test.jsonl", asdu.scan_claude, "claude-test-001", "claude"),
        ]
        for filename, scanner, identifier, source in scanners:
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / filename).write_text(
                    (FIXTURES / filename).read_text(encoding="utf-8"), encoding="utf-8"
                )
                sessions = scanner(root, rules, False, progress, cache, None)
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0].session_id, identifier)
                self.assertEqual(sessions[0].source, source)
                self.assertEqual(sessions[0].cwd, "/workspace/demo")
                if source == "claude":
                    self.assertEqual(sessions[0].origin, "sidechain")

    def test_scope_and_progress_are_bounded(self) -> None:
        inside = asdu.Session(
            Path("/inside.jsonl"),
            1,
            1.0,
            "codex",
            "primary",
            "/workspace/demo",
            "inside",
            None,
            "inside",
            ("untagged",),
        )
        outside = asdu.Session(
            Path("/outside.jsonl"),
            1,
            1.0,
            "codex",
            "primary",
            "/elsewhere",
            "outside",
            None,
            "outside",
            ("untagged",),
        )
        self.assertTrue(asdu.in_scope(inside, Path("/workspace")))
        self.assertFalse(asdu.in_scope(outside, Path("/workspace")))
        original_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            progress = asdu.ScanProgress(True)
            progress.update("Codex", 1, 1, 200, 100)
            self.assertIn("100%", sys.stderr.getvalue())
        finally:
            sys.stderr = original_stderr

    def test_progress_can_render_inside_the_tui(self) -> None:
        seen: list[tuple[str, int]] = []
        progress = asdu.ScanProgress(
            True, lambda source, current, *_: seen.append((source, current))
        )
        progress.update("Codex", 2, 3, 4, 6)
        self.assertEqual(seen, [("Codex", 2)])

    def test_scan_continues_when_a_discovered_file_disappears(self) -> None:
        seen: list[tuple[int, int]] = []
        progress = asdu.ScanProgress(
            True, lambda _, current, total, *__: seen.append((current, total))
        )
        missing = Path("/definitely-missing-asdu-session.jsonl")
        result = asdu.scan_paths(
            "test",
            "Test",
            [missing],
            lambda _: None,
            [],
            False,
            progress,
            asdu.ContentCache(False),
            None,
        )
        self.assertEqual(result, [])
        self.assertEqual(seen, [(1, 1)])

    def test_jsonl_reader_tolerates_missing_and_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            path.write_text('not json\n[1]\n{"ok": true}\n', encoding="utf-8")
            self.assertEqual(list(asdu.iter_jsonl(path)), [{"ok": True}])
            self.assertEqual(list(asdu.iter_jsonl(path.with_name("missing.jsonl"))), [])

    def test_default_tags_cover_data_and_documentation(self) -> None:
        rules = asdu.load_tag_rules(None)
        data_session = session("data")
        docs_session = session("docs")
        self.assertIn("data", asdu.classify(data_session, rules, {"data pipeline"}))
        self.assertIn(
            "documentation", asdu.classify(docs_session, rules, {"release notes"})
        )

    def test_tag_browser_adds_all_sessions_without_creating_a_tag(self) -> None:
        entries = [session("one"), session("two")]
        items = asdu.browser_group_items(entries, "tag", "size")
        self.assertEqual(items[0][1], asdu.ALL_SESSIONS)
        self.assertEqual(items[0][2], entries)
        self.assertNotIn(asdu.ALL_SESSIONS, asdu.group_sessions(entries, "tag"))
        self.assertEqual(asdu.group_label(asdu.ALL_SESSIONS, "tag"), "all sessions")

    def test_digest_keeps_structural_first_latest_and_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-test.jsonl"
            records = [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "first useful request",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "latest useful request",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "latest reply"}],
                    },
                },
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            entry = asdu.Session(
                path,
                path.stat().st_size,
                path.stat().st_mtime,
                "codex",
                "primary",
                "/project",
                "test",
                None,
                "test",
                ("untagged",),
            )
            brief = asdu.digest(entry)
            self.assertIn("first useful request", brief)
            self.assertIn("latest useful request", brief)
            self.assertIn("latest reply", brief)
            self.assertIn("├ Last reply\n│ latest reply\n└", brief)
            self.assertIn("Resume: codex resume test", brief)
            self.assertTrue(brief.rstrip().endswith("Resume: codex resume test"))

    def test_resume_command_is_source_specific(self) -> None:
        codex = asdu.Session(
            Path("/x"),
            1,
            1.0,
            "codex",
            "primary",
            "/space project",
            "one;two",
            None,
            "x",
            ("untagged",),
        )
        claude = asdu.Session(
            Path("/x"),
            1,
            1.0,
            "claude",
            "primary",
            "(unknown)",
            "id",
            None,
            "x",
            ("untagged",),
        )
        self.assertEqual(asdu.resume_command(codex), "codex resume 'one;two'")
        self.assertEqual(asdu.resume_command(claude), "claude --resume id")

    def test_action_rejects_changed_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("one", encoding="utf-8")
            before = path.stat()
            entry = asdu.Session(
                path,
                before.st_size,
                before.st_mtime,
                "codex",
                "primary",
                "/project",
                "test",
                None,
                "test",
                ("untagged",),
            )
            path.write_text("changed", encoding="utf-8")
            with self.assertRaises(OSError):
                asdu.require_unchanged(entry)

    def test_action_log_is_content_free_and_uses_xdg_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_state = asdu.os.environ.get("XDG_STATE_HOME")
            asdu.os.environ["XDG_STATE_HOME"] = directory
            try:
                entry = session("private-request")
                asdu.record_action("archive", entry, Path("/archive/item.gz"))
                event = json.loads(asdu.action_log_path().read_text(encoding="utf-8"))
            finally:
                if old_state is None:
                    del asdu.os.environ["XDG_STATE_HOME"]
                else:
                    asdu.os.environ["XDG_STATE_HOME"] = old_state
            self.assertEqual(event["action"], "archive")
            self.assertEqual(event["archive"], "/archive/item.gz")
            self.assertNotIn("title", event)


if __name__ == "__main__":
    unittest.main()
