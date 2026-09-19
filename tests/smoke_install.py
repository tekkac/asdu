"""Linux wheel smoke test. Run through tests/Dockerfile with disposable data."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path

import asdu
import asdu_browser
import asdu_sessions
import asdu_tui as ui
import asdu_views as views
from asdu_sources import claude, codex, kimi, omp, opencode


class InstalledSmoke(unittest.TestCase):
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
            with sqlite3.connect(opencode_db) as database:
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
            self.assertEqual(
                {entry.source for entry in entries},
                {"codex", "claude", "kimi", "omp", "opencode"},
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
                "--no-progress",
            ]
            self.assertTrue(
                subprocess.run(
                    [executable, "summary", *arguments],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
            )
            self.assertTrue(
                subprocess.run(
                    [
                        executable,
                        "digest",
                        *arguments,
                        "--session",
                        "codex-test-001",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
