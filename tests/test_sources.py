"""Reader contracts and read-only safety, using fictional local files only."""

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_asdu import Screen, asdu, browser, session, transcripts

from asdu_sources import (
    READERS,
    available_actions,
    claude,
    codex,
    omp,
    perform_session_action,
    read_brief,
    session_controls,
    source_adapters,
)


def discovered_codex_title(*records: dict) -> str:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "sessions"
        root.mkdir()
        metadata = {
            "type": "session_meta",
            "payload": {"id": "example", "cwd": "/fictional", "source": "cli"},
        }
        (root / "rollout-example.jsonl").write_text(
            "\n".join(map(json.dumps, (metadata, *records))), encoding="utf-8"
        )
        entries = codex.discover(
            root,
            [],
            False,
            asdu.ScanProgress(False),
            transcripts.ContentCache(False),
            None,
        )
        return transcripts.session_label(entries[0])


class SourceTests(unittest.TestCase):
    def test_claude_agents_are_unique_children_of_their_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = {
                "type": "user",
                "sessionId": "parent-session",
                "cwd": "/fictional",
                "isSidechain": False,
                "message": {"role": "user", "content": "Main request"},
            }
            child = {
                "type": "user",
                "sessionId": "parent-session",
                "agentId": "child-agent",
                "cwd": "/fictional",
                "isSidechain": True,
                "message": {"role": "user", "content": "Child task"},
            }
            (root / "parent.jsonl").write_text(json.dumps(parent), encoding="utf-8")
            child_dir = root / "parent-session" / "subagents"
            child_dir.mkdir(parents=True)
            (child_dir / "agent-child.jsonl").write_text(
                json.dumps(child), encoding="utf-8"
            )

            entries = claude.discover(
                root,
                [],
                False,
                asdu.ScanProgress(False),
                transcripts.ContentCache(False),
                None,
            )
            by_id = {entry.session_id: entry for entry in entries}
            self.assertEqual(by_id["child-agent"].origin, "subagent")
            self.assertEqual(by_id["child-agent"].parent_id, "parent-session")
            self.assertEqual(
                browser.parent_links(entries)[by_id["child-agent"].storage_key],
                by_id["parent-session"].storage_key,
            )
            with patch.object(claude.subprocess, "run") as run:
                self.assertFalse(session_controls(by_id["child-agent"]).commands)
            run.assert_not_called()

    def test_claude_background_agent_exposes_native_controls(self):
        entry = replace(
            session("background-session"),
            source="claude",
            origin="primary",
        )
        result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "id": "backgrou",
                        "sessionId": "background-session",
                        "kind": "background",
                        "state": "blocked",
                    }
                ]
            ),
        )
        with patch.object(claude.subprocess, "run", return_value=result) as run:
            controls = session_controls(entry)
        self.assertEqual(controls.runtime_id, "backgrou")
        self.assertEqual(controls.runtime_kind, "background")
        self.assertEqual(controls.runtime_state, "blocked")
        self.assertEqual(
            [(command.label, command.argv) for command in controls.commands],
            [
                ("Attach", ("claude", "attach", "backgrou")),
                ("Logs", ("claude", "logs", "backgrou")),
                ("Stop", ("claude", "stop", "backgrou")),
                ("Remove", ("claude", "rm", "backgrou")),
            ],
        )
        self.assertEqual(run.call_args.args[0], ["claude", "agents", "--json", "--all"])

    def test_forks_join_the_tree_without_becoming_subagents(self):
        parent = session("parent")
        fork = replace(
            session("fork"),
            origin="primary",
            forked_from=parent.session_id,
        )
        self.assertEqual(
            browser.parent_links([parent, fork])[fork.storage_key],
            parent.storage_key,
        )
        self.assertEqual(fork.origin, "primary")

    def test_claude_latest_custom_title_precedes_generated_title(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "renamed.jsonl"
            records = [
                {
                    "type": "custom-title",
                    "customTitle": "Older name",
                    "sessionId": "renamed",
                },
                {
                    "type": "user",
                    "sessionId": "renamed",
                    "cwd": "/fictional",
                    "isSidechain": False,
                    "message": {"role": "user", "content": "Original request"},
                },
                {
                    "type": "custom-title",
                    "customTitle": "Renamed by the user",
                    "sessionId": "renamed",
                },
                {
                    "type": "ai-title",
                    "aiTitle": "Later generated title",
                    "sessionId": "renamed",
                },
            ]
            path.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")

            entries = claude.discover(
                root,
                [],
                False,
                asdu.ScanProgress(False),
                transcripts.ContentCache(False),
                None,
            )

            self.assertEqual(entries[0].title, "Renamed by the user")

    def test_claude_generated_title_and_model_are_native_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "generated.jsonl"
            records = [
                {
                    "type": "user",
                    "sessionId": "generated",
                    "cwd": "/fictional",
                    "isSidechain": False,
                    "message": {"role": "user", "content": "Original request"},
                },
                {
                    "type": "ai-title",
                    "aiTitle": "Native generated title",
                    "sessionId": "generated",
                },
                {
                    "type": "assistant",
                    "sessionId": "generated",
                    "message": {
                        "role": "assistant",
                        "model": "claude-fictional-1",
                        "content": [{"type": "text", "text": "Done"}],
                    },
                },
            ]
            path.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")

            entry = claude.discover(
                root,
                [],
                False,
                asdu.ScanProgress(False),
                transcripts.ContentCache(False),
                None,
            )[0]

            self.assertEqual(entry.title, "Native generated title")
            self.assertEqual(read_brief(entry).latest_model, "claude-fictional-1")

    def test_claude_background_name_is_a_native_title(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "type": "user",
                "sessionId": "background",
                "agentName": "Named background work",
                "cwd": "/fictional",
                "isSidechain": False,
                "message": {"role": "user", "content": "Fallback request"},
            }
            (root / "background.jsonl").write_text(json.dumps(record), encoding="utf-8")
            entries = claude.discover(
                root,
                [],
                False,
                asdu.ScanProgress(False),
                transcripts.ContentCache(False),
                None,
            )
            self.assertEqual(entries[0].title, "Named background work")

    def test_claude_discovery_ignores_non_session_jsonl_layouts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "session.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "session",
                        "cwd": "/fictional",
                        "isSidechain": False,
                        "message": {"role": "user", "content": "Real request"},
                    }
                ),
                encoding="utf-8",
            )
            plugin = project / "plugin" / "skill-injections.jsonl"
            plugin.parent.mkdir()
            plugin.write_text(json.dumps({"skill": "fixture"}), encoding="utf-8")
            progress = asdu.ScanProgress(False)

            entries = claude.discover(
                root,
                [],
                False,
                progress,
                transcripts.ContentCache(False),
                None,
            )

            self.assertEqual([entry.session_id for entry in entries], ["session"])
            self.assertNotIn(plugin, progress.skipped)

    def test_codex_session_without_title_material_shows_question_mark(self):
        self.assertEqual(discovered_codex_title(), "?")

    def test_codex_goal_is_the_title_before_an_acknowledgement(self):
        self.assertEqual(
            discovered_codex_title(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_goal_updated",
                        "goal": {"objective": "Prove a fictional theorem"},
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "ok"},
                },
            ),
            "Prove a fictional theorem",
        )

    def test_codex_brief_uses_native_provider_and_latest_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-metadata.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "metadata",
                        "cwd": "/fictional",
                        "source": "cli",
                        "model_provider": "fictional-provider",
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {"model": "fictional-model-v2"},
                },
            ]
            path.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
            entry = codex.discover(
                root,
                [],
                False,
                asdu.ScanProgress(False),
                transcripts.ContentCache(False),
                None,
            )[0]

            brief = read_brief(entry)
            self.assertEqual(brief.providers, ["fictional-provider"])
            self.assertEqual(brief.latest_model, "fictional-model-v2")

    def test_codex_short_request_and_native_archived_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            active_root = home / "sessions"
            archived_root = home / "archived_sessions"
            active_root.mkdir()
            archived_root.mkdir()

            def write(path, identifier, request):
                records = [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": identifier,
                            "cwd": "/fictional",
                            "source": "vscode",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": (
                                        "<system_instruction>generated wrapper"
                                        "</system_instruction>\n" + request
                                    ),
                                }
                            ],
                        },
                    },
                ]
                path.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")

            write(active_root / "rollout-active.jsonl", "active", "review")
            write(archived_root / "rollout-old.jsonl", "old", "old")
            entries = codex.discover(
                active_root,
                [],
                False,
                asdu.ScanProgress(False),
                transcripts.ContentCache(False),
                None,
            )
            by_id = {entry.session_id: entry for entry in entries}
            self.assertEqual(transcripts.session_label(by_id["active"]), "review")
            self.assertFalse(by_id["active"].archived)
            self.assertEqual(available_actions(by_id["active"]), {"archive", "delete"})
            self.assertTrue(by_id["old"].archived)
            self.assertEqual(available_actions(by_id["old"]), {"unarchive", "delete"})
            self.assertFalse(session_controls(by_id["old"]).commands)
            self.assertEqual(by_id["old"].source_home, str(home))

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
                brief = read_brief(entries[0], preview=preview)
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
            self.assertEqual(
                session_controls(entries[0]).commands[0].argv,
                ("omp", "--resume", "greeting"),
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
                read_brief(by_id["worker"]).recorded_via,
            )
            self.assertFalse(any(available_actions(entry) for entry in entries))
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

    def test_omp_header_lineage_accepts_session_ids_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent_path = root / "parent.jsonl"
            records = [{"type": "session", "id": "parent", "cwd": "/fictional"}]
            parent_path.write_text(json.dumps(records[0]), encoding="utf-8")
            for name, marker in (
                ("by-id", "parent"),
                ("by-path", str(parent_path)),
            ):
                (root / f"{name}.jsonl").write_text(
                    json.dumps(
                        {
                            "type": "session",
                            "id": name,
                            "cwd": "/fictional",
                            "parentSession": marker,
                        }
                    ),
                    encoding="utf-8",
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
            self.assertEqual(by_id["by-id"].parent_id, "parent")
            self.assertEqual(by_id["by-path"].parent_id, "parent")

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
            brief = read_brief(entry)
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
            self.assertEqual(available_actions(entry), frozenset())
            self.assertIn(root / "broken.jsonl", progress.invalid)
            self.assertIn(root / "broken.jsonl", progress.skipped)
            for preview in (False, True):
                brief = read_brief(entry, preview=preview)
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
        entry = replace(session("example"), source="omp")
        with (
            patch.object(transcripts, "move_to_trash") as trash,
            patch.object(transcripts, "archive_session") as archive,
        ):
            for action in ("trash", "archive"):
                with self.assertRaisesRegex(OSError, "read-only"):
                    perform_session_action(entry, action)
            trash.assert_not_called()
            archive.assert_not_called()

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
        adapters = source_adapters(Path("/codex"), Path("/claude"), Path("/omp"))
        self.assertEqual(set(adapters), set(READERS))
        entry = session("same")
        copy = replace(entry, path=Path("/copy.jsonl"))
        self.assertEqual(entry.key, copy.key)
        self.assertNotEqual(entry.storage_key, copy.storage_key)
        self.assertNotEqual(entry.key, replace(entry, source="claude").key)


if __name__ == "__main__":
    unittest.main()
