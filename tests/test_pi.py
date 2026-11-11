"""Pi session discovery and recoverable actions using fictional transcripts."""

import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import asdu
import asdu_browser as browser
import asdu_tui as ui
import asdu_views as views
from asdu_sources import perform_session_action, pi, scan, source_adapters


def write_session(path, identifier, parent=None):
    header = {"type": "session", "version": 3, "id": identifier, "cwd": "/project"}
    if parent:
        header["parentSession"] = str(parent)
    records = [
        header,
        {
            "type": "message",
            "id": "one",
            "parentId": None,
            "message": {"role": "user", "content": "Inspect the fixture"},
        },
        {
            "type": "message",
            "id": "two",
            "parentId": "one",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Fixture inspected"}],
                "provider": "example",
                "model": "example-model",
            },
        },
        {"type": "session_info", "name": "old name"},
        {"type": "compaction"},
        {"type": "session_info", "name": "current name"},
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in records))


class PiTests(unittest.TestCase):
    def test_discovery_brief_and_fork(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent.jsonl"
            child = root / "child.jsonl"
            write_session(parent, "parent")
            write_session(child, "child", parent)
            entries = pi.discover(root, ui.ScanProgress(False))
            by_id = {entry.session_id: entry for entry in entries}
            self.assertEqual(by_id["parent"].title, "current name")
            self.assertEqual(by_id["child"].forked_from, "parent")
            self.assertEqual(
                browser.parent_links(entries)[browser.row_id(by_id["child"])],
                browser.row_id(by_id["parent"]),
            )
            brief = views.digest(by_id["parent"])
            self.assertIn("Inspect the fixture", brief)
            self.assertIn("Fixture inspected", brief)
            self.assertIn("1 compactions", brief)
            self.assertEqual(
                pi.session_controls(by_id["parent"]).commands[0].argv,
                ("pi", "--session", str(parent)),
            )

    def test_trash_checks_identity_and_changes_before_moving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "one.jsonl"
            write_session(path, "one")
            entry = pi.discover(root, ui.ScanProgress(False))[0]
            with patch("asdu_sessions.move_to_trash") as trash:
                perform_session_action(entry, "trash")
                trash.assert_called_once_with(path)
                path.write_text(path.read_text() + "{}\n")
                with self.assertRaisesRegex(OSError, "changed"):
                    perform_session_action(entry, "trash")
                trash.assert_called_once()

    def test_root_environment_precedence(self):
        with patch.dict(
            "os.environ",
            {
                "PI_CODING_AGENT_DIR": "/agent",
                "PI_CODING_AGENT_SESSION_DIR": "/sessions",
            },
            clear=True,
        ):
            self.assertEqual(asdu.default_pi_root(), Path("/sessions"))
        with patch.dict("os.environ", {"PI_CODING_AGENT_DIR": "/agent"}, clear=True):
            self.assertEqual(asdu.default_pi_root(), Path("/agent/sessions"))

    def test_overlapping_pi_and_omp_roots_count_files_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_session(root / "one.jsonl", "one")
            adapters = source_adapters({"omp": root, "pi": root})
            entries = scan(("omp", "pi"), adapters, ui.ScanProgress(False))
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].source, "omp")

    def test_fork_path_is_normalized_before_linking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent.jsonl"
            write_session(parent, "parent")
            write_session(
                root / "child.jsonl", "child", root / "sub" / ".." / parent.name
            )
            entries = pi.discover(root, ui.ScanProgress(False))
            child = next(entry for entry in entries if entry.session_id == "child")
            self.assertEqual(child.forked_from, "parent")

    def test_trash_refuses_windows_reparse_points(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "one.jsonl"
            write_session(path, "one")
            entry = pi.discover(root, ui.ScanProgress(False))[0]
            junction = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            with (
                patch.object(Path, "lstat", return_value=junction),
                self.assertRaisesRegex(OSError, "linked"),
            ):
                pi.prepare_actions([entry], "trash")

    def test_trash_refuses_wrong_root_and_reports_trash_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "one.jsonl"
            write_session(path, "one")
            entry = pi.discover(root, ui.ScanProgress(False))[0]
            with self.assertRaisesRegex(OSError, "outside"):
                perform_session_action(
                    replace(entry, source_home=str(root / "other")), "trash"
                )
            with (
                patch(
                    "asdu_sessions.move_to_trash",
                    side_effect=OSError("Trash unavailable"),
                ),
                self.assertRaisesRegex(OSError, "Trash unavailable"),
            ):
                perform_session_action(entry, "trash")
            self.assertTrue(path.exists())

    def test_bad_headers_and_unknown_version_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.jsonl").write_text("not json\n")
            (root / "new.jsonl").write_text(
                json.dumps({"type": "session", "version": 9, "id": "new"}) + "\n"
            )
            self.assertEqual(pi.discover(root, ui.ScanProgress(False)), [])

    def test_supported_versions_use_request_as_title_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for version in (1, 2, 3):
                path = root / f"{version}.jsonl"
                write_session(path, str(version))
                records = [json.loads(line) for line in path.read_text().splitlines()]
                records[0]["version"] = version
                path.write_text(
                    "".join(
                        json.dumps(item) + "\n"
                        for item in records
                        if item["type"] != "session_info"
                    )
                )
            entries = pi.discover(root, ui.ScanProgress(False))
            self.assertEqual(len(entries), 3)
            self.assertTrue(
                all("Inspect the fixture" in entry.title for entry in entries)
            )

    def test_trash_confirmation_explains_active_session_limit(self):
        with patch.object(views, "action_dialog", return_value="no") as dialog:
            window = unittest.mock.Mock()
            self.assertFalse(views.confirm_pi_trash(window, 2))
        self.assertIn("Close Pi sessions first", dialog.call_args.args[2][0])
