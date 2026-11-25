#!/usr/bin/env python3
"""Dry checks for the description harness. No model calls."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import adoption  # noqa: E402
import desc_variant  # noqa: E402
import stats  # noqa: E402
from grok_transcript import parse_streaming_json  # noqa: E402

FIXTURE = HERE / "fixtures" / "grok_streaming" / "dropin_sample.jsonl"


class AdoptionTests(unittest.TestCase):
    def kind(self, tool, args=None):
        return adoption.classify_call(tool, args or {})

    def test_speedread_names(self):
        self.assertEqual(self.kind("speedread-read"), "speedread")
        self.assertEqual(self.kind("speedread-search"), "speedread")
        self.assertEqual(self.kind("speedread-map"), "speedread")
        self.assertEqual(self.kind("speedread-trace"), "speedread")
        self.assertEqual(self.kind("speedread__read"), "speedread")
        # A tool that does not exist yet still counts.
        self.assertEqual(self.kind("speedread-context"), "speedread")
        self.assertEqual(self.kind("speedread__context"), "speedread")
        self.assertTrue(adoption.is_trace_tool("speedread-trace"))
        self.assertTrue(adoption.is_trace_tool("speedread__trace"))
        self.assertFalse(adoption.is_trace_tool("speedread-search"))

    def test_use_tool_unwraps_grok_catalog_key(self):
        self.assertEqual(self.kind("use_tool", {"tool_name": "speedread__read", "tool_input": {}}), "speedread")
        self.assertEqual(self.kind("search_tool", {"query": "read"}), "other")

    def test_native_and_edits(self):
        for name in ("view", "grep", "glob", "rg", "read_file", "list_dir", "glob"):
            self.assertEqual(self.kind(name), "native", name)
        for name in ("search_replace", "edit", "write"):
            self.assertEqual(self.kind(name, {"path": "a"}), "other", name)

    def test_shell_reader_runner_and_sed(self):
        self.assertEqual(self.kind("bash", {"command": "cat src/a.py"}), "shell")
        self.assertEqual(self.kind("run_terminal_cmd", {"command": "head -n 5 src/a.py"}), "shell")
        self.assertEqual(self.kind("bash", {"command": "rg -n foo src"}), "shell")
        self.assertEqual(self.kind("bash", {"command": "git grep -n foo"}), "shell")
        # A reader chained with a test is an execution step, not a code-reading step.
        self.assertEqual(self.kind("bash", {"command": "cat src/a.py && pytest"}), "other")
        self.assertEqual(self.kind("bash", {"command": "go test ./..."}), "other")
        self.assertEqual(self.kind("bash", {"command": "sed -n '1,20p' src/a.py"}), "shell")
        self.assertEqual(self.kind("bash", {"command": "sed -i 's/a/b/' src/a.py"}), "other")
        self.assertEqual(self.kind("bash", {"command": "echo hello"}), "other")
        self.assertEqual(self.kind("bash", {"command": 'echo "cat && pytest"'}), "other")
        # The quoted operator is not a split; the real operator is.
        self.assertEqual(self.kind("bash", {"command": 'echo "a && b" && cat src/a.py'}), "shell")

    def test_shares_ignore_non_reads(self):
        calls = [
            {"tool": "speedread__read"},
            {"tool": "read_file"},
            {"tool": "run_terminal_cmd", "args": {"command": "ls src"}},
            {"tool": "search_replace", "args": {"path": "a"}},
            {"tool": "run_terminal_cmd", "args": {"command": "pytest"}},
        ]
        s = adoption.summarize_calls(calls)
        self.assertTrue(s["used_speedread"])
        self.assertEqual(s["code_read_steps"], 3)
        self.assertEqual(s["speedread_steps"], 1)
        self.assertEqual(s["native_steps"], 1)
        self.assertEqual(s["shell_steps"], 1)
        self.assertEqual(s["other_steps"], 2)
        self.assertAlmostEqual(s["speedread_share"], 1 / 3)

    def test_prompts_for_unprompted_arms_are_clean(self):
        for name in ("agent_tasks.json", "relationship_tasks.json", "chromium_tasks.json", "coding_tasks.json"):
            suite = json.loads((HERE / name).read_text())
            for task in suite["tasks"]:
                bad = adoption.prompt_violations(task["prompt"])
                self.assertEqual(bad, [], f"{name} {task['id']} mentions {bad}")


class GrokTranscriptTests(unittest.TestCase):
    def test_fixture_is_labeled_and_classifies(self):
        note = (FIXTURE.parent / "README.md").read_text().lower()
        self.assertIn("not a live", note)
        parsed = parse_streaming_json(FIXTURE.read_text())
        self.assertEqual(parsed["session_id"], "11111111-1111-4111-8111-111111111111")
        self.assertIn("628", parsed["answer"])
        self.assertEqual(parsed["bad_lines"], 0)
        summary = adoption.summarize_calls(parsed["calls"])
        self.assertEqual(summary["speedread_steps"], 2)  # speedread__read and future speedread__context
        self.assertEqual(summary["native_steps"], 1)
        self.assertEqual(summary["shell_steps"], 1)  # cat | head
        self.assertEqual(summary["other_steps"], 2)  # pytest chain and search_replace
        self.assertTrue(summary["used_speedread"])
        names = summary["tools"]
        self.assertIn("speedread__read", names)
        self.assertIn("speedread__context", names)
        self.assertNotIn("use_tool", names)

    def test_acp_envelope_is_not_dropped(self):
        line = json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": "s", "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "a",
                "name": "speedread__map",
                "rawInput": {"path": "."},
                "status": "pending",
            }},
        })
        parsed = parse_streaming_json(line)
        self.assertEqual(adoption.summarize_calls(parsed["calls"])["speedread_steps"], 1)


class VariantTests(unittest.TestCase):
    def test_current_sets_no_description_env(self):
        self.assertIsNone(desc_variant.load_variant("current"))
        env = desc_variant.description_env("current", rss_log="/tmp/rss.txt")
        self.assertNotIn(desc_variant.ENV, env)
        self.assertEqual(env[desc_variant.RSS_ENV], "/tmp/rss.txt")
        toml = desc_variant.grok_config("/usr/bin/speedread", "current", rss_log="/tmp/rss.txt")
        self.assertNotIn("SPEEDREAD_DESCRIPTION_FILE", toml)
        self.assertIn("SPEEDREAD_RSS_LOG", toml)
        self.assertFalse((desc_variant.DESCRIPTIONS / "lean.json").exists())

    def test_variant_file_is_the_only_text(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lean.json"
            path.write_text(json.dumps({"tools": {"read": "SHORTER READ"}}))
            # The loader only reads evals/descriptions. A temp file is applied
            # by pointing the env at it, which is what the server tests do.
            cfg = desc_variant.copilot_mcp_config("/bin/speedread", "current")
            self.assertNotIn("env", cfg["mcpServers"]["speedread"])
            self.assertEqual(path.read_text().count("SHORTER READ"), 1)

    def test_missing_variant_exits(self):
        with self.assertRaises(SystemExit):
            desc_variant.load_variant("does-not-exist")


class StatsTests(unittest.TestCase):
    def test_cluster_bootstrap_runs(self):
        recs = []
        for task in ("a", "b", "c", "d"):
            for cond, used in (("baseline", False), ("dropin", True)):
                recs.append({
                    "task": task, "condition": cond, "model": "grok-build",
                    "used_speedread": used, "speedread_share": 1.0 if used else 0.0,
                    "score": 1.0, "pass": True, "status": "ok",
                })
        table = stats.adoption_table(recs, arm="dropin", n=200)
        self.assertIn("trial adoption", table)
        self.assertIn("1.000", table)


if __name__ == "__main__":
    unittest.main()
