"""Gemini CLI adapter tests use only fictional local session data."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import asdu_tui as ui
from asdu_sources import gemini

MAIN_ID = "11111111-1111-4111-8111-111111111111"
CHILD_ID = "22222222-2222-4222-8222-222222222222"


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


class GeminiTests(unittest.TestCase):
    def make_root(self, base: Path) -> tuple[Path, Path, Path]:
        root = base / ".gemini"
        project = root / "tmp" / "demo"
        chats = project / "chats"
        chats.mkdir(parents=True)
        (project / ".project_root").write_text("/workspace/demo", encoding="utf-8")
        (root / "projects.json").write_text(
            json.dumps({"projects": {"/workspace/demo": "demo"}}),
            encoding="utf-8",
        )
        return root, project, chats

    def test_current_jsonl_maps_lineage_brief_rewinds_and_owned_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root, project, chats = self.make_root(Path(directory))
            main = chats / "session-2026-09-21T10-00-11111111.jsonl"
            records = [
                {
                    "sessionId": MAIN_ID,
                    "projectHash": hashlib.sha256(b"/workspace/demo").hexdigest(),
                    "startTime": "2026-09-21T10:00:00Z",
                    "lastUpdated": "2026-09-21T10:04:00Z",
                    "kind": "main",
                },
                {"id": "u1", "type": "user", "content": "Map Gemini safely"},
                {
                    "id": "a1",
                    "type": "gemini",
                    "content": [{"text": "Fixture inspected"}],
                    "model": "gemini-fixture",
                    "toolCalls": [{"name": "read_file"}],
                    "thoughts": [{"subject": "fixture"}],
                },
                {"id": "u2", "type": "user", "content": "Discard this"},
                {"id": "a2", "type": "gemini", "content": "Discarded reply"},
                {"$rewindTo": "u2"},
                {"$set": {"summary": "Gemini fixture"}},
            ]
            write_jsonl(main, records)
            child = chats / MAIN_ID / f"{CHILD_ID}.jsonl"
            write_jsonl(
                child,
                [
                    {
                        "sessionId": CHILD_ID,
                        "projectHash": records[0]["projectHash"],
                        "startTime": "2026-09-21T10:01:00Z",
                        "lastUpdated": "2026-09-21T10:02:00Z",
                        "kind": "subagent",
                        "directories": ["/workspace/demo/sub"],
                        "summary": "Inspect fixture child",
                    },
                    {"id": "cu", "type": "user", "content": "Child task"},
                    {"id": "ca", "type": "gemini", "content": "Child done"},
                ],
            )
            log = project / "logs" / f"session-{MAIN_ID}.jsonl"
            log.parent.mkdir()
            log.write_text("fictional log", encoding="utf-8")
            output = project / "tool-outputs" / f"session-{MAIN_ID}" / "result.txt"
            output.parent.mkdir(parents=True)
            output.write_text("fictional output", encoding="utf-8")
            plan = project / MAIN_ID / "plans" / "plan.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("fictional plan", encoding="utf-8")

            sessions = gemini.discover(root, ui.ScanProgress(False))
            self.assertEqual(len(sessions), 2)
            by_id = {session.session_id: session for session in sessions}
            parent = by_id[MAIN_ID]
            child_session = by_id[CHILD_ID]
            self.assertEqual(
                (parent.source, parent.origin, parent.cwd, parent.title),
                ("gemini", "primary", "/workspace/demo", "Gemini fixture"),
            )
            self.assertEqual(
                (child_session.origin, child_session.parent_id, child_session.cwd),
                ("subagent", MAIN_ID, "/workspace/demo"),
            )
            self.assertEqual(
                parent.size,
                main.stat().st_size
                + log.stat().st_size
                + output.stat().st_size
                + plan.stat().st_size,
            )

            brief = gemini.load_brief(parent)
            self.assertEqual(brief.first_user, "Map Gemini safely")
            self.assertEqual(brief.latest_user, "Map Gemini safely")
            self.assertEqual(brief.latest_reply, "Fixture inspected")
            self.assertEqual((brief.user_messages, brief.assistant_messages), (1, 1))
            self.assertEqual(brief.event_counts["rewind"], 1)
            self.assertEqual(brief.event_counts["tool"], 1)
            self.assertEqual(brief.event_counts["thought"], 1)
            self.assertEqual(brief.latest_model, "gemini-fixture")
            self.assertEqual(
                gemini.session_controls(parent).commands[0].argv,
                ("gemini", "--resume", MAIN_ID),
            )
            self.assertFalse(gemini.session_controls(child_session).commands)

    def test_legacy_json_uses_registry_hash_and_info_only_is_not_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / ".gemini"
            cwd = "/workspace/legacy"
            project_hash = hashlib.sha256(cwd.encode()).hexdigest()
            chats = root / "tmp" / project_hash / "chats"
            chats.mkdir(parents=True)
            (root / "projects.json").write_text(
                json.dumps({"projects": {cwd: "legacy"}}), encoding="utf-8"
            )
            session_path = chats / "session-legacy.json"
            session_path.write_text(
                json.dumps(
                    {
                        "sessionId": "legacy-session",
                        "projectHash": project_hash,
                        "startTime": "2026-01-01T10:00:00Z",
                        "lastUpdated": "2026-01-01T10:01:00Z",
                        "messages": [
                            {
                                "id": "i1",
                                "type": "info",
                                "content": "Update successful",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            sessions = gemini.discover(root, ui.ScanProgress(False))
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].cwd, cwd)
            self.assertEqual(sessions[0].title, "?")
            self.assertFalse(gemini.session_controls(sessions[0]).commands)

    def test_jsonl_metadata_updates_replace_messages_and_duplicate_files_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, chats = self.make_root(Path(directory))
            legacy = chats / "session-copy.json"
            legacy.write_text(
                json.dumps(
                    {
                        "sessionId": MAIN_ID,
                        "projectHash": "fixture-hash",
                        "lastUpdated": "2026-01-01T00:00:00Z",
                        "messages": [
                            {"id": "old", "type": "user", "content": "Old"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            current = chats / "session-copy.jsonl"
            write_jsonl(
                current,
                [
                    {
                        "sessionId": MAIN_ID,
                        "projectHash": "fixture-hash",
                        "lastUpdated": "2026-02-01T00:00:00Z",
                    },
                    {
                        "$set": {
                            "messages": [
                                {
                                    "id": "new",
                                    "type": "user",
                                    "content": "Replacement prompt",
                                }
                            ]
                        }
                    },
                ],
            )
            sessions = gemini.discover(root, ui.ScanProgress(False))
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].path, current)
            self.assertEqual(
                sessions[0].size, legacy.stat().st_size + current.stat().st_size
            )
            brief = gemini.load_brief(sessions[0])
            self.assertEqual(brief.first_user, "Replacement prompt")


if __name__ == "__main__":
    unittest.main()
