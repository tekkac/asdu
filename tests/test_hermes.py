"""Hermes SQLite discovery, lineage, briefs, and controls."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import asdu_tui as ui
import asdu_views as views
from asdu_sources import available_actions, hermes, session_controls

ROOT = "20260920_100000_a1b2c3d4"
CONTINUATION = "20260920_101000_b2c3d4e5"
DELEGATE = "20260920_102000_c3d4e5f6"
BRANCH = "20260920_103000_d4e5f6a7"


def create_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as database, database:
        database.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                model TEXT,
                model_config TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                cwd TEXT,
                git_repo_root TEXT,
                billing_provider TEXT,
                title TEXT,
                last_activity_at REAL,
                archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                reasoning TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                compacted INTEGER NOT NULL DEFAULT 0,
                _compressed_summary INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER
            );
            """
        )
        database.executemany(
            """
            INSERT INTO sessions VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                (
                    ROOT,
                    "cli",
                    "model-root",
                    "{}",
                    None,
                    1000,
                    1100,
                    "compression",
                    2,
                    0,
                    "/workspace/demo/subdir",
                    "/workspace/demo",
                    "fictional",
                    "Map Hermes safely",
                    1100,
                    0,
                ),
                (
                    CONTINUATION,
                    "cli",
                    "model-next",
                    "{}",
                    ROOT,
                    1101,
                    None,
                    None,
                    3,
                    1,
                    "/workspace/demo/subdir",
                    "/workspace/demo",
                    "fictional",
                    None,
                    1300,
                    0,
                ),
                (
                    DELEGATE,
                    "tool",
                    "model-child",
                    json.dumps({"_delegate_from": CONTINUATION}),
                    CONTINUATION,
                    1200,
                    None,
                    None,
                    1,
                    0,
                    "/workspace/demo/subdir",
                    "/workspace/demo",
                    "fictional",
                    "Inspect lineage",
                    1250,
                    0,
                ),
                (
                    BRANCH,
                    "cli",
                    "model-branch",
                    json.dumps({"_branched_from": CONTINUATION}),
                    CONTINUATION,
                    1210,
                    None,
                    None,
                    1,
                    0,
                    "/workspace/demo/subdir",
                    "/workspace/demo",
                    "fictional",
                    "Try another path",
                    1260,
                    1,
                ),
            ),
        )
        database.executemany(
            """
            INSERT INTO messages
              (session_id, role, content, tool_calls, tool_name, timestamp,
               token_count, reasoning, active, compacted, _compressed_summary)
            VALUES (?, ?, ?, NULL, NULL, ?, NULL, NULL, ?, ?, ?)
            """,
            (
                (ROOT, "user", "Map the live Hermes store", 1001, 1, 0, 0),
                (ROOT, "assistant", "Root reply", 1002, 1, 0, 0),
                (CONTINUATION, "user", "Continue the mapping", 1102, 1, 0, 0),
                (CONTINUATION, "assistant", "Hermes fixture inspected", 1103, 1, 0, 0),
                (CONTINUATION, "assistant", "old compacted reply", 1104, 0, 1, 1),
                (DELEGATE, "assistant", "Delegate reply", 1201, 1, 0, 0),
                (BRANCH, "user", "Try the branch", 1211, 1, 0, 0),
            ),
        )
        database.execute(
            "INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?)",
            (CONTINUATION, "fictional", "model-next", 100, 50),
        )


class HermesTests(unittest.TestCase):
    def test_announces_source_before_opening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            path.touch()
            events = []
            progress = ui.ScanProgress(True, lambda *event: events.append(event))

            def connect_after_announcement(_path):
                self.assertEqual(events[-1][0], "Hermes")
                raise sqlite3.OperationalError("stop after checking transition")

            with patch.object(hermes, "connect", side_effect=connect_after_announcement):
                self.assertEqual(hermes.discover(path, progress), [])

    def test_maps_workspace_lineage_state_and_logical_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            create_database(path)
            entries = hermes.discover(path, ui.ScanProgress(False))
            by_id = {entry.session_id: entry for entry in entries}

        root = by_id[ROOT]
        continuation = by_id[CONTINUATION]
        delegate = by_id[DELEGATE]
        branch = by_id[BRANCH]
        self.assertEqual((root.title, root.cwd), ("Map Hermes safely", "/workspace/demo"))
        self.assertEqual(
            (continuation.origin, continuation.parent_id, continuation.title),
            ("primary", ROOT, "untitled — Continue the mapping"),
        )
        self.assertEqual(
            (delegate.origin, delegate.parent_id), ("subagent", CONTINUATION)
        )
        self.assertEqual(
            (branch.origin, branch.parent_id, branch.forked_from, branch.archived),
            ("primary", None, CONTINUATION, True),
        )
        self.assertTrue(all(entry.source == "hermes" for entry in entries))
        self.assertTrue(all(entry.size_is_logical for entry in entries))
        self.assertTrue(all(entry.size > 0 for entry in entries))
        self.assertFalse(available_actions(root))

    def test_brief_uses_active_messages_and_native_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            create_database(path)
            entry = next(
                item
                for item in hermes.discover(path, ui.ScanProgress(False))
                if item.session_id == CONTINUATION
            )
            digest = views.digest(entry)

        self.assertIn("Continue the mapping", digest)
        self.assertIn("Hermes fixture inspected", digest)
        self.assertNotIn("old compacted reply", digest)
        self.assertIn("Recorded via: cli", digest)
        self.assertIn("Provider: fictional", digest)
        self.assertIn("Model: model-next", digest)
        self.assertIn("1 turns across 2 events; 1 compactions", digest)

    def test_resume_uses_verified_native_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            create_database(path)
            entry = hermes.discover(path, ui.ScanProgress(False))[0]

        self.assertEqual(
            session_controls(entry).commands[0].argv,
            ("hermes", "--tui", "--resume", entry.session_id),
        )

    def test_rejects_unsupported_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with closing(sqlite3.connect(path)) as database, database:
                database.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
            progress = ui.ScanProgress(False)
            self.assertEqual(hermes.discover(path, progress), [])
            self.assertEqual(progress.invalid, {path})


if __name__ == "__main__":
    unittest.main()
