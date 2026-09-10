"""Core browser, rendering, and lifecycle regression tests."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "asdu.py"
FIXTURES = Path(__file__).parent / "fixtures" / "asdu"
SPEC = importlib.util.spec_from_file_location("asdu", SCRIPT)
assert SPEC and SPEC.loader
asdu = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = asdu
sys.path.insert(0, str(SCRIPT.parent))
SPEC.loader.exec_module(asdu)

import asdu_browser as browser
import asdu_sessions as transcripts
import asdu_sources as source_api
import asdu_tui as ui
import asdu_views as views
from asdu_sources import (
    available_actions,
    claude,
    codex,
    prepare_session_actions,
)


def session(identifier: str, parent: str | None = None, **changes):
    entry = transcripts.Session(
        Path(f"/{identifier}.jsonl"),
        10,
        1.0,
        "codex",
        "primary",
        "/project",
        identifier,
        parent,
        identifier,
    )
    return replace(entry, **changes) if changes else entry


class Screen:
    def __init__(self, keys, width=100, height=24):
        self.keys = iter(keys)
        self.width, self.height = width, height
        self.frames, self.frame = [], []
        self.nonblocking = False
        self.colors_enabled = False

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.frame = []

    def addnstr(self, row, column, text, limit, attr=0):
        assert 0 <= row < self.height
        clipped = text[:limit]
        assert column + views.display_width(clipped) < self.width
        self.frame.append((row, column, clipped, attr))

    def refresh(self):
        self.frames.append(self.frame[:])

    def getch(self):
        return -1 if self.nonblocking else next(self.keys)

    def nodelay(self, enabled):
        self.nonblocking = enabled

    def timeout(self, milliseconds):
        self.delay = milliseconds

    def selected(self):
        return " ".join(
            text
            for row, _, text, attr in self.frames[-1]
            if row not in (0, self.height - 1) and attr & ui.curses.A_REVERSE
        )


def browse_screen(
    entries,
    keys,
    mode="cwd",
    sort="name",
    rescan=None,
    start=Path("/project"),
    width=100,
    height=24,
):
    screen = Screen(keys, width, height)
    with ExitStack() as stack:
        for name in ("curs_set", "mousemask", "mouseinterval"):
            stack.enter_context(patch.object(ui.curses, name))
        stack.enter_context(patch.object(ui.curses, "has_colors", return_value=False))
        stack.enter_context(patch.object(ui.curses, "color_pair", return_value=0))
        stack.enter_context(patch.object(ui.curses, "wrapper", lambda run: run(screen)))
        stack.enter_context(patch.object(ui, "clear_terminal"))
        ui.tui(entries, mode, sort, start, rescan or (lambda _: entries))
    return screen


def frame_text(screen):
    return " ".join(text for _, _, text, _ in screen.frames[-1])


def frame_row(screen, row):
    """Reconstruct one fake-screen row while preserving drawn columns."""
    cells = [" "] * screen.width
    for item_row, column, text, _ in screen.frames[-1]:
        if item_row == row:
            cells[column : column + len(text)] = text
    return "".join(cells)


class BrowserTests(unittest.TestCase):
    def test_all_sessions_is_a_view_not_a_synthetic_group(self):
        entries = [
            session("one", cwd="/project/a"),
            session("two", cwd="/project/b"),
        ]
        for items in (
            browser.cwd_listing(entries, Path("/project"), "size"),
            browser.browser_group_items(entries, "source", "size"),
        ):
            self.assertNotIn("__asdu_all_sessions__", [item[1] for item in items])

    def test_group_cycles_to_flat_size_sorted_all_sessions(self):
        small = session("small", cwd="/project/a", size=1)
        large = session("large", cwd="/project/b", size=20)
        screen = browse_screen([small, large], [ord("g"), ord("q")], sort="name")
        self.assertIn("all sessions", frame_text(screen))
        self.assertIn("large", screen.selected())

        self.assertIn("source  type", frame_text(screen))

        returned = browse_screen(
            [small, large],
            [ord("g"), ord("g"), ord("g"), ord("q")],
            sort="name",
        )
        self.assertIn("name↑", frame_text(returned))

    def test_global_search_uses_title_folder_and_id(self):
        target = session(
            "019bcb82-bef5-7503-88a7-e192d75fc3b8",
            cwd="/project/two/folders/away",
            title="Improve asdu search",
        )
        other = session("other", cwd="/elsewhere", title="Unrelated")
        for query in ("asdu", "two/folders", "019bcb82"):
            self.assertEqual(browser.search_sessions([other, target], query), [target])

    def test_search_prompt_accepts_text_and_escape_cancels(self):
        self.assertIsNone(views.search_prompt(Screen([27])))
        self.assertEqual(views.search_prompt(Screen([ord("a"), ord("s"), 10])), "as")

    def test_search_opens_flat_results_and_back_restores_folder(self):
        target = session("target", cwd="/elsewhere", title="Needle session")
        with patch.object(views, "search_prompt", return_value="needle"):
            screen = browse_screen(
                [session("local"), target],
                [ord("/"), 127, ord("q")],
            )
        self.assertIn("local", screen.selected())
        self.assertNotIn("Needle session", frame_text(screen))

    def test_unknown_sessions_are_a_root_bucket(self):
        unknown = session("unknown", cwd="(unknown)")
        self.assertEqual(browser.cwd_listing([unknown], Path("/project"), "size"), [])
        self.assertEqual(
            browser.cwd_listing([unknown], Path("/"), "size"),
            [("folder", "(unknown folder)", [unknown])],
        )

    def test_backspace_can_climb_above_start_directory(self):
        screen = browse_screen([session("outside", cwd="/other")], [127, ord("q")])
        self.assertIn("/other", frame_text(screen))

    def test_empty_folder_explains_scan_and_parent(self):
        text = frame_text(browse_screen([], [ord("q")]))
        self.assertIn("No supported sessions", text)
        self.assertIn("Press Backspace", text)

    def test_navigation_keeps_an_empty_list_at_zero(self):
        state = ui.TuiState(
            [], "cwd", "size", Path("/project"), browser.BrowserState.create()
        )
        frame = SimpleNamespace(total=0, page_size=10)
        for key in (
            ui.curses.KEY_DOWN,
            ui.curses.KEY_UP,
            ui.curses.KEY_NPAGE,
            ui.curses.KEY_PPAGE,
            ui.curses.KEY_HOME,
            ui.curses.KEY_END,
        ):
            self.assertTrue(ui.handle_list_navigation(state, frame, key))
            self.assertEqual(state.browser.selected, 0)

    def test_empty_filter_explains_filter_and_hides_open(self):
        text = frame_text(
            browse_screen([session("main")], [ord("F"), ord("F"), ord("q")])
        )
        self.assertIn("No sessions match: child", text)
        self.assertNotIn("were started", text)
        self.assertNotIn("Enter open", text)

    def test_group_cycles_from_folder_to_source(self):
        text = frame_text(
            browse_screen([session("task")], [ord("g"), ord("g"), ord("q")])
        )
        self.assertIn("/project  sources", text)

    def test_rescan_rebuilds_current_view(self):
        screen = browse_screen(
            [session("old")], [ord("r"), ord("q")], rescan=lambda _: [session("new")]
        )
        self.assertIn("new", frame_text(screen))
        self.assertNotIn("/old.jsonl", frame_text(screen))

    def test_source_filter_rebuilds_current_view(self):
        entries = [session("codex"), session("claude", source="claude")]
        text = frame_text(browse_screen(entries, [ord("f"), ord("q")]))
        self.assertIn("claude", text)
        self.assertNotIn("codex.jsonl", text)

    def test_source_and_session_type_filters_compose(self):
        entries = [
            session("codex-main", title="codex main"),
            session("claude-main", source="claude", title="claude main"),
            session(
                "claude-child",
                source="claude",
                origin="subagent",
                title="claude child",
            ),
        ]
        visible, _ = browser.browser_visible_sessions(
            entries, "claude", "main", "cwd", Path("/project")
        )
        self.assertEqual([entry.title for entry in visible], ["claude main"])
        text = frame_text(browse_screen(entries, [ord("f"), ord("F"), ord("q")]))
        self.assertIn("claude  main", text)
        self.assertIn("claude main", text)
        self.assertNotIn("claude child", text)

    def test_session_type_filter_reaches_unknown_as_question_mark(self):
        entries = [
            session("main", title="known main"),
            session("unknown", origin="unknown", title="unknown type"),
        ]
        text = frame_text(
            browse_screen(entries, [ord("F"), ord("F"), ord("F"), ord("F"), ord("q")])
        )
        self.assertIn("  ?", text)
        self.assertIn("unknown type", text)
        self.assertNotIn("known main", text)

    def test_folder_session_gap_is_visual_only(self):
        entries = [
            session("nested", cwd="/project/folder", title="nested"),
            session("direct", cwd="/project", title="direct"),
        ]
        screen = browse_screen(entries, [ui.curses.KEY_DOWN, ord("q")])
        rows = {
            row: "".join(
                text for item_row, _, text, _ in screen.frames[-1] if item_row == row
            )
            for row, _, _, _ in screen.frames[-1]
        }
        folder_row = next(row for row, text in rows.items() if "/folder" in text)
        session_row = next(row for row, text in rows.items() if "direct" in text)
        self.assertEqual(session_row, folder_row + 2)
        self.assertIn("direct", screen.selected())
        self.assertNotIn("source  type", frame_text(screen))

    def test_folder_selection_does_not_repeat_header_totals(self):
        screen = browse_screen([session("nested", cwd="/project/folder")], [ord("q")])
        self.assertIn("1 sessions", frame_row(screen, 0))
        self.assertEqual(frame_row(screen, screen.height - 2).strip(), "")

    def test_tree_navigation_crosses_from_session_back_to_folder(self):
        entries = [
            session("nested", cwd="/project/folder", title="nested"),
            session("direct", cwd="/project", title="direct"),
        ]
        screen = browse_screen(
            entries,
            [ui.curses.KEY_DOWN, ui.curses.KEY_UP, ord("q")],
        )
        self.assertIn("/folder", screen.selected())


class TreeTests(unittest.TestCase):
    def test_all_tree_stays_inside_scope_and_keeps_scope_in_header(self):
        parent = session("parent", cwd="/outside", title="Outside parent", size=120)
        child = session(
            "child", "parent", cwd="/project", title="Inside child", size=80
        )
        screen = browse_screen(
            [parent, child],
            [ord("g"), ord("t"), ord("q")],
            sort="size",
        )
        text = frame_text(screen)
        self.assertIn("/project  all sessions", text)
        self.assertIn("Inside child", text)
        self.assertNotIn("Outside parent", text)
        self.assertNotIn("200 B", text)

    def test_all_sessions_can_toggle_tree(self):
        parent, child = session("parent"), session("child", "parent")
        screen = browse_screen(
            [parent, child],
            [ord("g"), ord("t"), ord("q")],
            sort="name",
        )
        self.assertIn("▸", frame_text(screen))
        self.assertIn("t tree", frame_text(screen))

    def test_all_sessions_tree_arrows_expand_and_enter_children(self):
        parent, child = session("parent"), session("child", "parent")
        screen = browse_screen(
            [parent, child],
            [
                ord("g"),
                ord("t"),
                ui.curses.KEY_RIGHT,
                ui.curses.KEY_RIGHT,
                ord("q"),
            ],
            sort="name",
        )
        self.assertIn("child", screen.selected())

    def test_all_sessions_tree_sorts_roots_by_descendant_size(self):
        parent = session("parent", size=1)
        child = session("child", "parent", size=100)
        standalone = session("standalone", size=50)
        screen = browse_screen(
            [standalone, child, parent],
            [ord("g"), ord("t"), ord("q")],
            sort="size",
        )
        self.assertIn("101 B", screen.selected())
        self.assertIn("parent", screen.selected())

    def test_tree_is_folded_by_default_per_view(self):
        parent, child = session("parent"), session("child", "parent")
        state = browser.BrowserState.create()
        enabled, folds = state.open_group(
            "cwd", "/project", "all", "all", Path("/project"), [parent, child]
        )
        self.assertTrue(enabled)
        self.assertEqual(folds, {browser.row_id(parent)})
        self.assertEqual(state.toggle_tree([parent, child]), (False, set()))
        state.close_group()
        enabled, _ = state.open_group(
            "cwd", "/other", "all", "all", Path("/other"), [parent, child]
        )
        self.assertTrue(enabled)

    def test_relationships_keep_sources_and_cycles_separate(self):
        parent, child = session("parent"), session("child", "parent")
        foreign = session("parent", source="claude")
        links = browser.parent_links([parent, child, foreign])
        self.assertEqual(links[browser.row_id(child)], browser.row_id(parent))
        self.assertEqual(
            browser.parent_links([session("a", "b"), session("b", "a")]), {}
        )

    def test_subtree_is_descendants_first(self):
        entries = [session("root"), session("child", "root"), session("grand", "child")]
        self.assertEqual(
            [
                entry.session_id
                for entry in browser.session_subtree(entries, entries[0])
            ],
            ["grand", "child", "root"],
        )

    def test_up_down_move_between_tree_siblings(self):
        entries = [session("parent"), session("child", "parent"), session("sibling")]
        screen = browse_screen(
            entries,
            [ord("z"), ui.curses.KEY_DOWN, ord("q")],
            sort="name",
        )
        self.assertIn("sibling", screen.selected())

    def test_folded_tree_and_stats(self):
        parent, child = session("parent"), session("child", "parent")
        rows = browser.session_tree([parent, child], "name", {browser.row_id(parent)})
        self.assertEqual([entry.session_id for entry, _ in rows], ["parent"])
        self.assertEqual(
            browser.subtree_stats([parent, child])[browser.row_id(parent)], (20, 1)
        )


class RenderingTests(unittest.TestCase):
    def test_narrow_footer_keeps_help_and_quit(self):
        text = frame_text(browse_screen([session("one")], [ord("q")], width=60))
        self.assertIn("? help", text)
        self.assertIn("q quit", text)

    def test_claude_brief_previews_before_runtime_lookup(self):
        events = []

        def fake_digest(_session, _poll=None, preview=False, controls=None):
            events.append("preview" if preview else "full")
            return events[-1]

        def fake_controls(_session):
            events.append("controls")
            return transcripts.SessionControls()

        def consume(_window, _title, _body, update=None):
            deadline = time.monotonic() + 1
            while "full" not in events and time.monotonic() < deadline:
                if update:
                    update()
                time.sleep(0.001)

        with (
            patch.object(views, "digest", side_effect=fake_digest),
            patch.object(source_api, "session_controls", side_effect=fake_controls),
            patch.object(ui, "text_view", side_effect=consume),
        ):
            ui.open_brief(Screen([]), session("claude", source="claude"), [])
        self.assertEqual(events, ["preview", "controls", "full"])

    def test_codex_brief_counts_extracted_messages(self):
        entry = codex.discover(FIXTURES, ui.ScanProgress(False))[0]
        self.assertIn("2 messages across 3 events", views.digest(entry))

    def test_startup_banner_is_tty_only_and_preserves_scrollback(self):
        terminal = io.StringIO()
        with (
            patch.object(terminal, "isatty", return_value=True),
            patch.object(ui.sys, "stderr", terminal),
        ):
            progress = ui.ScanProgress(True)
            progress.update("Codex", 4, 10, 2_000, 4_000)
            progress.finish()
        output = terminal.getvalue()
        self.assertIn("█████", output)
        self.assertIn("agent session disk usage", output)
        self.assertIn("╭────────────────", output)
        self.assertIn("Indexing Codex", output)
        self.assertNotIn("\033[H", output)
        self.assertNotIn("\033[J", output)

        redirected = io.StringIO()
        with patch.object(ui.sys, "stderr", redirected):
            progress = ui.ScanProgress(True)
            progress.update("Codex", 4, 10, 2_000, 4_000)
            progress.finish()
        self.assertEqual(redirected.getvalue(), "")

    def test_startup_banner_has_ascii_fallback(self):
        terminal = io.StringIO()
        with (
            patch.object(terminal, "isatty", return_value=True),
            patch.object(ui.sys, "stderr", terminal),
            patch.object(views, "ASCII_UI", True),
        ):
            ui.ScanProgress(True)
        output = terminal.getvalue()
        self.assertIn("___", output)
        self.assertTrue(output.isascii())

    def test_session_rows_gain_adaptive_bars(self):
        for width, cells in ((100, 12), (90, 8), (89, 0)):
            screen = Screen([], width=width)
            views.draw_session_line(screen, 2, session("task"), False, largest=10)
            line = "".join(text for _, _, text, _ in screen.frame)
            self.assertEqual(line.count("█"), cells)

    def test_session_column_header_matches_rendered_columns(self):
        entry = session("aligned", title="Aligned session")
        screen = browse_screen([entry], [ord("q")], width=120)
        header = frame_row(screen, 1)
        rendered = frame_row(screen, 2)
        date = views.session_date(entry)
        self.assertEqual(
            header.index("updated") + len("updated"),
            rendered.index(date) + len(date),
        )
        self.assertEqual(header.index("session"), rendered.index("Aligned session"))

    def test_only_source_token_is_colored(self):
        screen = Screen([], width=110)
        screen.colors_enabled = True
        with patch.object(
            ui.curses, "color_pair", side_effect=lambda value: value << 8
        ):
            views.draw_session_line(screen, 2, session("task"), False, largest=10)
        self.assertTrue(
            any(
                text.strip() == "codex" and attr & ui.curses.A_BOLD
                for _, _, text, attr in screen.frame
            )
        )
        self.assertFalse(
            any("task" in text and attr & (7 << 8) for _, _, text, attr in screen.frame)
        )

    def test_selected_row_is_full_width_and_inverted_without_color(self):
        screen = Screen([], width=100)
        views.draw_session_line(screen, 2, session("task"), True, largest=10)
        _, _, text, attr = screen.frame[0]
        self.assertTrue(attr & ui.curses.A_REVERSE)
        self.assertEqual(views.display_width(text), 99)

    def test_selected_row_uses_color_instead_of_inversion(self):
        screen = Screen([], width=100)
        screen.colors_enabled = True
        selection = 1 << 20
        with patch.object(
            ui.curses,
            "color_pair",
            side_effect=lambda pair: selection if pair == views.SELECTED_COLOR else 0,
        ):
            views.draw_session_line(screen, 2, session("task"), True, largest=10)
        _, _, text, attr = screen.frame[0]
        self.assertTrue(attr & selection)
        self.assertFalse(attr & ui.curses.A_REVERSE)
        self.assertEqual(views.display_width(text), 99)

    def test_ascii_fallback_and_text_width(self):
        with patch.object(views, "ASCII_UI", True):
            self.assertEqual(views.size_bar(50, 100, 4), "[##  ]")
            self.assertEqual(views.terminal_art("├─ ▸ child ↓"), "+- > child v")
            self.assertEqual(views.compact_text("abcdef", 4), "abc…")
            self.assertEqual(views.pad_display("Ａ", 3), "Ａ ")

    def test_scan_status_is_one_bounded_line(self):
        for width in (20, 80, 160):
            line = views.scan_status("Codex", 400, 610, 2_000, 4_700, width)
            self.assertLessEqual(views.display_width(line), width)
            self.assertNotIn("\n", line)


class ActionTests(unittest.TestCase):
    def test_claude_has_only_recoverable_trash(self):
        self.assertEqual(
            available_actions(session("claude", source="claude")), {"trash"}
        )

    def test_read_only_source_skips_empty_dialog(self):
        with patch.object(views, "confirm_session_action") as dialog:
            screen = browse_screen([session("omp", source="omp")], [ord("a"), ord("q")])
        dialog.assert_not_called()
        self.assertIn("read-only", frame_text(screen))
        self.assertNotIn("a action", frame_text(screen))

    def test_action_dialog_explains_scope_cost_and_shortcuts(self):
        root, child = session("root", size=10), session("child", "root", size=20)
        screen = Screen([ord("q")], width=120)
        self.assertIsNone(views.confirm_session_action(screen, root, [child, root]))
        text = " ".join(item[2] for item in screen.frames[0])
        self.assertIn("reversible", text)
        self.assertIn("D  Delete tree", text)
        self.assertIn("30 B", text)

    def test_tree_action_applies_descendants_first(self):
        root, child = session("root"), session("child", "root")
        seen = []
        with (
            patch.object(views, "confirm_session_action", return_value="delete tree"),
            patch.object(views, "confirm_permanent_delete", return_value=True),
            patch.object(source_api, "prepare_session_actions"),
            patch.object(
                source_api,
                "perform_prepared_action",
                side_effect=lambda entry, _: (
                    seen.append(entry.session_id) or transcripts.ActionResult()
                ),
            ),
            patch.object(transcripts, "record_action"),
        ):
            browse_screen([root, child], [ord("a"), ord("q")])
        self.assertEqual(seen, ["child", "root"])

    def test_codex_action_rejects_bad_ids_and_bounds_subprocess(self):
        with patch.object(codex.subprocess, "run") as run:
            with self.assertRaisesRegex(OSError, "invalid Codex session ID"):
                codex.perform_action(session("--help"), "delete")
            run.assert_not_called()
        identifier = "019bcb82-bef5-7503-88a7-e192d75fc3b8"
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(codex.subprocess, "run", return_value=completed) as run:
            codex.perform_action(session(identifier), "delete")
        self.assertEqual(
            run.call_args.args[0], ["codex", "delete", "--force", identifier]
        )
        self.assertEqual(run.call_args.kwargs["stdin"], codex.subprocess.DEVNULL)
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        with (
            patch.object(
                codex.subprocess,
                "run",
                side_effect=codex.subprocess.TimeoutExpired("codex", 10),
            ),
            self.assertRaisesRegex(OSError, "timed out"),
        ):
            codex.perform_action(session(identifier), "archive")

    def test_codex_action_verifies_native_storage_change(self):
        identifier = "019bcb82-bef5-7503-88a7-e192d75fc3b8"
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sessions" / f"rollout-{identifier}.jsonl"
            path.parent.mkdir()
            path.write_text("{}\n")
            entry = session(
                identifier,
                path=path,
                size=path.stat().st_size,
                modified=path.stat().st_mtime,
                source_home=str(root),
            )
            with (
                patch.object(codex.subprocess, "run", return_value=completed),
                self.assertRaisesRegex(OSError, "still exists"),
            ):
                codex.perform_action(entry, "delete")
            with (
                patch.object(codex.subprocess, "run", return_value=completed),
                self.assertRaisesRegex(OSError, "archived session"),
            ):
                codex.perform_action(entry, "archive")

            destination = root / "archived_sessions" / path.name

            def archive(*_args, **_kwargs):
                destination.parent.mkdir()
                path.rename(destination)
                return completed

            with patch.object(codex.subprocess, "run", side_effect=archive):
                outcome = codex.perform_action(entry, "archive")
            self.assertEqual(outcome.replacement.path, destination)
            self.assertTrue(outcome.replacement.archived)

    def test_claude_actions_fail_closed_for_live_or_unverifiable_sessions(self):
        entry = session("live", source="claude")
        with (
            patch.object(claude, "active_session_ids", return_value={"live"}),
            self.assertRaisesRegex(OSError, "active Claude session"),
        ):
            prepare_session_actions([entry], "trash")
        with (
            patch.object(claude.subprocess, "run", side_effect=FileNotFoundError()),
            self.assertRaisesRegex(OSError, "verify active Claude sessions"),
        ):
            prepare_session_actions([entry], "trash")

    def test_changed_transcript_is_never_trashed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("new")
            entry = session("changed", source="claude", path=path, size=1, modified=1.0)
            with (
                patch.object(transcripts, "move_to_trash") as trash,
                self.assertRaisesRegex(OSError, "changed since scan"),
            ):
                transcripts.trash_session(entry)
            trash.assert_not_called()

    def test_action_log_contains_metadata_not_content(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_STATE_HOME": directory}),
        ):
            transcripts.record_action(
                "delete", session("secret", title="private transcript content")
            )
            record = (Path(directory) / "asdu" / "actions.jsonl").read_text()
        self.assertIn('"session_id":"secret"', record)
        self.assertNotIn("private transcript content", record)


class CliAndReaderTests(unittest.TestCase):
    def test_cli_rejects_meaningless_all_view_options(self):
        common = [
            "--codex-root",
            str(FIXTURES),
            "--source",
            "codex",
            "--no-progress",
        ]
        for arguments in (
            ["asdu", "summary", "--group", "all", *common],
            ["asdu", "--group", "all", "--sort", "count", *common],
        ):
            with (
                patch.object(sys, "argv", arguments),
                patch.object(sys, "stderr", io.StringIO()),
                patch.object(ui, "tui"),
                self.assertRaises(SystemExit),
            ):
                asdu.run_main()

    def test_default_cli_is_folder_browse_and_scan_is_global(self):
        with (
            patch.object(
                sys,
                "argv",
                ["asdu", "--no-progress", "--codex-root", str(FIXTURES)],
            ),
            patch.object(source_api, "scan", return_value=[]) as scan,
            patch.object(ui, "tui") as tui,
        ):
            self.assertEqual(asdu.run_main(), 0)
        self.assertEqual(scan.call_args.args[0], ("codex", "claude", "omp"))
        self.assertEqual(tui.call_args.args[1], "cwd")
        self.assertEqual(tui.call_args.args[3], Path.cwd().resolve())

    def test_removed_classification_flags_are_rejected(self):
        for option in (
            "--all",
            "--config",
            "--tag",
            "--content-keywords",
            "--no-content-cache",
        ):
            with (
                patch.object(sys, "argv", ["asdu", option]),
                patch.object(sys, "stderr", io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                asdu.run_main()

    def test_scan_paths_reports_missing_files(self):
        progress = ui.ScanProgress(False)
        missing = Path("/missing/session.jsonl")
        self.assertEqual(
            transcripts.scan_paths("Test", [missing], lambda _: None, progress), []
        )
        self.assertEqual(progress.skipped, {missing})

    def test_jsonl_reader_tolerates_malformed_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            path.write_text('bad\n[]\n{"ok":true}\n')
            self.assertEqual(list(transcripts.iter_jsonl(path)), [{"ok": True}])

    def test_clear_terminal_only_writes_to_terminal(self):
        for terminal in (True, False):
            stream = io.StringIO()
            with (
                patch.object(stream, "isatty", return_value=terminal),
                patch.object(ui.sys, "stderr", stream),
                patch.object(ui.sys, "stdout", stream),
            ):
                ui.clear_terminal()
            self.assertEqual(bool(stream.getvalue()), terminal)


if __name__ == "__main__":
    unittest.main()
