"""Linux wheel smoke test. Run through tests/Dockerfile with disposable data."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

import asdu
import asdu_browser
import asdu_sessions
import asdu_tui as ui
import asdu_views as views
from asdu_sources import antigravity, claude, codex, hermes, kimi, omp, opencode


class InstalledSmoke(unittest.TestCase):
    def run_cli(self, executable, *arguments):
        result = subprocess.run(
            [executable, *arguments], capture_output=True, text=True, check=False
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result.stdout

    def test_installed_wheel(self):
        source = Path(__file__).resolve().parents[1]
        for module in (asdu, asdu_browser, asdu_sessions, ui, views):
            installed = Path(module.__file__).resolve()
            self.assertNotEqual(installed, source / installed.name)
        executable = Path(sys.executable).with_name(
            "asdu.exe" if os.name == "nt" else "asdu"
        )
        self.assertEqual(
            subprocess.check_output([executable, "--version"], text=True).strip(),
            f"asdu {version('asdu')}",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = source / "tests" / "fixtures" / "asdu"
            readers = (
                (codex, "rollout-codex.jsonl"),
                (claude, "claude-test.jsonl"),
                (omp, "omp-test.jsonl"),
            )
            entries = []
            roots = {}
            for reader, filename in readers:
                target = root / reader.__name__.rsplit(".", 1)[-1]
                target.mkdir()
                shutil.copy(fixtures / filename, target)
                roots[reader] = target
                entries.extend(reader.discover(target, ui.ScanProgress(False)))
            kimi_root = root / "kimi"
            shutil.copytree(fixtures / "kimi-code" / "sessions", kimi_root)
            roots[kimi] = kimi_root
            entries.extend(kimi.discover(kimi_root, ui.ScanProgress(False)))

            opencode_db = root / "opencode.db"
            with closing(sqlite3.connect(opencode_db)) as database, database:
                database.executescript(
                    """
                    CREATE TABLE session (
                      id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
                      title TEXT, time_updated INTEGER, time_archived INTEGER
                    );
                    CREATE TABLE message (
                      id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
                      time_updated INTEGER, data TEXT
                    );
                    CREATE TABLE part (
                      id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                      time_created INTEGER, time_updated INTEGER, data TEXT
                    );
                    INSERT INTO session VALUES (
                      'ses_fixture001', NULL, '/workspace/demo',
                      'OpenCode fixture', 1000, NULL
                    );
                    """
                )
                database.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                    (
                        "msg_fixture001",
                        "ses_fixture001",
                        1000,
                        1000,
                        json.dumps({"role": "assistant"}),
                    ),
                )
                database.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "prt_fixture001",
                        "msg_fixture001",
                        "ses_fixture001",
                        1000,
                        1000,
                        json.dumps({"type": "text", "text": "fixture inspected"}),
                    ),
                )
            roots[opencode] = opencode_db
            entries.extend(opencode.discover(opencode_db, ui.ScanProgress(False)))

            hermes_db = root / "state.db"
            with closing(sqlite3.connect(hermes_db)) as database, database:
                database.executescript(
                    """
                    CREATE TABLE sessions (
                      id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT,
                      model_config TEXT, parent_session_id TEXT,
                      started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
                      cwd TEXT, git_repo_root TEXT, billing_provider TEXT,
                      title TEXT, last_activity_at REAL,
                      archived INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE messages (
                      id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                      role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL,
                      active INTEGER NOT NULL DEFAULT 1,
                      compacted INTEGER NOT NULL DEFAULT 0,
                      _compressed_summary INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO sessions VALUES (
                      '20260920_100000_fixture', 'cli', 'model-fixture', '{}',
                      NULL, 1000, NULL, NULL, '/workspace/demo',
                      '/workspace/demo', 'fictional', 'Hermes fixture', 1001, 0
                    );
                    INSERT INTO messages VALUES (
                      1, '20260920_100000_fixture', 'assistant',
                      'fixture inspected', 1001, 1, 0, 0
                    );
                    """
                )
            roots[hermes] = hermes_db
            entries.extend(hermes.discover(hermes_db, ui.ScanProgress(False)))

            antigravity_root = root / "antigravity"
            antigravity_conversations = antigravity_root / "conversations"
            antigravity_conversations.mkdir(parents=True)
            antigravity_id = "33333333-3333-4333-8333-333333333333"
            antigravity_db = antigravity_conversations / f"{antigravity_id}.db"
            with closing(sqlite3.connect(antigravity_db)) as database, database:
                database.executescript(
                    """
                    CREATE TABLE trajectory_meta (
                      trajectory_id TEXT PRIMARY KEY, cascade_id TEXT
                    );
                    CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_payload BLOB);
                    INSERT INTO trajectory_meta VALUES ('fixture', 'fixture');
                    INSERT INTO steps VALUES (0, X'00');
                    """
                )
            with closing(
                sqlite3.connect(antigravity_root / "conversation_summaries.db")
            ) as database, database:
                database.executescript(
                    """
                    CREATE TABLE conversation_summaries (
                      conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT,
                      step_count INTEGER, last_modified_time TEXT,
                      workspace_uris TEXT, parent_conversation_id TEXT,
                      agent_name TEXT
                    );
                    """
                )
                database.execute(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        antigravity_id,
                        "Antigravity fixture",
                        "fixture inspected",
                        1,
                        "2026-09-19T12:00:00+00:00",
                        json.dumps(["file:///workspace/demo"]),
                        "",
                        "",
                    ),
                )
            roots[antigravity] = antigravity_root
            entries.extend(
                antigravity.discover(antigravity_root, ui.ScanProgress(False))
            )
            self.assertEqual(
                {entry.source for entry in entries},
                {"codex", "claude", "hermes", "kimi", "omp", "opencode", "agy"},
            )
            for entry in entries:
                self.assertIn("fixture inspected", views.digest(entry))

            arguments = [
                "--codex-root",
                str(roots[codex]),
                "--claude-root",
                str(roots[claude]),
                "--omp-root",
                str(roots[omp]),
                "--kimi-root",
                str(roots[kimi]),
                "--opencode-db",
                str(roots[opencode]),
                "--antigravity-root",
                str(roots[antigravity]),
                "--hermes-db",
                str(roots[hermes]),
                "--no-progress",
            ]
            self.assertTrue(self.run_cli(executable, "summary", *arguments).strip())
            self.assertTrue(
                self.run_cli(
                    executable,
                    "digest",
                    *arguments,
                    "--session",
                    "codex-test-001",
                ).strip()
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
