"""Reader contracts and read-only safety, using fictional local files only."""

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_asdu import Screen, asdu, session, transcripts

from asdu_sources import READERS, omp


class SourceTests(unittest.TestCase):
    def test_omp_short_user_requests_survive_titles_briefs_and_keywords(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [{"type": "session", "id": "greeting", "cwd": "/fictional"}]
            for role, attribution, text in (
                ("user", "user", "hello?"),
                ("assistant", None, "Hello! How can I help?"),
                ("user", "user", "HELLO"),
            ):
                records.append(
                    {
                        "type": "message",
                        "message": {
                            "role": role,
                            "attribution": attribution,
                            "content": [{"type": "text", "text": text}],
                        },
                    }
                )
            (root / "greeting.jsonl").write_text(
                "\n".join(map(json.dumps, records)), encoding="utf-8"
            )
            entries = omp.discover(
                root,
                [transcripts.TagRule("greeting", keywords=("hello",))],
                True,
                asdu.ScanProgress(False),
                transcripts.ContentCache(False),
                None,
            )
            self.assertEqual(transcripts.session_label(entries[0]), "hello?")
            self.assertEqual(entries[0].tags, ("greeting",))
            for preview in (False, True):
                brief = transcripts.read_brief(entries[0], preview=preview)
                self.assertEqual(brief.first_user, "hello?")
                self.assertEqual(brief.latest_user, "HELLO")
            self.assertEqual(
                list(
                    omp.user_texts(
                        {
                            "type": "message",
                            "message": {
                                "role": "user",
                                "attribution": "agent",
                                "content": "Injected instruction",
                            },
                        }
                    )
                ),
                [],
            )

    def test_omp_metadata_and_verified_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent.jsonl"
            artifacts = root / "parent"
            artifacts.mkdir()

            def write(path, records):
                path.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")

            write(
                parent,
                [
                    {
                        "type": "session",
                        "id": "parent-id",
                        "title": "Task agent in title only",
                        "cwd": "/fictional",
                    },
                    {
                        "type": "message",
                        "message": {
                            "role": "toolResult",
                            "toolName": "task",
                            "details": {
                                "progress": [
                                    {"id": "worker"},
                                    {"id": "missing"},
                                    {"id": "../outside"},
                                ]
                            },
                        },
                    },
                ],
            )
            for name in ("worker", "orphan"):
                write(
                    artifacts / f"{name}.jsonl",
                    [
                        {"type": "title", "title": "A stored title"},
                        {"type": "session", "id": name, "cwd": "/fictional"},
                        {
                            "type": "session_init",
                            "agent": "custom-worker",
                            "parentId": "message-not-session",
                        },
                    ],
                )
            entries = omp.discover(
                root,
                [],
                False,
                asdu.ScanProgress(False),
                transcripts.ContentCache(False),
                None,
            )
            by_id = {entry.session_id: entry for entry in entries}
            self.assertEqual(by_id["worker"].origin, "subagent")
            self.assertEqual(by_id["worker"].parent_id, "parent-id")
            self.assertIsNone(by_id["orphan"].parent_id)
            self.assertEqual(by_id["parent-id"].origin, "primary")
            self.assertIn(
                "OMP task agent: custom-worker",
                transcripts.read_brief(by_id["worker"]).recorded_via,
            )
            self.assertFalse(any(entry.actions for entry in entries))
            # Conflicting recorded parents must not create an arbitrary tree.
            write(
                artifacts / "orphan.jsonl",
                [
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
            cleared = omp.link_children([replace(e, parent_id=None) for e in entries])
            self.assertIsNone(
                next(e for e in cleared if e.session_id == "worker").parent_id
            )

    def test_omp_brief_tracks_provider_switches_and_latest_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {"type": "session", "id": "switch", "cwd": "/fictional"},
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "provider": "provider-a",
                        "model": "model-1",
                        "content": "one",
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "provider": "provider-a",
                        "model": "model-2",
                        "content": "two",
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "provider": "provider-b",
                        "model": "model-3",
                        "content": "three",
                    },
                },
            ]
            path = root / "switch.jsonl"
            path.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
            entry = omp.discover(
                root,
                [],
                False,
                asdu.ScanProgress(False),
                transcripts.ContentCache(False),
                None,
            )[0]
            brief = transcripts.read_brief(entry)
            self.assertEqual(entry.origin, "primary")
            self.assertEqual(brief.providers, ["provider-a", "provider-b"])
            self.assertEqual(brief.latest_model, "model-3")
            digest = asdu.digest(entry)
            self.assertIn("Provider: provider-a → provider-b", digest)
            self.assertIn("Model: model-3", digest)

    def test_unknown_alignment_and_omp_color(self):
        positions = []
        with patch.object(asdu, "color_attr", return_value=0):
            for source, origin in (
                ("codex", "primary"),
                ("claude", "subagent"),
                ("omp", "unknown"),
            ):
                screen = Screen([], width=120)
                asdu.draw_session_line(
                    screen,
                    2,
                    replace(session("example"), source=source, origin=origin),
                    False,
                )
                positions.append(
                    next(column for _, column, text, _ in screen.frame if "ago" in text)
                )
        self.assertEqual(len(set(positions)), 1)
        self.assertEqual(asdu.origin_label("unknown"), "?")
        self.assertNotIn(
            asdu.source_color("omp"),
            (0, asdu.source_color("codex"), asdu.source_color("claude")),
        )

    def test_omp_discovery_brief_keywords_and_scope(self):
        fixture = Path(__file__).parent / "fixtures/asdu/omp-test.jsonl"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = Path(shutil.copy(fixture, root))
            (root / "broken.jsonl").write_text("broken\n[]\n", encoding="utf-8")
            progress = asdu.ScanProgress(False)
            cache = transcripts.ContentCache(False)
            rules = [
                transcripts.TagRule("operations", keywords=("deployment",)),
                transcripts.TagRule("security", keywords=("security audit",)),
            ]
            entries = omp.discover(root, rules, True, progress, cache, None)
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry.key, ("omp", "omp-test-001"))
            self.assertIsNone(entry.parent_id)
            self.assertEqual(entry.size, path.stat().st_size)
            self.assertEqual(entry.tags, ("operations",))
            self.assertEqual(entry.title, "Inspect the moon-cheese deployment")
            self.assertEqual(entry.actions, frozenset())
            self.assertIn(root / "broken.jsonl", progress.invalid)
            self.assertIn(root / "broken.jsonl", progress.skipped)
            for preview in (False, True):
                brief = transcripts.read_brief(entry, preview=preview)
                self.assertIn("deployment", brief.first_user)
                self.assertIn("fixture inspected", brief.latest_reply)
                self.assertEqual(brief.event_counts["message"], 3)
                self.assertEqual(brief.providers, ["fictional-cloud"])
                self.assertEqual(brief.latest_model, "moon-model-v2")
            self.assertEqual(
                omp.discover(root, [], False, progress, cache, Path("/unrelated")), []
            )

    def test_omp_title_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            for role, expected in (
                ("user", "untitled — A substantive fictional request"),
                ("assistant", "untitled — reply: A substantive fictional request"),
            ):
                records = [
                    {"type": "session", "id": "example", "cwd": "/fictional"},
                    {
                        "type": "message",
                        "message": {
                            "role": role,
                            "content": "A substantive fictional request",
                        },
                    },
                ]
                path.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
                entries = omp.discover(
                    root,
                    [],
                    False,
                    asdu.ScanProgress(False),
                    transcripts.ContentCache(False),
                    None,
                )
                self.assertEqual(entries[0].title, expected)

    def test_read_only_actions_are_enforced_before_io(self):
        for source in ("omp", "future-database"):
            entry = replace(session("example"), source=source)
            with (
                patch.object(transcripts, "move_to_trash") as trash,
                patch.object(transcripts, "require_unchanged") as stat,
            ):
                for action in (transcripts.trash_session, transcripts.archive_session):
                    with self.assertRaisesRegex(OSError, "read-only"):
                        action(entry)
                trash.assert_not_called()
                stat.assert_not_called()

    def test_read_only_dialog_has_no_action_shortcuts(self):
        entry = replace(session("example"), source="omp")
        screen = Screen([ord("a"), ord("t"), 10])
        self.assertIsNone(asdu.confirm_session_action(screen, entry))
        self.assertTrue(
            any(
                "Read-only" in text
                for frame in screen.frames
                for _, _, text, _ in frame
            )
        )

    def test_registry_and_storage_identity(self):
        adapters = transcripts.source_adapters(
            Path("/codex"), Path("/claude"), Path("/omp")
        )
        self.assertEqual(set(adapters), set(READERS))
        entry = session("same")
        copy = replace(entry, path=Path("/copy.jsonl"))
        self.assertEqual(entry.key, copy.key)
        self.assertNotEqual(entry.storage_key, copy.storage_key)
        self.assertNotEqual(entry.key, replace(entry, source="claude").key)


if __name__ == "__main__":
    unittest.main()
