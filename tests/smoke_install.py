"""Linux wheel smoke test. Run via tests/Dockerfile; uses disposable data only."""

import gzip
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import asdu
import asdu_browser as browser
import asdu_sessions as transcripts
from asdu_sources import claude as claude_reader
from asdu_sources import codex as codex_reader
from asdu_sources import omp as omp_reader


class InstalledSmoke(unittest.TestCase):
    def test_installed_wheel(self):
        source = Path(__file__).resolve().parents[1]
        for module in (asdu, transcripts, browser):
            installed = Path(module.__file__).resolve()
            original = source / installed.name
            self.assertNotEqual(installed, original)
            self.assertEqual(installed.read_bytes(), original.read_bytes())
        for module in (codex_reader, claude_reader, omp_reader):
            installed = Path(module.__file__).resolve()
            original = source / "asdu_sources" / installed.name
            self.assertNotEqual(installed, original)
            self.assertEqual(installed.read_bytes(), original.read_bytes())
        self.assertEqual(
            subprocess.check_output(["asdu", "--version"], text=True).strip(),
            f"asdu {version('asdu')}",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}):
                codex = root / "codex"
                claude = root / "claude"
                codex.mkdir()
                claude.mkdir()
                fixtures = source / "tests" / "fixtures" / "asdu"
                shutil.copy(fixtures / "rollout-codex.jsonl", codex)
                shutil.copy(fixtures / "claude-test.jsonl", claude)
                progress = asdu.ScanProgress(False)
                cache = transcripts.ContentCache(False)
                entries = codex_reader.discover(codex, [], False, progress, cache, None)
                entries += claude_reader.discover(claude, [], False, progress, cache, None)
                omp = root / "omp"
                omp.mkdir()
                shutil.copy(fixtures / "omp-test.jsonl", omp)
                entries += omp_reader.discover(omp, [], False, progress, cache, None)
                self.assertEqual(
                    {entry.source for entry in entries}, {"codex", "claude", "omp"}
                )
                self.assertEqual(len(progress.invalid), 1)
                for entry in entries:
                    self.assertIn("fixture inspected", asdu.digest(entry))
                args = [
                    "--codex-root",
                    str(codex),
                    "--claude-root",
                    str(claude),
                    "--omp-root",
                    str(omp),
                    "--all",
                ]
                for command in ("summary", "sessions", "digest"):
                    extra = (
                        ["--session", "codex-test-001"] if command == "digest" else []
                    )
                    result = subprocess.run(
                        ["asdu", command, *args, *extra],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    self.assertTrue(result.stdout.strip())

                parent = entries[0]
                child = replace(
                    parent,
                    session_id="child",
                    parent_id=parent.session_id,
                    path=codex / "rollout-child.jsonl",
                )
                tree = browser.session_tree([child, parent], "size")
                self.assertEqual(
                    [entry.session_id for entry, _ in tree],
                    [parent.session_id, "child"],
                )
                self.assertEqual(
                    len(
                        browser.session_tree(
                            [child, parent], "size", {browser.row_id(parent)}
                        )
                    ),
                    1,
                )

                original = parent.path.read_bytes()
                archived = transcripts.archive_session(parent)
                self.assertFalse(parent.path.exists())
                self.assertTrue(archived.is_relative_to(data))
                with gzip.open(archived, "rb") as handle:
                    self.assertEqual(handle.read(), original)

                disposable = entries[1].path
                original = disposable.read_bytes()
                transcripts.move_to_trash(disposable)
                self.assertFalse(disposable.exists())
                trashed = list((data / "Trash" / "files").iterdir())
                self.assertEqual(len(trashed), 1)
                self.assertEqual(trashed[0].read_bytes(), original)
                shutil.move(trashed[0], disposable)
                self.assertEqual(disposable.read_bytes(), original)
                self.assertFalse(asdu.ascii_ui())


if __name__ == "__main__":
    unittest.main(verbosity=2)
