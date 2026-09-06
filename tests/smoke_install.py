"""Linux wheel smoke test. Run via tests/Dockerfile; uses disposable data only."""

import gzip
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

import asdu


class InstalledSmoke(unittest.TestCase):
    def test_installed_wheel(self):
        source = Path(__file__).resolve().parents[1]
        installed = Path(asdu.__file__).resolve()
        self.assertNotEqual(installed, source / "asdu.py")
        self.assertEqual(installed.read_bytes(), (source / "asdu.py").read_bytes())
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
                cache = asdu.ContentCache(False)
                entries = asdu.scan_codex(codex, [], False, progress, cache, None)
                entries += asdu.scan_claude(claude, [], False, progress, cache, None)
                self.assertEqual(
                    {entry.source for entry in entries}, {"codex", "claude"}
                )
                self.assertEqual(len(progress.invalid), 1)
                for entry in entries:
                    self.assertIn("fixture inspected", asdu.digest(entry))
                args = [
                    "--codex-root",
                    str(codex),
                    "--claude-root",
                    str(claude),
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
                tree = asdu.session_tree([child, parent], "size")
                self.assertEqual(
                    [entry.session_id for entry, _ in tree],
                    [parent.session_id, "child"],
                )
                self.assertEqual(
                    len(
                        asdu.session_tree(
                            [child, parent], "size", {asdu.row_id(parent)}
                        )
                    ),
                    1,
                )

                original = parent.path.read_bytes()
                archived = asdu.archive_session(parent)
                self.assertFalse(parent.path.exists())
                self.assertTrue(archived.is_relative_to(data))
                with gzip.open(archived, "rb") as handle:
                    self.assertEqual(handle.read(), original)

                disposable = entries[1].path
                original = disposable.read_bytes()
                asdu.move_to_trash(disposable)
                self.assertFalse(disposable.exists())
                trashed = list((data / "Trash" / "files").iterdir())
                self.assertEqual(len(trashed), 1)
                self.assertEqual(trashed[0].read_bytes(), original)
                shutil.move(trashed[0], disposable)
                self.assertEqual(disposable.read_bytes(), original)
                self.assertFalse(asdu.ascii_ui())


if __name__ == "__main__":
    unittest.main(verbosity=2)
