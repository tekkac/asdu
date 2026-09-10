"""Linux wheel smoke test. Run through tests/Dockerfile with disposable data."""

import shutil
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
from asdu_sources import claude, codex, omp


class InstalledSmoke(unittest.TestCase):
    def test_installed_wheel(self):
        source = Path(__file__).resolve().parents[1]
        for module in (asdu, asdu_browser, asdu_sessions, ui, views):
            installed = Path(module.__file__).resolve()
            self.assertNotEqual(installed, source / installed.name)
        executable = Path(sys.executable).with_name("asdu")
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
            self.assertEqual(
                {entry.source for entry in entries}, {"codex", "claude", "omp"}
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
