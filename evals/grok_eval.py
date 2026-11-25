#!/usr/bin/env python3
"""Description A/B trials on Grok CLI (primary runner for this harness).

    python3 evals/grok_eval.py --bench <dir> --tasks evals/agent_tasks.json \
        --conditions baseline,dropin --trials 5 --models grok-build,grok-4.7 \
        --description current --out <run-dir>

Headless invocation, per the Grok CLI docs:

    grok --no-auto-update -p "<prompt>" -m <model> --output-format streaming-json \
        --always-approve --cwd <repo> -s <uuid> \
        --disallowed-tools ... --deny ...

``baseline`` does not configure the speedread MCP server and denies
``MCPTool(speedread__*)``. ``dropin`` and ``available`` add the server with
no guidance sentence (unprompted). ``exclusive`` removes the built-in readers
with ``--disallowed-tools`` and ``--deny``. ``preferred`` is a control that
adds one guidance sentence; it is not an adoption claim.

Token and cost totals come from ``grok usage <session-id>`` in the trial's
``GROK_HOME``. The streaming ``end`` event is kept as a labeled fallback.
Adoption is computed from the NDJSON tool events by evals/adoption.py.

A trial that errors, times out, or is skipped is written to trials.jsonl.
It is not dropped. Adoption's denominator is every scheduled trial that
produced a transcript (including crashes). Skips before launch are listed
and reported separately.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import adoption  # noqa: E402
import desc_variant  # noqa: E402
from agent_eval import grade as grade_regex  # noqa: E402
from grok_transcript import parse_streaming_json  # noqa: E402

UNPROMPTED = {"dropin", "available"}
# Built-in tool ids from the Grok headless docs. Shell is run_terminal_cmd.
# Exclusive removes readers and leaves the shell and search_replace.
_ISOLATION = "Agent,web_search,web_fetch"
_EXCLUSIVE = "read_file,grep,list_dir,glob,Agent,web_search,web_fetch"


def condition_setup(cond):
    """Return (launch_speedread, extra argv)."""
    if cond == "baseline":
        return False, ["--disallowed-tools", _ISOLATION, "--deny", "MCPTool(speedread__*)"]
    if cond in UNPROMPTED or cond == "preferred":
        return True, ["--disallowed-tools", _ISOLATION]
    if cond in ("exclusive", "speedread"):
        return True, [
            "--disallowed-tools", _EXCLUSIVE,
            "--deny", "Read", "--deny", "Grep",
        ]
    raise ValueError(f"unknown condition {cond}")


def prepare_grok_home(real_home, speedread_bin, variant, rss_log, use_mcp):
    home = Path(tempfile.mkdtemp(prefix="speedread-grok-"))
    if real_home.is_dir():
        for name in ("auth.json", "mcp_credentials.json"):
            src = real_home / name
            if src.exists():
                (home / name).symlink_to(src)
    if use_mcp:
        (home / "config.toml").write_text(desc_variant.grok_config(speedread_bin, variant, rss_log=rss_log))
    else:
        (home / "config.toml").write_text(
            "# baseline trial: no MCP servers, so speedread is not on the tool list\n"
        )
    return home


def grok_usage(session_id, env):
    try:
        proc = subprocess.run(
            ["grok", "usage", session_id], capture_output=True, text=True, env=env, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as ex:
        return {}, f"grok usage failed: {ex}"
    if proc.returncode != 0:
        return {}, (proc.stderr or proc.stdout or "grok usage failed")[-400:]
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}, "grok usage did not return JSON"
    sess = payload.get("session") or {}
    ticks = sess.get("costUsdTicks")
    return {
        "input_tokens": sess.get("inputTokens"),
        "output_tokens": sess.get("outputTokens"),
        "cache_read_tokens": sess.get("cachedReadTokens"),
        "cache_creation_tokens": sess.get("cacheCreationTokens"),
        "reasoning_tokens": sess.get("reasoningTokens"),
        "total_tokens": sess.get("totalTokens"),
        "model_calls": sess.get("modelCalls"),
        "cost_usd_ticks": ticks,
        "cost_usd": (ticks / 1e10) if isinstance(ticks, int) else None,
        "cost_is_partial": bool(sess.get("costIsPartial")),
        "usage_is_incomplete": bool(sess.get("usageIsIncomplete")),
        "primary_model": sess.get("primaryModelId"),
        "usage_source": "grok usage",
    }, ""


def stream_usage(end):
    usage = (end or {}).get("usage") or {}
    if not usage:
        return {}
    ticks = end.get("total_cost_usd_ticks")
    cost = end.get("total_cost_usd")
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "model_calls": end.get("num_turns"),
        "cost_usd_ticks": ticks,
        "cost_usd": cost if isinstance(cost, (int, float)) else (ticks / 1e10 if isinstance(ticks, int) else None),
        "usage_source": "streaming-json end event",
    }


def read_rss(path):
    if not path or not Path(path).is_file():
        return None
    for line in Path(path).read_text().splitlines():
        if line.startswith("peak_rss_kib="):
            raw = line.split("=", 1)[1].strip()
            return int(raw) if raw.isdigit() else None
    return None


def run_trial(job, args, out_dir, bench):
    task, cond, trial, model = job
    use_mcp, extra = condition_setup(cond)
    repo = bench / task["repo"]
    rec = {
        "task": task["id"], "condition": cond, "trial": trial, "model": model,
        "description": args.description, "runner": "grok", "status": "error",
        "pass": False, "score": 0.0, "used_speedread": False,
        "category": task.get("category") or task.get("difficulty"),
    }
    if not repo.is_dir():
        rec.update(status="skipped", error=f"repo not checked out: {repo}")
        return rec
    project_cfg = repo / ".grok" / "config.toml"
    if project_cfg.exists():
        rec.update(status="skipped", error=f"{project_cfg} would override the trial MCP config")
        return rec
    violations = adoption.prompt_violations(task["prompt"]) if cond in UNPROMPTED else []
    if violations:
        rec.update(status="skipped", error=f"unprompted prompt mentions {violations}")
        return rec

    prompt = task["prompt"]
    if cond == "preferred":
        from coding_eval import PREFER
        prompt = prompt + PREFER
    session_id = str(uuid.uuid4())
    rss_path = out_dir / "rss" / f"{task['id']}__{cond}__{trial}__{model}.txt"
    rss_path.parent.mkdir(parents=True, exist_ok=True)
    real_home = Path(os.environ.get("GROK_HOME", Path.home() / ".grok"))
    home = prepare_grok_home(real_home, args.speedread, args.description, rss_path if use_mcp else None, use_mcp)
    env = {**os.environ, "GROK_HOME": str(home)}
    cmd = [
        "grok", "--no-auto-update", "-p", prompt, "-m", model,
        "--output-format", "streaming-json", "--always-approve",
        "--cwd", str(repo), "-s", session_id, "--max-turns", str(args.max_turns),
        *extra,
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env, timeout=args.timeout)
        out, err, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as ex:
        out = ex.stdout if isinstance(ex.stdout, str) else (ex.stdout or b"").decode(errors="replace")
        err, code = "timeout", -1
    except OSError as ex:
        out, err, code = "", str(ex), -1
    wall = time.time() - t0
    transcript = out_dir / "transcripts" / f"{task['id']}__{cond}__{trial}__{model}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    scrubbed = out.replace(str(bench), "<bench>").replace(str(Path.home()), "~")
    transcript.write_text(scrubbed)
    parsed = parse_streaming_json(out)
    calls = parsed["calls"]
    adopt = adoption.summarize_calls(calls)
    answer = parsed["answer"]
    if task.get("graders"):
        score, passed, hits = grade_regex(task, answer)
    else:
        score, passed, hits = None, None, None
    usage, usage_err = grok_usage(session_id, env)
    source = "grok usage"
    if not usage:
        usage = stream_usage(parsed["end"])
        source = usage.get("usage_source", "missing")
    api_ms = None
    end = parsed["end"] or {}
    for key in ("duration_api_ms", "durationApiMs"):
        if isinstance(end.get(key), (int, float)):
            api_ms = end[key]
    status = "ok" if code == 0 and not parsed["errors"] else "error"
    if code == -1 and err == "timeout":
        status = "timeout"
    rec.update(
        status=status, exit_code=code, wall_s=round(wall, 2), api_ms=api_ms,
        session_id=session_id, answer=(answer or "").strip()[:4000],
        score=score, **({"pass": passed} if passed is not None else {"pass": False}),
        grader_hits=hits, stop_reason=parsed.get("stop_reason"),
        stderr_tail=(err or "")[-400], usage_error=usage_err, usage_source=source,
        bad_lines=parsed["bad_lines"], errors=parsed["errors"][:3],
        peak_rss_kib=read_rss(rss_path) if use_mcp else None,
        **adopt, **{k: usage.get(k) for k in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
            "reasoning_tokens", "total_tokens", "cost_usd_ticks", "cost_usd",
            "cost_is_partial", "usage_is_incomplete", "primary_model")},
    )
    if usage.get("model_calls"):
        rec["model_calls"] = usage["model_calls"]
    elif parsed.get("model_calls"):
        rec["model_calls"] = parsed["model_calls"]
    shutil.rmtree(home, ignore_errors=True)
    return rec


def run_coding_trial(job, args, out_dir, bench, root):
    from coding_eval import grade as grade_tests
    from coding_eval import make_workspace
    task, cond, trial, model = job
    name = f"{task['id']}__{cond}__{trial}__{model}"
    try:
        ws = make_workspace(task, bench, args.venvs, root, name)
    except Exception as ex:
        return {
            "task": task["id"], "condition": cond, "trial": trial, "model": model,
            "runner": "grok", "status": "error", "pass": False, "used_speedread": False,
            "description": args.description, "error": repr(ex),
        }
    # Point the trial at the workspace by giving it a one-repo bench.
    view = Path(tempfile.mkdtemp(prefix="speedread-view-"))
    link = view / task["repo"]
    link.symlink_to(ws)
    try:
        rec = run_trial(job, args, out_dir, view)
    finally:
        shutil.rmtree(view, ignore_errors=True)
    try:
        g = grade_tests(task, ws)
    except Exception as ex:
        g = {"pass": False, "tests_ok": False, "touched_tests": [], "test_s": None, "diff": "", "error": repr(ex)}
    rec["pass"] = bool(g.get("pass"))
    rec["tests_ok"] = g.get("tests_ok")
    rec["touched_tests"] = g.get("touched_tests")
    rec["test_s"] = g.get("test_s")
    rec["score"] = 1.0 if rec["pass"] else 0.0
    (out_dir / "diffs").mkdir(exist_ok=True)
    (out_dir / "diffs" / f"{name}.diff").write_text(g.get("diff") or "")
    if not args.keep:
        shutil.rmtree(ws, ignore_errors=True)
    return rec


def already_done(path):
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("status") in {"ok", "error", "timeout"}:
            done.add((r["task"], r["condition"], r["trial"], r.get("model")))
    return done


def execute(args, tasks, jobs, bench, coding=False):
    import concurrent.futures as cf
    out_dir = Path(args.out)
    (out_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    done = already_done(out_dir / "trials.jsonl")
    jobs = [j for j in jobs if (j[0]["id"], j[1], j[2], j[3]) not in done]
    root = Path(tempfile.mkdtemp(prefix="speedread-coding-")) if coding else None
    print(f"{len(jobs)} trials to run ({len(done)} already recorded) → {out_dir}", flush=True)
    lock = threading.Lock()
    mode = "a" if (out_dir / "trials.jsonl").exists() else "w"
    with open(out_dir / "trials.jsonl", mode) as fh, cf.ThreadPoolExecutor(args.workers) as pool:
        def go(job):
            if coding:
                return run_coding_trial(job, args, out_dir, bench, root)
            return run_trial(job, args, out_dir, bench)
        futs = [pool.submit(go, j) for j in jobs]
        for n, fut in enumerate(cf.as_completed(futs), 1):
            try:
                rec = fut.result()
            except Exception as ex:
                rec = {"status": "error", "pass": False, "used_speedread": False, "error": repr(ex),
                       "task": "?", "condition": "?", "trial": -1, "model": "?"}
            with lock:
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
            print(
                f"[{n:>3}/{len(jobs)}] {rec.get('task','?'):<28} {rec.get('condition','?'):<9} "
                f"{rec.get('model','?'):<12} {rec.get('status')} pass={int(bool(rec.get('pass')))} "
                f"sr={int(bool(rec.get('used_speedread')))} t={rec.get('wall_s')}",
                flush=True,
            )
    if root:
        shutil.rmtree(root, ignore_errors=True)
    return out_dir


def plan_jobs(tasks, conditions, trials, models, seed):
    jobs = [(t, c, i, m) for m in models for t in tasks for c in conditions for i in range(trials)]
    random.Random(seed).shuffle(jobs)
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", type=Path, required=True)
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--conditions", default="baseline,dropin")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--models", default="grok-build")
    ap.add_argument("--description", default="current")
    ap.add_argument("--speedread", default="speedread")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--only", default=None)
    ap.add_argument("--coding", action="store_true")
    ap.add_argument("--venvs", type=Path, default=None)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    if args.description != "current":
        desc_variant.load_variant(args.description)
    suite = json.loads(args.tasks.read_text())
    tasks = [t for t in suite["tasks"] if not args.only or t["id"] in args.only.split(",")]
    conds = [c for c in args.conditions.split(",") if c]
    for t in tasks:
        for c in conds:
            if c in UNPROMPTED:
                bad = adoption.prompt_violations(t["prompt"])
                if bad:
                    raise SystemExit(f"{t['id']} ({c}) prompt mentions {bad}")
    models = [m for m in args.models.split(",") if m]
    jobs = plan_jobs(tasks, conds, args.trials, models, args.seed)
    execute(args, tasks, jobs, args.bench.resolve(), coding=args.coding)
    return 0


if __name__ == "__main__":
    sys.exit(main())
