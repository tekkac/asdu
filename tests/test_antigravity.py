"""Antigravity discovery stays read-only and counts owned files once."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import asdu_tui as ui
import asdu_views as views
from asdu_sources import antigravity, available_actions, session_controls

PARENT_ID = "11111111-1111-4111-8111-111111111111"
CHILD_ID = "22222222-2222-4222-8222-222222222222"


def create_conversation(path: Path, steps: int = 2) -> None:
    with closing(sqlite3.connect(path)) as database, database:
        database.executescript(
            """
            CREATE TABLE trajectory_meta (
              trajectory_id TEXT PRIMARY KEY, cascade_id TEXT
            );
            CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_payload BLOB);
            """
        )
        database.execute(
            "INSERT INTO trajectory_meta VALUES (?, ?)", (path.stem, path.stem)
        )
        database.executemany(
            "INSERT INTO steps VALUES (?, ?)",
            ((index, b"fictional") for index in range(steps)),
        )


def create_store(root: Path) -> None:
    conversations = root / "conversations"
    conversations.mkdir(parents=True)
    create_conversation(conversations / f"{PARENT_ID}.db", 3)
    create_conversation(conversations / f"{CHILD_ID}.db", 1)
    brain = root / "brain" / PARENT_ID
    brain.mkdir(parents=True)
    (brain / "artifact.txt").write_text("owned artifact", encoding="utf-8")
    cache = root / "cache"
    cache.mkdir()
    (cache / "last_conversations.json").write_text(
        json.dumps({"/workspace/fallback": CHILD_ID}), encoding="utf-8"
    )
    with closing(sqlite3.connect(root / "conversation_summaries.db")) as database:
        database.executescript(
            """
            CREATE TABLE conversation_summaries (
              conversation_id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              preview TEXT NOT NULL,
              step_count INTEGER NOT NULL,
              last_modified_time TEXT NOT NULL,
              workspace_uris TEXT NOT NULL,
              parent_conversation_id TEXT NOT NULL,
              agent_name TEXT NOT NULL
            );
            """
        )
        database.executemany(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    PARENT_ID,
                    "",
                    "Map Antigravity safely",
                    3,
                    "2026-09-19T12:00:00+00:00",
                    json.dumps(["file:///workspace/demo"]),
                    "",
                    "planner",
                ),
                (
                    CHILD_ID,
                    "Inspect the fictional child",
                    "Child preview",
                    1,
                    "2026-09-19T12:01:00+00:00",
                    "",
                    PARENT_ID,
                    "",
                ),
            ),
        )
        database.commit()


class AntigravityTests(unittest.TestCase):
    def test_discovers_titles_lineage_workspaces_and_owned_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_store(root)
            parent_db = root / "conversations" / f"{PARENT_ID}.db"
            sidecar = parent_db.with_name(parent_db.name + "-shm")
            sidecar.write_bytes(b"owned sidecar")
            os.utime(sidecar, (2_000_000_000, 2_000_000_000))
            entries = antigravity.discover(root, ui.ScanProgress(False))
            by_id = {entry.session_id: entry for entry in entries}
            parent, child = by_id[PARENT_ID], by_id[CHILD_ID]
            expected = parent_db.stat().st_size + sidecar.stat().st_size
            expected += (root / "brain" / PARENT_ID / "artifact.txt").stat().st_size
            sidecar_mtime = sidecar.stat().st_mtime
            digest = views.digest(parent)

        self.assertEqual(
            (parent.source, parent.origin, parent.cwd, parent.title),
            (
                "agy",
                "primary",
                "/workspace/demo",
                "untitled — Map Antigravity safely",
            ),
        )
        self.assertEqual(parent.size, expected)
        self.assertLess(parent.modified, sidecar_mtime)
        self.assertFalse(parent.size_is_logical)
        self.assertEqual(
            (child.origin, child.parent_id, child.cwd, child.title),
            (
                "subagent",
                PARENT_ID,
                "/workspace/fallback",
                "Inspect the fictional child",
            ),
        )
        self.assertIn("3 steps recorded.", digest)
        self.assertIn("Map Antigravity safely", digest)
        self.assertIn("Recorded via: planner", digest)
        self.assertEqual(available_actions(parent), frozenset())
        self.assertEqual(
            session_controls(parent).commands[0].argv,
            ("agy", "--conversation", PARENT_ID),
        )
        self.assertEqual(
            session_controls(replace(parent, session_id="--help")).commands, ()
        )

    def test_skips_malformed_conversation_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conversations = root / "conversations"
            conversations.mkdir(parents=True)
            malformed = conversations / f"{PARENT_ID}.db"
            malformed.write_bytes(b"not sqlite")
            progress = ui.ScanProgress(False)

            self.assertEqual(antigravity.discover(root, progress), [])
            self.assertIn(malformed, progress.invalid)

    def test_workspace_uri_decoding_is_portable(self):
        encoded = json.dumps(["file:///workspace/a%20project"])
        self.assertEqual(
            antigravity.workspace_path(encoded), "/workspace/a project"
        )

    def test_bad_summary_index_does_not_hide_physical_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conversations = root / "conversations"
            conversations.mkdir(parents=True)
            create_conversation(conversations / f"{PARENT_ID}.db")
            with closing(
                sqlite3.connect(root / "conversation_summaries.db")
            ) as database, database:
                database.execute("CREATE TABLE unrelated (value TEXT)")
            progress = ui.ScanProgress(False)

            entries = antigravity.discover(root, progress)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "?")
        self.assertEqual(entries[0].cwd, "(unknown)")
        self.assertIn(root / "conversation_summaries.db", progress.invalid)


if __name__ == "__main__":
    unittest.main()
