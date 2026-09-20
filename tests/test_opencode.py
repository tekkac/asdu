"""OpenCode SQLite discovery, briefs, controls, and action safety."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_asdu import session

import asdu_tui as ui
import asdu_views as views
from asdu_sources import opencode, session_controls


def create_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as database, database:
        database.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                parent_id TEXT,
                slug TEXT NOT NULL,
                directory TEXT NOT NULL,
                title TEXT NOT NULL,
                version TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                time_archived INTEGER
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL REFERENCES message(id) ON DELETE CASCADE,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE event_sequence (
                aggregate_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL,
                owner_id TEXT
            );
            CREATE TABLE event (
                id TEXT PRIMARY KEY,
                aggregate_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                data TEXT NOT NULL
            );
            """
        )
        database.executemany(
            """
            INSERT INTO session
              (id, project_id, parent_id, slug, directory, title, version,
               time_created, time_updated, time_archived)
            VALUES (?, 'project', ?, ?, '/workspace/open', ?, '1.18.31', 1000, ?, ?)
            """,
            (
                (
                    "ses_parent123",
                    None,
                    "parent",
                    "New session - 2026-09-17T00:00:00Z",
                    2000,
                    None,
                ),
                ("ses_child456", "ses_parent123", "child", "Inspect adapter", 3000, 4),
            ),
        )
        messages = (
            (
                "msg_user",
                "ses_parent123",
                1100,
                json.dumps(
                    {
                        "role": "user",
                        "model": {"providerID": "fictional", "modelID": "model-a"},
                    }
                ),
            ),
            (
                "msg_assistant",
                "ses_parent123",
                1200,
                json.dumps(
                    {
                        "role": "assistant",
                        "providerID": "fictional",
                        "modelID": "model-b",
                    }
                ),
            ),
        )
        database.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            [(key, owner, when, when, data) for key, owner, when, data in messages],
        )
        database.executemany(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    "prt_user",
                    "msg_user",
                    "ses_parent123",
                    1100,
                    1100,
                    json.dumps({"type": "text", "text": "Map OpenCode safely"}),
                ),
                (
                    "prt_reply",
                    "msg_assistant",
                    "ses_parent123",
                    1200,
                    1200,
                    json.dumps({"type": "text", "text": "Adapter inspected"}),
                ),
                (
                    "prt_compact",
                    "msg_assistant",
                    "ses_parent123",
                    1300,
                    1300,
                    json.dumps({"type": "compaction", "auto": True}),
                ),
            ),
        )
        database.execute("INSERT INTO event_sequence VALUES ('ses_parent123', 1, NULL)")
        database.execute(
            "INSERT INTO event VALUES ('evt_1', 'ses_parent123', 1, 'session.updated.1', '{}')"
        )


class OpenCodeTests(unittest.TestCase):
    def test_announces_source_before_opening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            path.touch()
            events = []
            progress = ui.ScanProgress(True, lambda *event: events.append(event))

            def connect_after_announcement(_path):
                self.assertEqual(events[-1][0], "OpenCode")
                raise sqlite3.OperationalError("stop after checking transition")

            with patch.object(opencode, "connect", side_effect=connect_after_announcement):
                self.assertEqual(opencode.discover(path, progress), [])

    def test_reports_database_work_as_record_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            create_database(path)
            events = []
            progress = ui.ScanProgress(True, lambda *event: events.append(event))

            opencode.discover(path, progress)

        self.assertTrue(
            any(event[1] > 0 and event[-1] == "records" for event in events)
        )
        self.assertEqual(events[-1][-1], "sessions")

    def test_maps_sessions_brief_lineage_and_logical_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            create_database(path)
            entries = opencode.discover(path, ui.ScanProgress(False))
            by_id = {entry.session_id: entry for entry in entries}
            parent, child = by_id["ses_parent123"], by_id["ses_child456"]
            digest = views.digest(parent)
        self.assertEqual(
            (parent.session_id, parent.title, parent.origin, parent.archived),
            ("ses_parent123", "untitled — Map OpenCode safely", "primary", False),
        )
        self.assertEqual(
            (child.session_id, child.parent_id, child.origin, child.archived),
            ("ses_child456", "ses_parent123", "subagent", True),
        )
        self.assertTrue(parent.size_is_logical)
        self.assertGreater(parent.size, 0)
        self.assertNotEqual(parent.storage_key, child.storage_key)
        self.assertIn("Map OpenCode safely", digest)
        self.assertIn("Adapter inspected", digest)
        self.assertIn("1 turns across", digest)
        self.assertIn("1 compactions", digest)
        self.assertIn("Provider: fictional", digest)
        self.assertIn("Model: model-b", digest)

    def test_delete_uses_native_tree_once_and_post_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            create_database(path)
            entries = opencode.discover(path, ui.ScanProgress(False))
            parent = next(
                entry for entry in entries if entry.session_id == "ses_parent123"
            )

            def delete_tree(*_args, **_kwargs):
                with closing(sqlite3.connect(path)) as database, database:
                    database.execute("PRAGMA foreign_keys = ON")
                    database.execute(
                        "DELETE FROM session WHERE id IN ('ses_child456', 'ses_parent123')"
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            opencode.prepare_actions(entries, "delete")
            with patch.object(
                opencode.subprocess, "run", side_effect=delete_tree
            ) as run:
                opencode.perform_action(parent, "delete")
            self.assertEqual(
                run.call_args.args[0],
                ["opencode", "--pure", "session", "delete", "ses_parent123"],
            )
            self.assertEqual(run.call_args.kwargs["env"]["OPENCODE_DB"], str(path))
            self.assertEqual(run.call_args.kwargs["stdin"], opencode.subprocess.DEVNULL)

    def test_delete_rejects_bad_ids_and_false_success(self):
        with patch.object(opencode.subprocess, "run") as run:
            with self.assertRaisesRegex(OSError, "invalid OpenCode session ID"):
                opencode.perform_action(session("--help", source="opencode"), "delete")
            run.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            create_database(path)
            parent = next(
                entry
                for entry in opencode.discover(path, ui.ScanProgress(False))
                if entry.session_id == "ses_parent123"
            )
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with (
                patch.object(opencode.subprocess, "run", return_value=completed),
                self.assertRaisesRegex(OSError, "tree still exists"),
            ):
                opencode.perform_action(parent, "delete")

    def test_rejects_changed_or_unsupported_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "opencode.db"
            create_database(path)
            parent = next(
                entry
                for entry in opencode.discover(path, ui.ScanProgress(False))
                if entry.session_id == "ses_parent123"
            )
            with self.assertRaisesRegex(OSError, "changed since scan"):
                opencode.prepare_actions([replace(parent, modified=9)], "delete")

            unsupported = root / "unsupported.db"
            with closing(sqlite3.connect(unsupported)) as database, database:
                database.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
            progress = ui.ScanProgress(False)
            self.assertEqual(opencode.discover(unsupported, progress), [])
            self.assertEqual(progress.invalid, {unsupported})

    def test_resume_uses_directory_without_shell_cd(self):
        entry = session(
            "ses_parent123",
            source="opencode",
            cwd="/workspace/open",
        )
        self.assertEqual(
            session_controls(entry).commands[0].argv,
            ("opencode", "/workspace/open", "--session", "ses_parent123"),
        )
        self.assertFalse(session_controls(replace(entry, archived=True)).commands)


if __name__ == "__main__":
    unittest.main()
