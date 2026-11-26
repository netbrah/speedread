#!/usr/bin/env python3
"""Dry checks for the description harness. No model calls."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import adoption  # noqa: E402
import desc_variant  # noqa: E402
import endpoint  # noqa: E402
import fetch_corpora  # noqa: E402
import grok_eval  # noqa: E402
import run_baseline  # noqa: E402
import mcp_allow  # noqa: E402
import stats  # noqa: E402
from grok_transcript import normalize_usage, parse_streaming_json  # noqa: E402

FIXTURE = HERE / "fixtures" / "grok_streaming" / "dropin_sample.jsonl"


class AdoptionTests(unittest.TestCase):
    def kind(self, tool, args=None):
        return adoption.classify_call(tool, args or {})

    def test_speedread_names(self):
        self.assertEqual(self.kind("speedread-read"), "speedread")
        self.assertEqual(self.kind("speedread-search"), "speedread")
        self.assertEqual(self.kind("speedread-map"), "speedread")
        self.assertEqual(self.kind("speedread__read"), "speedread")
        self.assertEqual(self.kind("speedread__search"), "speedread")
        self.assertEqual(self.kind("speedread__map"), "speedread")
        # trace is leaving and context did not land: reported, not counted.
        for name in ("speedread-trace", "speedread__trace", "speedread__context", "speedread-context"):
            self.assertEqual(self.kind(name), "other", name)
        extra = adoption.summarize_calls([
            {"tool": "speedread__read"},
            {"tool": "speedread__trace"},
            {"tool": "use_tool", "args": {"tool_name": "speedread__context", "tool_input": {}}},
        ])
        self.assertTrue(extra["used_speedread"])
        self.assertEqual(extra["speedread_steps"], 1)
        self.assertEqual(extra["other_speedread_calls"], 2)
        self.assertIn("speedread__context", extra["other_speedread_tools"])

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
        self.assertEqual(self.kind("run_terminal_command", {"command": "head -n 5 src/a.py"}), "shell")
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
            {"tool": "run_terminal_command", "args": {"command": "ls src"}},
            {"tool": "search_replace", "args": {"path": "a"}},
            {"tool": "run_terminal_command", "args": {"command": "pytest"}},
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
        for name in ("agent_tasks.json", "relationship_tasks.json", "chromium_tasks.json", "coding_tasks.json", "pin_tasks.json"):
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
        self.assertEqual(summary["speedread_steps"], 1)  # speedread__read only
        self.assertEqual(summary["native_steps"], 1)
        self.assertEqual(summary["shell_steps"], 1)  # cat | head
        self.assertEqual(summary["other_steps"], 3)  # pytest chain, search_replace, context
        self.assertEqual(summary["other_speedread_calls"], 1)  # speedread__context, not adoption
        self.assertIn("speedread__context", summary["other_speedread_tools"])
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
    def test_trace_and_context_keys_are_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "lean.json").write_text(json.dumps({
                "instructions": "SHORT",
                "tools": {"read": "SHORT READ", "trace": "NO", "context": "NO"},
            }))
            old = desc_variant.DESCRIPTIONS
            desc_variant.DESCRIPTIONS = root
            try:
                doc = desc_variant.load_variant("lean")
                env = desc_variant.description_env("lean", variant_out=root / "served.json")
            finally:
                desc_variant.DESCRIPTIONS = old
            self.assertEqual(set(doc["tools"]), {"read"})
            served = json.loads((root / "served.json").read_text())
            self.assertEqual(set(served["tools"]), {"read"})
            self.assertEqual(env["SPEEDREAD_DESCRIPTION_FILE"], str((root / "served.json").resolve()))

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


class SmokeShapeTests(unittest.TestCase):
    """Shapes taken from the grok 1.0.92 smoke. Not a baseline score."""

    def test_short_stderr_is_a_slice(self):
        self.assertEqual(grok_eval.tail(""), "")
        self.assertEqual(grok_eval.tail("hi"), "hi")
        self.assertEqual(grok_eval.tail("x" * 500), "x" * 400)
        # The bug was `(err or "")[-400]`, which raises IndexError below 400 chars.
        self.assertEqual(("" or "")[-400:], "")

    def test_argv_trusts_the_checkout_and_exclusive_denies_edit_tools(self):
        cmd = grok_eval.grok_argv("Say ok.", "grok-4.7", "/tmp/repo", "11111111-1111-4111-8111-111111111111", "dropin", 4)
        self.assertIn("--trust", cmd)
        self.assertNotIn("read_file", ",".join(cmd))
        exclusive = grok_eval.grok_argv("Say ok.", "grok-4.7", "/tmp/repo", "11111111-1111-4111-8111-111111111111", "exclusive", 4)
        disallowed = exclusive[exclusive.index("--disallowed-tools") + 1].split(",")
        for tool in grok_eval._EXCLUSIVE_DENY:
            self.assertIn(tool, disallowed)
        self.assertEqual(disallowed[:6], list(grok_eval._EXCLUSIVE_DENY))
        # Present as a call block only. The catalog filter is mcp_allow.py, in the config below.
        self.assertIn("MCPTool(speedread__trace)", exclusive)
        cfg = desc_variant.grok_config("/bin/speedread", "current")
        self.assertNotIn("skip_read_before_edit", cfg)
        self.assertNotIn("[toolset.search_replace]", cfg)
        self.assertIn("mcp_allow.py", cfg)
        self.assertIn("map,search,read", cfg)

    def test_init_failure_aborts_and_a_short_error_does_not(self):
        self.assertTrue(grok_eval.initialization_failure({
            "stderr_tail": "Couldn't start session: search_replace requires a Read tool",
            "errors": [],
        }))
        self.assertTrue(grok_eval.initialization_failure({
            "stderr_tail": "Couldn't create session",
            "errors": [],
        }))
        self.assertFalse(grok_eval.initialization_failure({
            "stderr_tail": "hi",
            "errors": [],
            "status": "error",
        }))

    def test_usage_input_includes_cache_and_is_not_added_again(self):
        # grok usage: inputTokens already includes cache. 15004 = 6172 + 8832.
        ledger = normalize_usage(session={
            "inputTokens": 15004,
            "cachedReadTokens": 8832,
            "outputTokens": 400,
            "totalTokens": 15404,
            "primaryModelId": "grok-4.7-build",
        })
        self.assertTrue(ledger["input_includes_cache"])
        self.assertEqual(ledger["input_tokens"], 15004)
        self.assertEqual(ledger["cache_read_tokens"], 8832)
        self.assertEqual(ledger["usage_source"], "grok usage")
        self.assertEqual(ledger["usage_model"], "grok-4.7-build")
        self.assertNotEqual(
            ledger["total_tokens"],
            ledger["input_tokens"] + ledger["cache_read_tokens"] + ledger["output_tokens"],
        )
        # Stream end: input_tokens excludes cache. 11617 + 35456 + 255 = 47328.
        stream = normalize_usage(end={"usage": {
            "input_tokens": 11617,
            "cache_read_input_tokens": 35456,
            "cache_creation_input_tokens": 0,
            "output_tokens": 255,
            "total_tokens": 47328,
        }})
        self.assertEqual(stream["input_tokens"], 11617 + 35456)
        self.assertEqual(stream["total_tokens"], 47328)
        self.assertEqual(stream["input_tokens"] + stream["output_tokens"], 47328)
        self.assertNotEqual(
            stream["total_tokens"],
            stream["input_tokens"] + stream["cache_read_tokens"] + stream["output_tokens"],
        )
        # A present ledger wins; its inclusive input is not rebuilt from the stream.
        both = normalize_usage(
            session={"inputTokens": 15004, "cachedReadTokens": 8832, "outputTokens": 400},
            end={"usage": {"input_tokens": 11617, "cache_read_input_tokens": 35456, "output_tokens": 255, "total_tokens": 47328}},
        )
        self.assertEqual(both["input_tokens"], 15004)
        self.assertEqual(both["usage_source"], "grok usage")


class StatsTests(unittest.TestCase):
    def test_cluster_bootstrap_runs(self):
        recs = []
        for task in ("a", "b", "c", "d"):
            for cond, used in (("baseline", False), ("dropin", True)):
                recs.append({
                    "task": task, "condition": cond, "model": "grok-4.7",
                    "used_speedread": used, "speedread_share": 1.0 if used else 0.0,
                    "score": 1.0, "pass": True, "status": "ok",
                })
        table = stats.adoption_table(recs, arm="dropin", n=200)
        self.assertIn("trial adoption", table)
        self.assertIn("1.000", table)


class FilterTests(unittest.TestCase):
    def test_tools_list_keeps_map_search_read_and_drops_trace(self):
        read = {"name": "read", "description": "trace is a word in this description"}
        line = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"tools": [read, {"name": "trace", "description": "the tool"}, {"name": "map", "description": "m"}, {"name": "search", "description": "s"}]},
        }, separators=(",", ":")) + "\n"
        filtered = mcp_allow.filter_tools_message(line, set(mcp_allow.ALLOW))
        names = [tool["name"] for tool in json.loads(filtered)["result"]["tools"]]
        self.assertEqual(names, ["read", "map", "search"])
        self.assertIn("trace is a word in this description", filtered)
        self.assertNotIn('"name":"trace"', filtered)
        self.assertIsNotNone(mcp_allow.blocked_call(
            '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"trace","arguments":{}}}\n',
            set(mcp_allow.ALLOW),
        ))
        self.assertIsNone(mcp_allow.blocked_call(
            '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"read","arguments":{}}}\n',
            set(mcp_allow.ALLOW),
        ))

    def test_preflight_fails_when_the_server_still_lists_trace(self):
        fake = Path(tempfile.mkdtemp()) / "fake_mcp.py"
        fake.write_text(textwrap.dedent("""\
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                method = msg.get("method")
                if method == "initialize":
                    body = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": body}) + "\\n")
                elif method == "tools/list":
                    tools = [{"name": name, "description": "d"} for name in ("map", "read", "search", "trace")]
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}}) + "\\n")
                elif method == "notifications/initialized":
                    continue
                else:
                    continue
                sys.stdout.flush()
        """))
        raw = mcp_allow.listed_names([sys.executable, str(fake)])
        self.assertIn("trace", raw)
        self.assertIsNotNone(mcp_allow.catalog_error(raw))
        filtered = mcp_allow.listed_names([
            sys.executable, str(Path(mcp_allow.__file__).resolve()),
            "--allow", "map,search,read", "--", sys.executable, str(fake),
        ])
        self.assertIsNone(mcp_allow.catalog_error(filtered))
        self.assertEqual(set(filtered), {"map", "search", "read"})

    def test_session_stream_with_trace_is_not_a_three_tool_catalog(self):
        leaked = '{"available_commands":[{"name":"speedread__map"},{"name":"speedread__search"},{"name":"speedread__read"},{"name":"speedread__trace"}]}\n'
        self.assertIn("trace", grok_eval.catalog_problem(leaked))
        clean = '{"available_commands":[{"name":"speedread__map"},{"name":"speedread__search"},{"name":"speedread__read"}]}\n'
        self.assertIsNone(grok_eval.catalog_problem(clean))

    def test_exclusive_argv_starts_on_a_fake_grok_that_requires_no_search_replace(self):
        """The fake grok fails the way 1.0.92 did: search_replace without read_file."""
        script = Path(tempfile.mkdtemp()) / "fake-grok.py"
        script.write_text(textwrap.dedent("""\
            import sys
            argv = sys.argv
            disallowed = ""
            if "--disallowed-tools" in argv:
                disallowed = argv[argv.index("--disallowed-tools") + 1]
            denied = set(disallowed.split(","))
            if "read_file" in denied and "search_replace" not in denied:
                sys.stderr.write("Couldn't create session: search_replace requires a Read tool\\n")
                sys.exit(1)
            sys.stdout.write('{"available_commands":[{"name":"speedread__map"},{"name":"speedread__search"},{"name":"speedread__read"}]}\\n')
            sys.exit(0)
        """))
        exclusive = grok_eval.grok_argv("pong", "grok-4.7", "/tmp/repo", "11111111-1111-4111-8111-111111111111", "exclusive", 1)
        exclusive[0] = sys.executable
        exclusive.insert(1, str(script))
        started = subprocess.run(exclusive, capture_output=True, text=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIsNone(grok_eval.catalog_problem(started.stdout))
        broken = [sys.executable, str(script), "--disallowed-tools", "read_file,grep,list_dir"]
        failed = subprocess.run(broken, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("Couldn't create session", failed.stderr)


class PlanTests(unittest.TestCase):
    def test_plan_follows_the_task_files(self):
        counts = run_baseline.plan_counts()
        self.assertEqual(counts["suite3"], 10)
        self.assertEqual(counts["pin"], 13)
        self.assertEqual(counts["chromium"], 14)
        self.assertEqual(counts["coding"], 8)
        self.assertEqual(counts["decision_tasks"], 37)
        self.assertEqual(counts["decision_trials_per_arm_per_model"], 185)
        self.assertEqual(counts["decision_trials"], 740)
        self.assertEqual(counts["exclusive_trials"], 74)
        self.assertEqual(counts["coding_trials"], 128)
        self.assertEqual(counts["total_trials"], 942)
        payload = run_baseline.plan_payload({})
        self.assertEqual(payload["decision_trials_per_arm_per_model"], 185)
        self.assertEqual(payload["total_trials"], 942)
        self.assertEqual(payload["estimated_spend_usd"], 742)
        blob = json.dumps(payload)
        self.assertNotIn("about 500", blob)
        self.assertNotIn("110", blob)

    def test_chromium_sentinels_match_the_checked_tree(self):
        # Lines read from gitiles at 3a7520d2d79d6569d3a11f968d32b20665fa4db3.
        # render_frame_host_impl.cc:18234 is the comment; the function is 18236.
        lock = json.loads((HERE / "corpora.json").read_text())["repos"]["chromium"]
        sent = {(s["path"], s["line"]): s["contains"] for s in lock["sentinels"]}
        expect = {
            ("chrome/VERSION", 1): "MAJOR=160",
            ("chrome/VERSION", 3): "BUILD=8155",
            ("url/gurl.h", 92): "is_valid",
            ("url/gurl.h", 159): "Resolve",
            ("url/gurl.h", 232): "IsStandard",
            ("url/gurl.h", 236): "IsAboutBlank",
            ("url/gurl.h", 240): "IsAboutSrcdoc",
            ("url/gurl.h", 252): "SchemeIsHTTPOrHTTPS",
            ("base/version.h", 45): "IsValid",
            ("base/version.h", 54): "CompareTo",
            ("base/version.h", 63): "GetString",
            ("base/command_line.h", 16): "BASE_COMMAND_LINE_H_",
            ("base/command_line.h", 38): "namespace base",
            ("content/browser/renderer_host/render_frame_host_impl.cc", 18236): "DidCommitNavigation",
            ("content/renderer/render_frame_impl.cc", 4029): "DidCommitNavigation",
        }
        self.assertEqual(sent, expect)
        tasks = json.loads((HERE / "chromium_tasks.json").read_text())["tasks"]
        rfh = next(t for t in tasks if t["id"] == "chromium-rfh-did-commit")
        self.assertIn(r"\b18236\b", rfh["graders"])
        self.assertNotIn("18234", json.dumps(rfh["graders"]))

    def test_flask_requirements_are_pinned_to_the_corpora_commit(self):
        text = (HERE / "flask-tests.txt").read_text()
        pin = json.loads((HERE / "corpora.json").read_text())["repos"]["flask"]["commit"]
        self.assertIn(pin, text)
        self.assertIn("pytest==", text)
        self.assertIn("--hash=sha256:", text)
        self.assertNotIn("flask==", text.lower())


class PinTests(unittest.TestCase):
    def test_fetcher_requires_full_shas_and_chromium_is_pinned(self):
        with self.assertRaises(SystemExit):
            fetch_corpora.require_full_sha("3a7520d2d79d", "chromium")
        full = "3a7520d2d79d6569d3a11f968d32b20665fa4db3"
        self.assertEqual(fetch_corpora.require_full_sha(full, "chromium"), full)
        lock = json.loads((HERE / "corpora.json").read_text())
        for name, spec in lock["repos"].items():
            fetch_corpora.require_full_sha(spec["commit"], name)
        self.assertEqual(lock["repos"]["chromium"]["commit"], full)


class EndpointTests(unittest.TestCase):
    SECRET = "super-secret-key-value"

    def test_flag_beats_env_beats_default(self):
        env = {
            endpoint.ENV_BASE_URL: "https://from-env.example/v1",
            endpoint.ENV_MODELS: "env-model",
            endpoint.ENV_API_KEY_ENV: "ENV_KEY_NAME",
        }
        got = endpoint.resolve(None, None, None, default_models="default-model", environ={})
        self.assertIsNone(got["base_url"])
        self.assertEqual(got["models"], ["default-model"])
        self.assertIsNone(got["api_key_env"])
        got = endpoint.resolve(None, None, None, default_models="default-model", environ=env)
        self.assertEqual(got["base_url"], "https://from-env.example/v1")
        self.assertEqual(got["models"], ["env-model"])
        self.assertEqual(got["api_key_env"], "ENV_KEY_NAME")
        got = endpoint.resolve(
            "https://from-flag.example/v1/", "flag-a,flag-b", "FLAG_KEY_NAME",
            default_models="default-model", environ=env,
        )
        self.assertEqual(got["base_url"], "https://from-flag.example/v1")
        self.assertEqual(got["models"], ["flag-a", "flag-b"])
        self.assertEqual(got["api_key_env"], "FLAG_KEY_NAME")

    def test_custom_base_url_requires_a_key_var_name(self):
        with self.assertRaises(SystemExit):
            endpoint.resolve("https://gw.example/v1", "m", None, default_models="m", environ={})

    def test_config_names_the_key_var_and_not_the_secret(self):
        ep = endpoint.resolve(
            "https://gw.example/v1", "onprem.a", "ONPREM_API_KEY",
            default_models="grok-4.7", environ={endpoint.ENV_API_KEY_ENV: "OTHER"},
        )
        toml = endpoint.model_sections(ep)
        self.assertIn('base_url = "https://gw.example/v1"', toml)
        self.assertIn('env_key = "ONPREM_API_KEY"', toml)
        self.assertIn('api_backend = "chat_completions"', toml)
        self.assertIn('[model."onprem.a"]', toml)
        self.assertNotIn(self.SECRET, toml)
        self.assertNotIn("api_key", toml)
        view = json.dumps(endpoint.public_view(ep, secret=self.SECRET))
        self.assertNotIn(self.SECRET, view)
        self.assertEqual(endpoint.model_sections({"base_url": None, "models": ["grok-4.7"], "api_key_env": None}), "")

    def test_preflight_against_a_local_stub_does_not_print_the_key(self):
        secret = self.SECRET
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization")
                body = json.dumps({"object": "list", "data": [{"id": "onprem-a"}, {"id": "onprem-b"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}/v1"
            env = {"ONPREM_API_KEY": secret}
            ep = {"base_url": base, "models": ["onprem-a"], "api_key_env": "ONPREM_API_KEY"}
            report = endpoint.preflight_report(ep, environ=env)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen["path"], "/v1/models")
        self.assertEqual(seen["auth"], "Bearer " + secret)
        self.assertIn("onprem-a", report)
        self.assertIn("onprem-b", report)
        self.assertIn("key_env ONPREM_API_KEY is set", report)
        self.assertNotIn(secret, report)

    def test_plan_and_cli_preflight_hide_the_key(self):
        secret = self.SECRET
        ep = {
            "base_url": f"http://127.0.0.1/v1?token={secret}",
            "models": ["onprem-a"],
            "api_key_env": "ONPREM_API_KEY",
        }
        text = run_baseline.plan_text({}, ep, secret=secret)
        self.assertNotIn(secret, text)
        self.assertIn("ONPREM_API_KEY", text)
        self.assertIn("[redacted]", text)
        clean = dict(ep, base_url="http://127.0.0.1:9/v1")
        home = grok_eval.prepare_grok_home(Path("/no/such"), "/bin/speedread", "current", None, False, clean)
        try:
            toml = (home / "config.toml").read_text()
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)
        self.assertIn('env_key = "ONPREM_API_KEY"', toml)
        self.assertNotIn(secret, toml)

    def test_run_baseline_preflight_cli_against_a_stub(self):
        secret = self.SECRET

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"data": [{"id": "onprem-a"}, {"id": "onprem-b"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}/v1"
            env = os.environ.copy()
            env["ONPREM_API_KEY"] = secret
            env.pop(endpoint.ENV_BASE_URL, None)
            env.pop(endpoint.ENV_MODELS, None)
            env.pop(endpoint.ENV_API_KEY_ENV, None)
            proc = subprocess.run(
                [sys.executable, str(HERE / "run_baseline.py"), "--preflight",
                 "--base-url", base, "--models", "onprem-a", "--api-key-env", "ONPREM_API_KEY"],
                cwd=HERE.parent, env=env, capture_output=True, text=True,
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("onprem-a", proc.stdout)
        self.assertIn("onprem-b", proc.stdout)
        self.assertNotIn(secret, proc.stdout)
        self.assertNotIn(secret, proc.stderr)
        self.assertNotIn("fixture self-check", proc.stdout)


if __name__ == "__main__":
    unittest.main()
