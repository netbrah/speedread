"""Agent half of the definition-token gate.

Runs on a machine with Grok CLI or Copilot CLI. The measurement VM does not
execute this. One command:

  python3 evals/definition_cost.py agent --harness grok \\
      --binary "$PWD/target/fast/speedread" --model <id> --yes

`--model` is required. Pass an id `grok models` lists on that machine.
Without `--yes` the command prints the plan and exits.

Copilot is the same gate with `--harness copilot`. It is not the adoption
experiment in evals/agent_eval.py: sub-agents and web tools are off in every
condition, and the exclusive condition also disallows the harness file tools
so a shell read cannot pass as tool selection.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from definition_cost import FIXTURE, FLAGS, host_info, load_tasks  # noqa: E402
from definition_mcp import Mcp  # noqa: E402
from definition_plan import (  # noqa: E402
    PROBE_CONDITIONS,
    PROBE_PROMPT,
    TASKS_PER_TOOL,
    TOOLS,
)

# Grok reaches MCP tools as `<server>__<tool>` and, per the session docs, also
# through search_tool / use_tool. `context` is recognised so a fifth tool is
# scored as itself rather than dropped.
SPEEDREAD_TOOLS = ("read", "search", "map", "trace", "context")
GROK_FILE_TOOLS = ("read_file", "grep", "list_dir", "bash")
COPILOT_DB = Path.home() / ".copilot" / "session-store.db"


def match_tool(name: str) -> str | None:
    low = name.strip().lower()
    for tool in SPEEDREAD_TOOLS:
        if low == tool or low.endswith("__" + tool) or low.endswith("-" + tool) or low.endswith("/" + tool):
            return tool
    return None


def nested_tool(raw, depth: int = 0):
    if depth > 6:
        return None
    if isinstance(raw, dict):
        for key in ("name", "tool", "tool_name", "toolName"):
            if isinstance(raw.get(key), str):
                found = match_tool(raw[key])
                if found:
                    args = raw.get("arguments", raw.get("args", raw.get("input", {})))
                    if not isinstance(args, dict):
                        args = {}
                    return {"tool": found, "arguments": args, "via": raw[key]}
        for value in raw.values():
            found = nested_tool(value, depth + 1)
            if found:
                return found
    elif isinstance(raw, list):
        for value in raw:
            found = nested_tool(value, depth + 1)
            if found:
                return found
    elif isinstance(raw, str):
        found = match_tool(raw)
        if found:
            return {"tool": found, "arguments": {}, "via": raw, "arguments_lost": True}
    return None


def resolve_call(tool_name: str, raw) -> dict | None:
    direct = match_tool(tool_name or "")
    if direct:
        args = raw if isinstance(raw, dict) else {}
        return {"tool": direct, "arguments": args, "via": tool_name}
    return nested_tool(raw)


def parse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def assistant_text(events: list[dict]) -> str:
    parts = []
    for event in events:
        if event.get("type") == "text" and isinstance(event.get("data"), str):
            parts.append(event["data"])
        elif event.get("type") == "assistant":
            content = ((event.get("message") or {}).get("content")) or []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
        elif event.get("type") == "result" and isinstance(event.get("result"), str):
            parts.append(event["result"])
        data = event.get("data") or {}
        if event.get("type") == "assistant.message" and isinstance(data, dict) and isinstance(data.get("content"), str):
            parts.append(data["content"])
    # Copilot's final assistant.message is the answer; streaming text chunks are the answer
    # for grok. Prefer the last assistant.message when present so chunked text is not doubled
    # with a result line.
    messages = []
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "assistant.message" and isinstance(data, dict) and isinstance(data.get("content"), str):
            messages.append(data["content"])
    if messages:
        return messages[-1]
    return "".join(parts)


def session_id_of(events: list[dict]) -> str | None:
    for event in events:
        for key in ("sessionId", "session_id"):
            if isinstance(event.get(key), str):
                return event[key]
        data = event.get("data")
        if isinstance(data, dict) and isinstance(data.get("sessionId"), str):
            return data["sessionId"]
    return None


def speedread_calls(events: list[dict]) -> list[dict]:
    found = []
    for event in events:
        if event.get("type") == "tool_call":
            resolved = resolve_call(event.get("toolName") or "", event.get("rawInput"))
            if resolved:
                found.append(resolved)
            continue
        if event.get("type") == "tool.execution_start":
            data = event.get("data") or {}
            resolved = resolve_call(data.get("toolName") or "", data.get("arguments"))
            if resolved:
                found.append(resolved)
            continue
        if event.get("type") != "assistant":
            continue
        content = ((event.get("message") or {}).get("content")) or []
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                resolved = resolve_call(block.get("name") or "", block.get("input"))
                if resolved:
                    found.append(resolved)
    return found


def surface_ok(events: list[dict], exclusive: bool):
    """True/False when a tools list was observed, else None (unverified)."""
    names = None
    for event in events:
        tools = event.get("tools")
        if event.get("type") in ("available_commands", "system") and isinstance(tools, list):
            names = tools
    if names is None:
        return None
    flat = {item if isinstance(item, str) else str(item) for item in names}
    if not exclusive:
        return True
    leaked = set(GROK_FILE_TOOLS) & flat
    has_entry = any(
        name == "use_tool" or name == "search_tool" or name.startswith("speedread")
        for name in flat
    )
    return not leaked and has_entry


def extract_full_input(obj: dict) -> tuple[int | None, dict]:
    """Full prompt tokens for turn 1: uncached + cache read + cache creation.

    Grok's headless `input_tokens` is the uncached bucket. Copilot's first-row
    `input_tokens` is used as-is when that is all the row has. The raw object
    is always kept; a missing bucket is not filled in with a guess.
    """
    turn = None
    turns = obj.get("turns") if isinstance(obj, dict) else None
    if isinstance(turns, list) and turns:
        turn = turns[0]
    elif isinstance(obj.get("session"), dict):
        turn = obj["session"]
    else:
        turn = obj
    blobs = []
    if isinstance(turn, dict):
        blobs.append(turn)
        if isinstance(turn.get("usage"), dict):
            blobs.append(turn["usage"])
    found = {}
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "cache_read_input_tokens": ("cache_read_input_tokens", "cacheReadInputTokens"),
        "cache_creation_input_tokens": ("cache_creation_input_tokens", "cacheCreationInputTokens"),
    }
    for blob in blobs:
        for canon, keys in aliases.items():
            for key in keys:
                if key in blob and isinstance(blob[key], (int, float)) and canon not in found:
                    found[canon] = blob[key]
    if "input_tokens" not in found:
        return None, found
    full = found["input_tokens"]
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if key in found:
            full += found[key]
    return int(full), found


def grok_usage(session_id: str) -> dict:
    proc = subprocess.run(["grok", "usage", session_id, "1"], capture_output=True, text=True)
    payload = {"exit_code": proc.returncode, "stderr": proc.stderr[-500:]}
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload["unparsed_stdout"] = proc.stdout[-2000:]
        payload["full_input_tokens"] = None
        return payload
    full, found = extract_full_input(obj)
    payload["full_input_tokens"] = full
    payload["buckets"] = found
    payload["raw"] = obj
    return payload


def copilot_first_input(session_id: str) -> dict:
    if not COPILOT_DB.exists():
        return {"full_input_tokens": None, "reason": f"missing {COPILOT_DB}"}
    try:
        con = sqlite3.connect(f"file:{COPILOT_DB}?mode=ro", uri=True, timeout=5)
        row = con.execute(
            "SELECT input_tokens, output_tokens, reasoning_tokens FROM assistant_usage_events "
            "WHERE session_id = ? AND parent_tool_call_id IS NULL ORDER BY rowid LIMIT 1",
            (session_id,),
        ).fetchone()
        con.close()
    except sqlite3.Error as exc:
        return {"full_input_tokens": None, "reason": str(exc)}
    if not row or row[0] is None:
        return {"full_input_tokens": None, "reason": "no usage row"}
    return {"full_input_tokens": int(row[0]), "output_tokens": row[1], "reasoning_tokens": row[2]}


def contract_ok(task: dict, arguments) -> tuple[bool, str]:
    if not isinstance(arguments, dict):
        return False, "arguments are not an object"
    contract = task.get("contract") or {}
    if "target_regex" in contract:
        targets = _strings(arguments.get("targets", arguments.get("target")))
        if not any(re.search(pat, target) for target in targets for pat in contract["target_regex"]):
            return False, f"targets {targets!r} matched none of {contract['target_regex']}"
    if "pattern_regex" in contract:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not any(re.search(pat, pattern) for pat in contract["pattern_regex"]):
            return False, f"pattern {pattern!r}"
    if contract.get("ast") is True and arguments.get("ast") not in (True, "true", "True", 1, "1"):
        return False, "ast is not true"
    if "direction" in contract:
        got = arguments.get("direction")
        if not (got is None and contract["direction"] == "callers"):
            if not isinstance(got, str) or got.strip().lower() != contract["direction"]:
                return False, f"direction {got!r}"
    if "path_regex" in contract:
        path = arguments.get("path")
        if not isinstance(path, str) or not any(re.search(pat, path) for pat in contract["path_regex"]):
            return False, f"path {path!r}"
    if "globs_regex" in contract:
        globs = _strings(arguments.get("globs"))
        if not any(re.search(pat, glob) for glob in globs for pat in contract["globs_regex"]):
            return False, f"globs {globs!r}"
    if contract.get("symbols") is True and arguments.get("symbols") not in (True, "true", "True", 1, "1"):
        return False, "symbols is not true"
    return True, ""


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_strings(item))
        return out
    return []


def replay_ok(mcp: Mcp, task: dict, tool: str, arguments: dict) -> tuple[bool, str]:
    try:
        _line, obj = mcp.rpc("tools/call", {"name": tool, "arguments": arguments})
    except Exception as exc:  # server died or returned non-json
        return False, str(exc)
    from definition_mcp import tool_text
    text, err = tool_text(obj)
    if err is not None:
        return False, f"server rejected: {err}"
    hits = [bool(re.search(pat, text or "")) for pat in task["response_graders"]]
    if not all(hits):
        return False, f"response graders {hits}"
    return True, ""


def graders_ok(patterns: list[str], text: str) -> bool:
    return all(re.search(pat, text or "") for pat in patterns)


def prepare_dirs(binary: str, harness: str, out: Path) -> dict[str, Path]:
    """Copies of the fixture outside this repo, so project-scoped MCP config
    cannot land in speedread's git root."""
    base = Path(tempfile.mkdtemp(prefix="speedread-gate-", dir="/tmp"))
    plain = base / "baseline"
    rooted = base / "with-mcp"
    shutil.copytree(FIXTURE, plain)
    shutil.copytree(FIXTURE, rooted)
    for path in (plain, rooted):
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, check=True, capture_output=True, text=True)
        if Path(top.stdout.strip()).resolve() != path.resolve():
            raise SystemExit(f"workdir {path} is inside another git root ({top.stdout.strip()})")
    if harness == "grok":
        add = ["grok", "mcp", "add", "--scope", "project", "speedread", "--",
               str(Path(binary).resolve()), "--root", str(rooted), *FLAGS, "mcp"]
        subprocess.run(add, cwd=rooted, check=True)
        (out / "grok-mcp-add.txt").write_text(" ".join(add) + "\n")
    else:
        cfg = {
            "mcpServers": {
                "speedread": {
                    "type": "local",
                    "command": str(Path(binary).resolve()),
                    "args": ["--root", str(rooted), *FLAGS, "mcp"],
                    "tools": ["*"],
                }
            }
        }
        path = out / "speedread-mcp.json"
        path.write_text(json.dumps(cfg, indent=2) + "\n")
        (out / "copilot-mcp-path.txt").write_text(str(path) + "\n")
    (out / "workdirs.json").write_text(json.dumps({"baseline": str(plain), "with_mcp": str(rooted)}, indent=2) + "\n")
    return {"baseline": plain, "available": rooted, "exclusive": rooted}


def grok_argv(prompt, model, session_id, cwd, sandbox, exclusive, max_turns) -> list[str]:
    cmd = [
        "grok", "--no-auto-update", "-p", prompt,
        "--output-format", "streaming-json",
        "--always-approve", "-m", model, "-s", session_id,
        "--cwd", str(cwd),
        "--no-subagents", "--disable-web-search", "--no-memory",
        "--sandbox", sandbox,
        "--max-turns", str(max_turns),
    ]
    if exclusive:
        cmd += ["--disallowed-tools", ",".join(GROK_FILE_TOOLS)]
    return cmd


def copilot_argv(prompt, model, mcp_config: str | None, exclusive: bool, max_credits: int) -> list[str]:
    cmd = [
        "copilot", "-p", prompt, "--model", model, "--max-ai-credits", str(max_credits),
        "--allow-all-tools", "--no-custom-instructions", "--disable-builtin-mcps",
        "--no-ask-user", "--no-auto-update", "--output-format", "json",
    ]
    if mcp_config:
        cmd += ["--additional-mcp-config", f"@{mcp_config}", "--allow-all-mcp-server-instructions"]
    excluded = ["task", "web_fetch", "web_search"]
    if exclusive:
        excluded = ["view", "grep", "glob", "rg", "bash", *excluded]
    cmd += ["--excluded-tools", *excluded]
    return cmd


def run_proc(cmd: list[str], cwd: Path, timeout: int) -> tuple[str, str, int]:
    env = os.environ.copy()
    env["GROK_DISABLE_AUTOUPDATER"] = "1"
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return out, (err or "") + "\ntimeout", -1


def iqr(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) < 4:
        return ys[-1] - ys[0]

    def q(p: float) -> float:
        k = (len(ys) - 1) * p
        f = int(k)
        c = min(f + 1, len(ys) - 1)
        return ys[f] + (ys[c] - ys[f]) * (k - f)

    return q(0.75) - q(0.25)


def median(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2:
        return float(ys[mid])
    return (ys[mid - 1] + ys[mid]) / 2


def rate_by_tool(recs: list[dict], key: str) -> dict:
    out = {}
    for tool in TOOLS:
        mine = [r for r in recs if r.get("tool") == tool and r.get("scored")]
        excluded = [r for r in recs if r.get("tool") == tool and not r.get("scored")]
        hits = sum(1 for r in mine if r.get(key))
        out[tool] = {
            "hits": hits,
            "scored": len(mine),
            "excluded_surface": len(excluded),
            "rate": (hits / len(mine)) if mine else None,
        }
    scored = [r for r in recs if r.get("scored")]
    hits = sum(1 for r in scored if r.get(key))
    out["pooled"] = {
        "hits": hits,
        "scored": len(scored),
        "rate": (hits / len(scored)) if scored else None,
    }
    return out


def plan_text(args) -> str:
    per_tool = TASKS_PER_TOOL * args.trials
    pooled = per_tool * len(TOOLS)
    probes = args.probe_sessions * len(PROBE_CONDITIONS)
    return "\n".join([
        "Agent gate plan (pre-registered; not a result)",
        f"  harness: {args.harness}",
        f"  model: {args.model}",
        f"  exclusive task sessions: {pooled} ({TASKS_PER_TOOL} tasks × {args.trials} trials × {len(TOOLS)} tools)",
        f"  per tool: {per_tool} trials",
        f"  first-call probe sessions: {probes} ({args.probe_sessions} × {', '.join(PROBE_CONDITIONS)}, interleaved)",
        f"  total sessions: {pooled + probes}",
        "  selection / argument-validity / task-success are scored on exclusive only",
        "  first-call delta primary pair is available − baseline on the probe prompt",
        "  this is not the unprompted-adoption experiment",
        "",
    ])


def example_commands(args, workdirs: dict[str, Path] | None) -> str:
    cwd = (workdirs or {}).get("available", Path("/tmp/speedread-gate-EXAMPLE/with-mcp"))
    plain = (workdirs or {}).get("baseline", Path("/tmp/speedread-gate-EXAMPLE/baseline"))
    lines = ["Example argv (session id is fresh per run):", ""]
    if args.harness == "grok":
        lines.append("MCP (once, cwd is its own git root outside this repo):")
        lines.append("  " + " ".join([
            "grok", "mcp", "add", "--scope", "project", "speedread", "--",
            str(Path(args.binary).resolve()), "--root", str(cwd), *FLAGS, "mcp",
        ]))
        lines.append("")
        lines.append("Probe, available:")
        lines.append("  " + " ".join(grok_argv(PROBE_PROMPT, args.model, "<uuid>", cwd, args.sandbox, False, 1)))
        lines.append("")
        lines.append("Probe, baseline (no MCP server in that workdir):")
        lines.append("  " + " ".join(grok_argv(PROBE_PROMPT, args.model, "<uuid>", plain, args.sandbox, False, 1)))
        lines.append("")
        lines.append("Task, exclusive:")
        lines.append("  " + " ".join(grok_argv("<task prompt>", args.model, "<uuid>", cwd, args.sandbox, True, 12)))
        lines.append("")
        lines.append("Usage, turn 1:")
        lines.append("  grok usage <session-id> 1")
    else:
        lines.append("Probe, baseline:")
        lines.append("  " + " ".join(copilot_argv(PROBE_PROMPT, args.model, None, False, args.max_credits)))
        lines.append("")
        lines.append("Task, exclusive:")
        lines.append("  " + " ".join(copilot_argv("<task prompt>", args.model, "<mcp.json>", True, args.max_credits)))
    lines.append("")
    return "\n".join(lines)


def cmd_agent(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    text = plan_text(args) + "\n" + example_commands(args, None)
    (out / "PLAN.txt").write_text(text)
    print(text)
    if args.print_commands and not args.yes:
        return 0
    if not args.yes:
        print("Not running. The pre-registered count starts only with --yes.")
        return 2
    tasks = load_tasks()
    workdirs = prepare_dirs(args.binary, args.harness, out)
    print(example_commands(args, workdirs))
    version = subprocess.run([args.harness, "version"], capture_output=True, text=True)
    meta = {
        "harness": args.harness,
        "harness_version": (version.stdout or version.stderr).strip(),
        "model": args.model,
        "trials_per_task": args.trials,
        "probe_sessions_per_condition": args.probe_sessions,
        "status": "running",
        "host": host_info(args.binary),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    mcp_config = str(out / "speedread-mcp.json") if args.harness == "copilot" else None
    transcripts = out / "transcripts"
    transcripts.mkdir(exist_ok=True)
    probe_path = out / "probes.jsonl"
    trial_path = out / "trials.jsonl"

    def launch(prompt: str, condition: str, kind: str, max_turns: int, timeout: int) -> dict:
        cwd = workdirs[condition]
        session = str(uuid.uuid4())
        exclusive = condition == "exclusive"
        if args.harness == "grok":
            cmd = grok_argv(prompt, args.model, session, cwd, args.sandbox, exclusive, max_turns)
        else:
            cfg = mcp_config if condition != "baseline" else None
            cmd = copilot_argv(prompt, args.model, cfg, exclusive, args.max_credits)
            session = None
        t0 = time.time()
        stdout, stderr, code = run_proc(cmd, cwd, timeout)
        events = parse_events(stdout)
        sid = session or session_id_of(events)
        usage = {}
        if sid and args.harness == "grok":
            usage = grok_usage(sid)
        elif sid and args.harness == "copilot":
            usage = copilot_first_input(sid)
        return {
            "kind": kind,
            "condition": condition,
            "exit_code": code,
            "session_id": sid,
            "wall_s": round(time.time() - t0, 2),
            "full_input_tokens": usage.get("full_input_tokens"),
            "usage": {k: v for k, v in usage.items() if k != "raw"},
            "usage_raw_kept": "raw" in usage,
            "events": events,
            "stdout": stdout,
            "stderr_tail": stderr[-400:],
            "cmd": cmd,
        }

    with probe_path.open("w") as fh:
        for repeat in range(args.probe_sessions):
            for condition in PROBE_CONDITIONS:
                got = launch(PROBE_PROMPT, condition, "probe", 1, 90)
                rec = {k: v for k, v in got.items() if k not in ("events", "stdout", "cmd")}
                rec["repeat"] = repeat
                rec["surface_ok"] = surface_ok(got["events"], condition == "exclusive")
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                (transcripts / f"probe-{repeat:04d}-{condition}.jsonl").write_text(got["stdout"])
                print(f"probe {repeat} {condition} in={rec['full_input_tokens']} exit={rec['exit_code']}", flush=True)

    replay = Mcp(args.binary, ["--root", str(FIXTURE.resolve()), *FLAGS, "mcp"], cwd="/")
    replay.handshake()
    order = []
    rng = random.Random(7)
    for trial in range(args.trials):
        batch = list(tasks)
        rng.shuffle(batch)
        for task in batch:
            order.append((trial, task))
    try:
        with trial_path.open("w") as fh:
            for n, (trial, task) in enumerate(order, 1):
                got = launch(task["prompt"], "exclusive", "task", 12, args.timeout)
                calls = speedread_calls(got["events"])
                first = calls[0] if calls else None
                selected = bool(first) and first["tool"] == task["tool"]
                arg_ok = False
                arg_note = "no speedread call"
                if first and selected:
                    ok, note = contract_ok(task, first["arguments"])
                    if not ok:
                        arg_note = note
                    elif first.get("arguments_lost"):
                        arg_note = "tool name found but arguments were not in the event"
                    else:
                        arg_ok, arg_note = replay_ok(replay, task, first["tool"], first["arguments"])
                elif first:
                    arg_note = f"first speedread tool was {first['tool']}"
                answer = assistant_text(got["events"])
                success = graders_ok(task["answer_graders"], answer)
                surface = surface_ok(got["events"], True)
                scored = surface is not False
                rec = {
                    "task": task["id"],
                    "tool": task["tool"],
                    "trial": trial,
                    "condition": "exclusive",
                    "exit_code": got["exit_code"],
                    "session_id": got["session_id"],
                    "wall_s": got["wall_s"],
                    "full_input_tokens": got["full_input_tokens"],
                    "surface_ok": surface,
                    "scored": scored,
                    "tool_selection": selected if scored else None,
                    "argument_validity": arg_ok if scored else None,
                    "task_success": success if scored else None,
                    "argument_note": arg_note,
                    "speedread_tools": [c["tool"] for c in calls],
                    "stderr_tail": got["stderr_tail"],
                }
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                (transcripts / f"{task['id']}__exclusive__{trial}.jsonl").write_text(got["stdout"])
                print(
                    f"[{n}/{len(order)}] {task['id']} sel={int(bool(selected))} arg={int(bool(arg_ok))} "
                    f"ok={int(bool(success))} surface={surface}",
                    flush=True,
                )
    finally:
        replay.close()

    probes = [json.loads(line) for line in probe_path.read_text().splitlines() if line.strip()]
    trials = [json.loads(line) for line in trial_path.read_text().splitlines() if line.strip()]
    summary = summarize(probes, trials, meta)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "summary.md").write_text(summary_md(summary))
    print(summary_md(summary))
    meta["status"] = "complete"
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return 0


def paired(probes: list[dict], left: str, right: str) -> list[float]:
    by = {}
    for row in probes:
        by.setdefault(row["repeat"], {})[row["condition"]] = row.get("full_input_tokens")
    diffs = []
    for repeat, cols in sorted(by.items()):
        a, b = cols.get(left), cols.get(right)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            diffs.append(a - b)
    return diffs


def summarize(probes: list[dict], trials: list[dict], meta: dict) -> dict:
    diff = paired(probes, "available", "baseline")
    diff_ex = paired(probes, "exclusive", "baseline")
    med = median(diff)
    spread = iqr(diff)
    inconclusive = spread is not None and med is not None and spread > abs(med)
    unverified = sum(1 for r in trials if r.get("surface_ok") is None)
    excluded = sum(1 for r in trials if r.get("scored") is False)
    return {
        "status": "baseline arm; not a Phase 2 comparison",
        "meta": {k: v for k, v in meta.items() if k != "host"},
        "host": meta.get("host"),
        "tool_selection": rate_by_tool(trials, "tool_selection"),
        "argument_validity": rate_by_tool(trials, "argument_validity"),
        "task_success": rate_by_tool(trials, "task_success"),
        "surface_unverified_trials": unverified,
        "surface_excluded_trials": excluded,
        "first_call": {
            "primary": "available − baseline, full input tokens of probe turn 1",
            "pairs": len(diff),
            "median": med,
            "iqr": spread,
            "min": min(diff) if diff else None,
            "max": max(diff) if diff else None,
            "verdict": "inconclusive" if inconclusive else ("measured" if diff else "unparsed"),
            "exclusive_minus_baseline_median": median(diff_ex),
            "exclusive_minus_baseline_iqr": iqr(diff_ex),
            "missing_probe_tokens": sum(1 for r in probes if r.get("full_input_tokens") is None),
        },
    }


def summary_md(summary: dict) -> str:
    fc = summary["first_call"]
    lines = [
        "# Agent gate",
        "",
        summary["status"],
        "",
        f"First-call {fc['primary']}: median {fc['median']}, IQR {fc['iqr']}, "
        f"min–max {fc['min']}–{fc['max']}, pairs {fc['pairs']}, verdict {fc['verdict']}.",
        "",
        "| Tool | selection | argument validity | task success |",
        "|---|---:|---:|---:|",
    ]
    for tool in list(TOOLS) + ["pooled"]:
        sel = summary["tool_selection"][tool]
        arg = summary["argument_validity"][tool]
        suc = summary["task_success"][tool]

        def cell(block):
            if block["rate"] is None:
                return "—"
            return f"{block['hits']}/{block['scored']} ({block['rate']:.3f})"

        lines.append(f"| {tool} | {cell(sel)} | {cell(arg)} | {cell(suc)} |")
    lines += [
        "",
        f"Surface unverified: {summary['surface_unverified_trials']}. "
        f"Excluded because file tools were still offered: {summary['surface_excluded_trials']}.",
        "",
    ]
    return "\n".join(lines)
