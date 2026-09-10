"""Source adapters parse native metadata and expose source-owned controls."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_asdu import FIXTURES, browser, session

import asdu_tui as ui
import asdu_views as views
from asdu_sources import (
    claude,
    codex,
    omp,
    perform_session_action,
    scan,
    session_controls,
    source_adapters,
)


class SourceTests(unittest.TestCase):
    def test_fixture_discovery_for_every_source(self):
        fixtures = (
            (codex, "rollout-codex.jsonl", "codex-test-001", "codex"),
            (claude, "claude-test.jsonl", "claude-test-001", "claude"),
            (omp, "omp-test.jsonl", "omp-test-001", "omp"),
        )
        for reader, filename, identifier, source in fixtures:
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / filename).write_bytes((FIXTURES / filename).read_bytes())
                entries = reader.discover(root, ui.ScanProgress(False))
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0].session_id, identifier)
                self.assertEqual(entries[0].source, source)
                expected_cwd = (
                    "/fictional/moon" if source == "omp" else "/workspace/demo"
                )
                self.assertEqual(entries[0].cwd, expected_cwd)
                self.assertIn("fixture inspected", views.digest(entries[0]))

    def test_registry_scans_all_existing_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roots = {name: root / name for name in ("codex", "claude", "omp")}
            for path in roots.values():
                path.mkdir()
            with (
                patch.object(codex, "discover", return_value=[session("c")]),
                patch.object(
                    claude,
                    "discover",
                    return_value=[session("l", source="claude")],
                ),
                patch.object(
                    omp, "discover", return_value=[session("o", source="omp")]
                ),
            ):
                adapters = source_adapters(
                    roots["codex"], roots["claude"], roots["omp"]
                )
                entries = scan(tuple(adapters), adapters, ui.ScanProgress(False))
            self.assertEqual(
                {entry.source for entry in entries}, {"codex", "claude", "omp"}
            )

    def test_codex_native_title_and_archived_state(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            active = home / "sessions"
            archived = home / "archived_sessions"
            active.mkdir()
            archived.mkdir()
            identifier = "019bcb82-bef5-7503-88a7-e192d75fc3b8"
            (home / "session_index.jsonl").write_text(
                json.dumps({"id": identifier, "thread_name": "Renamed"}) + "\n"
            )
            record = {
                "type": "session_meta",
                "payload": {
                    "id": identifier,
                    "cwd": "/project",
                    "source": "cli",
                },
            }
            (archived / f"rollout-{identifier}.jsonl").write_text(
                json.dumps(record) + "\n"
            )
            entries = codex.discover(active, ui.ScanProgress(False))
        self.assertEqual(entries[0].title, "Renamed")
        self.assertTrue(entries[0].archived)

    def test_codex_parent_and_review_metadata_are_native(self):
        cases = (
            (
                {"parent_thread_id": "parent", "source": "vscode"},
                "subagent",
                "parent",
            ),
            ({"thread_source": "guardian_review"}, "review", None),
            ({"source": "vscode"}, "primary", None),
        )
        for fields, origin, parent in cases:
            payload = {"id": "id", "cwd": "/project", **fields}
            with patch.object(
                codex,
                "iter_jsonl",
                return_value=[{"type": "session_meta", "payload": payload}],
            ):
                self.assertEqual(codex.read_metadata(Path("x"))[2:], (origin, parent))

    def test_claude_custom_title_and_sidechain_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            records = [
                {"type": "custom-title", "customTitle": "Renamed"},
                {
                    "sessionId": "parent",
                    "agentId": "child",
                    "cwd": "/project",
                    "isSidechain": True,
                },
            ]
            path.write_text("\n".join(map(json.dumps, records)))
            entry = claude.discover(root, ui.ScanProgress(False))[0]
        self.assertEqual(
            (entry.title, entry.origin, entry.session_id, entry.parent_id),
            ("Renamed", "subagent", "child", "parent"),
        )

    def test_claude_only_scans_known_jsonl_layouts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unrelated" / "deep").mkdir(parents=True)
            (root / "unrelated" / "deep" / "file.jsonl").write_text("{}\n")
            self.assertEqual(claude.discover(root, ui.ScanProgress(False)), [])

    def test_omp_native_roles_and_task_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent.jsonl"
            child_dir = root / "parent"
            child_dir.mkdir()
            child = child_dir / "worker.jsonl"
            parent.write_text(
                "\n".join(
                    map(
                        json.dumps,
                        [
                            {
                                "type": "session",
                                "id": "parent",
                                "cwd": "/project",
                            },
                            {
                                "type": "message",
                                "message": {"role": "user", "content": "request"},
                            },
                            {
                                "type": "message",
                                "message": {
                                    "role": "toolResult",
                                    "toolName": "task",
                                    "details": {"progress": [{"id": "worker"}]},
                                },
                            },
                        ],
                    )
                )
            )
            child.write_text(
                "\n".join(
                    map(
                        json.dumps,
                        [
                            {
                                "type": "session",
                                "id": "worker",
                                "cwd": "/project",
                            },
                            {"type": "session_init", "agent": "reviewer"},
                            {
                                "type": "message",
                                "message": {
                                    "role": "assistant",
                                    "provider": "anthropic",
                                    "model": "claude",
                                    "content": "done",
                                },
                            },
                        ],
                    )
                )
            )
            entries = omp.discover(root, ui.ScanProgress(False))
        linked = {entry.session_id: entry for entry in entries}
        self.assertEqual(linked["worker"].origin, "subagent")
        self.assertEqual(linked["worker"].parent_id, "parent")
        self.assertEqual(
            browser.parent_links(entries)[browser.row_id(linked["worker"])],
            browser.row_id(linked["parent"]),
        )

    def test_resume_commands_are_source_specific(self):
        identifier = "019bcb82-bef5-7503-88a7-e192d75fc3b8"
        self.assertEqual(
            session_controls(session(identifier)).commands[0].argv,
            ("codex", "resume", identifier),
        )
        with patch.object(claude, "background_agent", return_value=None):
            command = session_controls(session("claude", source="claude")).commands[0]
            self.assertEqual(command.argv, ("claude", "--resume", "claude"))
        self.assertEqual(
            session_controls(session("omp", source="omp")).commands[0].argv,
            ("omp", "--resume", "omp"),
        )

    def test_omp_is_read_only(self):
        with self.assertRaisesRegex(OSError, "read-only"):
            perform_session_action(session("omp", source="omp"), "delete")


if __name__ == "__main__":
    unittest.main()
