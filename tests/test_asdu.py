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


class Screen:
    def __init__(self, keys, width=100, height=24):
        self.keys = iter(keys)
        self.width, self.height = width, height
        self.frames, self.frame = [], []
        self.nonblocking = False

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.frame = []

    def addnstr(self, row, column, text, limit, attr=0):
        assert 0 <= row < self.height
        assert column + asdu.display_width(text[:limit]) < self.width
        self.frame.append((row, column, text[:limit], attr))

    def refresh(self):
        self.frames.append(self.frame[:])

    def getch(self):
        return -1 if self.nonblocking else next(self.keys)

    def nodelay(self, enabled):
        self.nonblocking = enabled

    def selected(self):
        return " ".join(
            text
            for row, _, text, attr in self.frames[-1]
            if row not in (0, self.height - 1) and attr & asdu.curses.A_REVERSE
        )


def browse_screen(entries, keys, mode="cwd", sort="name", notice=lambda: ""):
    screen = Screen(keys)
    with ExitStack() as stack:
        for name in ("curs_set", "mousemask", "mouseinterval"):
            stack.enter_context(patch.object(asdu.curses, name))
        stack.enter_context(patch.object(asdu.curses, "has_colors", return_value=False))
        stack.enter_context(patch.object(asdu.curses, "color_pair", return_value=0))
        stack.enter_context(
            patch.object(asdu.curses, "wrapper", lambda run: run(screen))
        )
        asdu.tui(entries, mode, sort, Path("/project"), True, lambda _: entries, notice)
    return screen


class AsduTests(unittest.TestCase):
    def test_selection_pointer_preserves_columns_and_ascii_fallback(self):
        for ascii_mode, marker in ((False, "›"), (True, ">")):
            with patch.object(asdu, "ascii_ui", return_value=ascii_mode):
                screen = Screen([])
                asdu.draw_line(screen, 2, "  /folder", selected=True, pointer=True)
                text = screen.frame[-1][2]
                self.assertTrue(text.startswith(marker + " /folder"))
                self.assertEqual(len(text), screen.width - 1)
                asdu.draw_session_line(screen, 3, session("demo"), True)
                self.assertTrue(screen.frame[-1][2].startswith(marker + " "))
                asdu.draw_line(screen, 4, "  plain", pointer=True)
                self.assertEqual(screen.frame[-1][2], "  plain")
                asdu.draw_line(screen, 5, "dialog", selected=True)
                self.assertTrue(screen.frame[-1][2].startswith("dialog"))

    def test_stripes_fill_rows_without_overriding_selection(self):
        screen = Screen([])
        with patch.object(asdu.curses, "color_pair", side_effect=lambda n: n << 8):
            asdu.draw_line(screen, 2, "folder", striped=True)
            self.assertEqual(len(screen.frame[-1][2]), screen.width - 1)
            self.assertEqual(screen.frame[-1][3], 16 << 8)
            asdu.draw_line(screen, 3, "selected", selected=True, striped=True)
            self.assertEqual(screen.frame[-1][3], asdu.curses.A_REVERSE)

    def test_session_stripes_preserve_column_colors(self):
        screen = Screen([])
        with patch.object(asdu.curses, "color_pair", side_effect=lambda n: n << 8):
            asdu.draw_session_line(screen, 2, session("demo"), False, striped=True)
        self.assertEqual(len(screen.frame[0][2]), screen.width - 1)
        self.assertEqual(screen.frame[2][3], (23 << 8) | asdu.curses.A_BOLD)
        self.assertEqual(screen.frame[3][3], 18 << 8)

    def test_scan_reports_skips_without_counting_out_of_scope_sessions(self):
        for source in ("codex", "claude"):
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                for name, cwd in (("valid", "/project"), ("elsewhere", "/other")):
                    record = (
                        {"type": "session_meta", "payload": {"id": name, "cwd": cwd}}
                        if source == "codex"
                        else {"sessionId": name, "cwd": cwd}
                    )
                    (root / f"rollout-{name}.jsonl").write_text(
                        "broken\n[]\n" + json.dumps(record) + "\n"
                    )
                for name, content in (("empty", ""), ("broken", "oops\n")):
                    (root / f"rollout-{name}.jsonl").write_text(content)
                progress = asdu.ScanProgress(False)
                entries = getattr(asdu, f"scan_{source}")(
                    root,
                    [],
                    False,
                    progress,
                    asdu.ContentCache(False),
                    Path("/project"),
                )
                self.assertEqual([entry.session_id for entry in entries], ["valid"])
                self.assertEqual(len(progress.skipped), 2)
                self.assertEqual(
                    progress.notice(),
                    "2 files skipped; 2 files contain invalid records",
                )
                self.assertEqual(asdu.ScanProgress(False).notice(), "")

    def test_missing_file_is_skipped(self):
        progress = asdu.ScanProgress(False)
        missing = Path("/missing/session.jsonl")
        entries = asdu.scan_paths(
            "codex",
            "Codex",
            [missing],
            lambda _: None,
            [],
            False,
            progress,
            asdu.ContentCache(False),
            None,
        )
        self.assertEqual(entries, [])
        self.assertEqual(progress.skipped, {missing})

    def test_scan_notice_survives_navigation(self):
        screen = browse_screen(
            [session("one"), session("two")],
            [asdu.curses.KEY_DOWN, asdu.curses.KEY_UP, ord("q")],
            notice=lambda: "2 files skipped",
        )
        for frame in screen.frames:
            self.assertTrue(any("2 files skipped" in text for _, _, text, _ in frame))

    def test_nested_parent_and_guardian_metadata(self):
        source = {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}
        for top, expected in ((None, "parent"), ("explicit", "explicit")):
            payload = {"source": source, "parent_thread_id": top}
            with patch.object(
                asdu,
                "iter_jsonl",
                return_value=[{"type": "session_meta", "payload": payload}],
            ):
                self.assertEqual(asdu.read_metadata(Path("/demo"))[3], expected)
        payload = {"source": {"subagent": {"other": "guardian"}}}
        with patch.object(
            asdu,
            "iter_jsonl",
            return_value=[{"type": "session_meta", "payload": payload}],
        ):
            metadata = asdu.read_metadata(Path("/demo"))
            self.assertEqual(metadata[2], "review")
            self.assertIsNone(metadata[3])

    def test_brief_draws_before_loading_and_can_cancel(self):
        for cancel in (False, True):
            screen = Screen([])

            def load(entry, poll):
                self.assertEqual(len(screen.frames), 1)
                self.assertTrue(
                    any(
                        "Reading conversation" in text
                        for _, _, text, _ in screen.frames[0]
                    )
                )
                self.assertTrue(
                    any(
                        "Folder: /project" in text for _, _, text, _ in screen.frames[0]
                    )
                )
                if cancel:
                    with patch.object(screen, "getch", return_value=127):
                        poll()
                return "completed brief"

            with (
                patch.object(asdu, "digest", side_effect=load),
                patch.object(asdu, "text_view") as view,
            ):
                asdu.open_brief(screen, session("demo"), [session("demo")])
                self.assertEqual(view.call_count, 0 if cancel else 1)
            self.assertFalse(screen.nonblocking)

    def test_digest_scan_polls_for_cancellation(self):
        entry = asdu.replace(session("demo"), path=FIXTURES / "rollout-codex.jsonl")
        with self.assertRaises(asdu.BriefCancelled):
            asdu.digest(entry, unittest.mock.Mock(side_effect=asdu.BriefCancelled))

    def test_lean_layout_separator_and_contextual_controls(self):
        entries = [
            asdu.replace(session("nested"), cwd="/project/folder"),
            session("direct"),
        ]
        screen = browse_screen(entries, [ord("q")])
        frame = screen.frames[-1]
        self.assertTrue(
            any(row == 2 and "/folder" in text for row, _, text, _ in frame)
        )
        self.assertTrue(any(row == 4 and "direct" in text for row, _, text, _ in frame))
        self.assertFalse(any(row == 3 for row, _, _, _ in frame))
        footer = next(text for row, _, text, _ in frame if row == 23)
        self.assertNotIn("a action", footer)
        self.assertIn("t tree", footer)
        screen = browse_screen(entries, [asdu.curses.KEY_DOWN, ord("q")])
        self.assertTrue(
            any(
                row == 23 and "a action" in text
                for row, _, text, _ in screen.frames[-1]
            )
        )
        entries.extend(session(f"z{i:02d}") for i in range(30))
        screen = browse_screen(entries, [asdu.curses.KEY_END, ord("q")])
        self.assertIn("z29", screen.selected())
        self.assertTrue(
            any(row <= 21 and "z29" in text for row, _, text, _ in screen.frames[-1])
        )

    def test_flat_list_starts_without_back_row(self):
        screen = browse_screen([session("demo")], [10, ord("q")], mode="tag")
        rows = {row for row, _, text, _ in screen.frames[-1] if "demo" in text}
        self.assertEqual(rows, {2})

    def test_backspace_returns_without_navigation_rows(self):
        screen = browse_screen(
            [session("demo")], [10, asdu.curses.KEY_UP, ord("q")], mode="tag"
        )
        self.assertIn("demo", screen.selected())
        screen = browse_screen([session("demo")], [10, 127, ord("q")], mode="tag")
        self.assertIn("all sessions", screen.selected())
        nested = asdu.replace(session("nested"), cwd="/project/folder")
        screen = browse_screen([nested], [10, ord("q")])
        self.assertIn("nested", screen.selected())
        self.assertFalse(any("← Back" in text for _, _, text, _ in screen.frames[-1]))
        self.assertFalse(any("/.." in text for _, _, text, _ in screen.frames[-1]))
        screen = browse_screen([nested], [10, 127, ord("q")])
        self.assertIn("/folder", screen.selected())
        with patch.object(asdu, "ASCII_UI", True):
            screen = browse_screen([nested], [10, ord("q")])
            self.assertIn("nested", screen.selected())
            self.assertFalse(
                any("< Back" in text for _, _, text, _ in screen.frames[-1])
            )

    def test_two_box_indexing_layout(self):
        for width in (0, 15, 31, 32, 80):
            lines = asdu.indexing_lines(width, "codex", 342, 1200, 2400)
            self.assertTrue(all(asdu.display_width(line) <= width for line in lines))
            if width >= 32:
                self.assertEqual(len(lines), 11)
                self.assertTrue(all(asdu.display_width(line) == 32 for line in lines))
                self.assertIn("50%", lines[8])
                self.assertIn("342 sessions", lines[7])
            else:
                self.assertEqual(len(lines), 1)

    def test_ascii_display_override(self):
        with patch.object(asdu, "ASCII_UI", True):
            self.assertEqual(asdu.size_bar(50, 100, 4), "[##  ]")
            self.assertEqual(asdu.progress_bar(50, 4), "[##  ]")
            self.assertTrue(all(line.isascii() for line in asdu.splash_lines(80)))
            self.assertEqual(asdu.terminal_art("├─ ▸ child ↓"), "+- > child v")
        lines = asdu.splash_lines(80)
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(asdu.display_width(line) == 32 for line in lines))
        self.assertNotIn("░", "".join(lines))

    def test_tree_sizes_include_descendants_once_and_ignore_folds(self):
        parent = asdu.replace(session("parent"), size=100)
        child = asdu.replace(session("child", "parent"), size=20)
        grandchild = asdu.replace(session("grandchild", "child"), size=5)
        other = asdu.replace(session("other"), size=110)
        entries = [parent, child, grandchild, other]
        stats = asdu.subtree_stats([*entries, child])
        self.assertEqual(stats[asdu.row_id(parent)], (125, 2))
        self.assertEqual(stats[asdu.row_id(child)], (25, 1))
        self.assertEqual(asdu.session_tree(entries, "size")[0][0], parent)
        with patch.object(asdu, "digest", return_value="brief"):
            self.assertIn(
                "File: 100 B\nTree: 125 B", asdu.session_brief(parent, entries)
            )
        for keys in ([ord("t"), ord("q")], [ord("t"), ord("z"), ord("q")]):
            screen = browse_screen(entries, keys, sort="size")
            frame = screen.frames[-1]
            rows = " ".join(text for row, _, text, _ in frame if row >= 2)
            self.assertIn("125 B", rows)
            self.assertIn("(+2)", rows)
            footer = " ".join(text for row, _, text, _ in frame if row == 22)
            self.assertIn("235 B", footer)
        self.assertEqual(parent.size, 100)

    def test_child_task_title_and_explicit_fork_in_brief(self):
        metadata = {
            "id": "child",
            "cwd": "/project",
            "parent_thread_id": "parent",
            "forked_from_id": "parent",
            "source": {
                "subagent": {
                    "thread_spawn": {"agent_path": "/root/capacity_mechanism_novelty"}
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-child.jsonl"
            path.write_text(
                json.dumps({"type": "session_meta", "payload": metadata}) + "\n"
            )
            with patch.object(
                asdu,
                "derive_title",
                side_effect=AssertionError("Task title needs no transcript scan"),
            ):
                entries = asdu.scan_codex(
                    Path(directory),
                    [],
                    False,
                    asdu.ScanProgress(False),
                    asdu.ContentCache(False),
                    None,
                )
            child = entries[0]
            self.assertEqual(asdu.session_label(child), "capacity mechanism novelty")
            self.assertEqual(child.size, path.stat().st_size)
            brief = asdu.read_digest(child)
            self.assertIn("Task: /root/capacity_mechanism_novelty", brief)
            self.assertIn("Forked from: parent", brief)
            self.assertNotIn("Parent session: parent", brief)
            self.assertIn("may be inherited", brief)

    def test_task_metadata_requires_explicit_well_formed_fields(self):
        for payload in (
            {},
            {"source": "cli", "parent_thread_id": "parent"},
            {"source": {"subagent": {"thread_spawn": {"agent_path": 3}}}},
            {"source": {"subagent": "bad"}, "forked_from_id": []},
        ):
            self.assertEqual(asdu.task_metadata(payload), ("", ""))
        self.assertEqual(asdu.session_label(session("original")), "original")

    def test_chooser_highlight_stays_inside_border(self):
        for width in (38, 60, 100):
            options = ["cwd", "tag", "source", "origin"]
            screen = Screen([asdu.curses.KEY_DOWN, 10], width)
            self.assertEqual(
                asdu.choose(screen, "Group sessions by", options, "cwd"), "tag"
            )
            for frame in screen.frames:
                highlights = [
                    (column, text)
                    for _, column, text, attr in frame
                    if attr & asdu.curses.A_REVERSE
                ]
                self.assertEqual(len(highlights), 1)
                column, text = highlights[0]
                self.assertEqual(column, 1)
                self.assertEqual(asdu.display_width(text), min(48, width - 3))
                self.assertNotIn("│", text)

    def test_actions_and_confirmation_have_identical_keys_at_all_sizes(self):
        for width in (28, 40, 80):
            for key, expected in (("a", "archive"), ("t", "trash"), ("q", None)):
                screen = Screen([ord(key)], width)
                self.assertEqual(
                    asdu.confirm_session_action(screen, session("example")), expected
                )
            for key, expected in (("y", "yes"), ("n", None), ("q", None)):
                screen = Screen([ord(key)], width)
                self.assertEqual(asdu.confirm_trash(screen), expected)
                self.assertTrue(
                    any("Confirm Trash" in text for _, _, text, _ in screen.frames[0])
                )
            self.assertEqual(
                asdu.confirm_session_action(
                    Screen([asdu.curses.KEY_DOWN, 10], width), session("example")
                ),
                "trash",
            )
            self.assertIsNone(asdu.confirm_trash(Screen([10], width)))

    def test_name_sort_uses_visible_titles_everywhere(self):
        apple = asdu.replace(session("a"), title="untitled — Apple")
        banana = asdu.replace(session("b"), title="Banana")
        entries = [banana, apple]
        self.assertEqual(asdu.ordered_sessions(entries, "name"), [apple, banana])
        self.assertEqual(
            [e for e, _ in asdu.session_tree(entries, "name")], [apple, banana]
        )
        self.assertEqual(
            [es[0] for _, _, es in asdu.cwd_listing(entries, Path("/project"), "name")],
            [apple, banana],
        )

    def test_folder_sessions_support_actions_and_tree(self):
        entries = [session("parent"), session("child", "parent")]
        with patch.object(asdu, "confirm_session_action", return_value=None) as action:
            browse_screen(entries, [ord("a"), ord("q")])
            action.assert_called_once_with(unittest.mock.ANY, entries[1])
        screen = browse_screen(entries, [ord("t"), ord("q")])
        self.assertTrue(any("tree name" in text for _, _, text, _ in screen.frames[-1]))
        self.assertIn("parent", screen.selected())

    def test_folder_return_and_sort_keep_selection(self):
        entries = [
            asdu.replace(session("a"), cwd="/project/a"),
            asdu.replace(session("b"), cwd="/project/b"),
        ]
        for back in (127, 8, asdu.curses.KEY_BACKSPACE):
            screen = browse_screen(entries, [asdu.curses.KEY_DOWN, 10, back, ord("q")])
            self.assertIn("/b", screen.selected())
        entries = [
            asdu.replace(session("a"), size=1),
            asdu.replace(session("b"), size=100),
        ]
        for mode, keys in (
            ("cwd", [ord("s"), ord("q")]),
            ("tag", [10, ord("s"), ord("q")]),
        ):
            with patch.object(asdu, "choose", return_value="size"):
                screen = browse_screen(entries, keys, mode)
            self.assertIn("a", screen.selected())
            self.assertNotIn("b", screen.selected())

    def test_mixed_folder_tree_keeps_folders_and_uses_displayed_session(self):
        entries = [
            asdu.replace(session("nested"), cwd="/project/folder"),
            session("parent"),
            session("child", "parent"),
            session("zebra"),
        ]
        down, right = asdu.curses.KEY_DOWN, asdu.curses.KEY_RIGHT
        screen = browse_screen(entries, [ord("t"), ord("q")])
        self.assertIn("/folder", screen.selected())
        self.assertTrue(any("parent" in text for _, _, text, _ in screen.frames[-1]))
        self.assertFalse(
            any("child" in text for row, _, text, _ in screen.frames[-1] if row >= 3)
        )
        # Expanded children do not interrupt root-level up/down navigation.
        screen = browse_screen(entries, [ord("t"), down, right, down, ord("q")])
        self.assertIn("zebra", screen.selected())
        # Enter and actions must use the tree row, not the old flat-list index.
        keys = [ord("t"), down, right, right]
        with (
            patch.object(asdu, "digest", return_value="brief") as digest,
            patch.object(asdu, "text_view"),
        ):
            browse_screen(entries, [*keys, 10, ord("q")])
            self.assertEqual(digest.call_args.args[0].session_id, "child")
        with patch.object(asdu, "confirm_session_action", return_value=None) as action:
            browse_screen(entries, [*keys, ord("a"), ord("q")])
            self.assertEqual(action.call_args.args[1].session_id, "child")
        # Turning tree view off keeps the selected session in the mixed list.
        screen = browse_screen(entries, [*keys, ord("t"), ord("q")])
        self.assertIn("child", screen.selected())
        self.assertTrue(any("/folder" in text for _, _, text, _ in screen.frames[-1]))

    def test_mixed_tree_folder_navigation_and_rescan(self):
        entries = [
            asdu.replace(session("nested"), cwd="/project/folder"),
            session("parent"),
            session("child", "parent"),
        ]
        screen = browse_screen(entries, [ord("t"), 10, 127, ord("r"), ord("q")])
        self.assertIn("/folder", screen.selected())
        self.assertTrue(any("tree name" in text for _, _, text, _ in screen.frames[-1]))
        self.assertTrue(any("parent" in text for _, _, text, _ in screen.frames[-1]))

    def test_brief_search_tracks_match_separately_from_scroll(self):
        screen = Screen([ord("/"), ord("n"), ord("n"), ord("q")], height=8)
        body = "\n".join(
            ["hit start"] + ["filler"] * 18 + ["hit end one", "hit end two"]
        )
        with patch.object(asdu, "search_prompt", return_value="hit"):
            asdu.text_view(screen, "Title", body)
        # First match is included; the two end matches share a viewport.
        self.assertEqual(screen.frames[0], screen.frames[1])
        self.assertEqual(screen.frames[2], screen.frames[3])

    def test_cli_deduplicates_sources_and_rejects_old_root(self):
        args = [
            "asdu",
            "summary",
            "--codex-root",
            str(FIXTURES),
            "--source",
            "codex",
            "--source",
            "codex",
            "--all",
        ]
        with (
            patch.object(sys, "argv", args),
            patch.object(asdu, "scan", return_value=[]) as scan,
            patch.object(asdu, "print_summary"),
        ):
            self.assertEqual(asdu.main(), 0)
            self.assertEqual(scan.call_args.args[0], ("codex",))
        with (
            patch.object(sys, "argv", ["asdu", "--root", str(FIXTURES)]),
            patch.object(sys, "stderr", io.StringIO()),
            self.assertRaises(SystemExit) as result,
        ):
            asdu.main()
        self.assertEqual(result.exception.code, 2)

    def test_missing_config_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            asdu.load_tag_rules(Path("/definitely-missing-asdu.toml"))

    def test_provenance_keeps_launcher_separate_from_relationship(self):
        for fields, expected in (
            ({"source": "vscode"}, "primary"),
            ({"source": "exec", "originator": "codex_exec"}, "primary"),
            ({"source": "vscode", "parent_thread_id": "parent"}, "subagent"),
            ({}, "unknown"),
        ):
            with patch.object(
                asdu,
                "iter_jsonl",
                return_value=[{"type": "session_meta", "payload": fields}],
            ):
                self.assertEqual(asdu.read_metadata(Path("/demo"))[2], expected)

    def test_claude_queue_and_meta_are_consistent(self):
        records = [
            {
                "cwd": "/project",
                "sessionId": "queue",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": "Ignore this injected research question",
                },
            },
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "Please solve this research question",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.jsonl"
            path.write_text("\n".join(map(json.dumps, records)))
            entries = asdu.scan_claude(
                Path(directory),
                [],
                False,
                asdu.ScanProgress(False),
                asdu.ContentCache(False),
                None,
            )
            self.assertIn("Please solve", entries[0].title)
            brief = asdu.read_digest(entries[0])
            self.assertIn("Please solve", brief)
            self.assertNotIn("Ignore this", brief)

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
        self.assertEqual(len(window.frames), 4)  # Includes the immediate brief shell.
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
        for width in (0, 1, 8, 12):
            for size in (0, 1, 50, 100, 200):
                self.assertEqual(
                    asdu.display_width(asdu.size_bar(size, 100, width)), width
                )
        self.assertEqual(asdu.size_bar(0, 0), "░" * 12)
        self.assertEqual(asdu.size_bar(1, 1000), "▏" + "░" * 11)
        self.assertEqual(asdu.size_bar(100, 100), "█" * 12)
        for width in (0, 10, 28, 34, 80):
            lines = asdu.splash_lines(width)
            self.assertTrue(all(asdu.display_width(line) <= width for line in lines))
            self.assertLessEqual(len(set(map(asdu.display_width, lines))), 1)
        with patch.object(sys, "stderr") as stream:
            stream.encoding = "ascii"
            self.assertEqual(asdu.size_bar(50, 100, 4), "[##  ]")
            self.assertTrue(all(line.isascii() for line in asdu.splash_lines(34)))
        seen: list[tuple[str, int]] = []
        progress = asdu.ScanProgress(
            True, lambda source, current, *_: seen.append((source, current))
        )
        progress.update("Codex", 2, 3, 4, 6)
        self.assertEqual(seen, [("Codex", 2)])

    def test_bare_startup_shows_splash_before_scan(self) -> None:
        for options, terminal, expected in (
            ([], True, True),
            (["--no-progress"], True, False),
            ([], False, False),
        ):
            stream = io.StringIO()

            def inspect_scan(*args):
                self.assertEqual(
                    "agent session disk usage" in stream.getvalue(), expected
                )
                self.assertEqual(args[4].enabled, expected)
                return []

            with (
                patch.object(
                    sys, "argv", ["asdu", "--codex-root", str(FIXTURES), *options]
                ),
                patch.object(sys, "stderr", stream),
                patch.object(stream, "isatty", return_value=terminal),
                patch.object(asdu, "scan", side_effect=inspect_scan) as scan,
                patch.object(asdu, "tui"),
            ):
                self.assertEqual(asdu.main(), 0)
                scan.assert_called_once()
                self.assertEqual(
                    stream.getvalue().count("agent session disk usage"), int(expected)
                )

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
            self.assertIn("├ Last reply\n│ latest reply\n╰", brief)
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
