#!/usr/bin/env python3
"""Description A/B trials on Grok CLI (primary runner for this harness).

    python3 evals/grok_eval.py --bench <dir> --tasks evals/agent_tasks.json \
        --conditions baseline,dropin --trials 5 --models grok-4.7,grok-4.7-build-fast \
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
``--trust`` is set on every invocation: a fresh git checkout does not load
project MCP servers until the folder is trusted.

The process named in the trial MCP config is ``evals/mcp_allow.py``, which
forwards to ``speedread mcp`` and drops every tool except map, search, and
read from ``tools/list``. ``--deny MCPTool(speedread__trace)`` does not do
that: on grok 1.0.92 it only blocks a later call, and the name stays in
``available_commands`` and ``search_tool``.

Exclusive denies ``read_file``, ``grep``, ``list_dir``,
``run_terminal_command``, ``search_replace``, and ``write``. Grok 1.0.92
ignores ``skip_read_before_edit`` and will not start a session that still
has ``search_replace`` and no Read tool. Denying the edit tools is the
config that starts. If any session still fails to initialize, the run stops.

One live check, not the baseline:

    python3 evals/grok_eval.py --live-check --speedread "$PWD/target/fast/speedread"

A trial that errors, times out, or is skipped is written to trials.jsonl.
It is not dropped. Adoption's denominator is every scheduled trial that
produced a transcript (including crashes). Skips before launch are listed
and reported separately.
"""

import argparse
import json
import os
import random
import re
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
import endpoint  # noqa: E402
import mcp_allow  # noqa: E402
from agent_eval import grade as grade_regex  # noqa: E402
from grok_transcript import normalize_usage, parse_streaming_json  # noqa: E402

UNPROMPTED = {"dropin", "available"}
_ISOLATION = "Agent,web_search,web_fetch"
# Same exclusive set as evals/definition_agent.py on cursor/definition-token-baseline-552c.
# search_replace and write are denied so Grok does not demand a Read tool.
# The shell id on grok 1.0.92 is run_terminal_command.
_EXCLUSIVE_DENY = (
    "read_file",
    "grep",
    "list_dir",
    "run_terminal_command",
    "search_replace",
    "write",
)
_EXCLUSIVE = ",".join(_EXCLUSIVE_DENY + ("Agent", "web_search", "web_fetch"))
# grok usage reports the default agent model under this id, not the -m flag.
USAGE_MODEL_ALIAS = {"grok-4.7": "grok-4.7-build"}
_INIT_MARKERS = (
    "couldn't create session",
    "could not create session",
    "couldn't start session",
    "could not start session",
    "failed to initialize",
    "failed to start session",
    "tool requirement",
    "requires a read",
    "invalid tool configuration",
    "invalid toolset",
)
_CATALOG = {"map", "search", "read"}
_PROBE_PROMPT = "Reply with the single word pong. Do not call any tool."


def condition_setup(cond):
    """Return (launch_speedread, extra argv).

    ``--deny MCPTool(speedread__trace)`` does not remove trace from the
    catalog. grok 1.0.92 still lists it in ``available_commands`` and returns
    it from ``search_tool``; the flag only rejects the call, which skews
    unprompted choice. The catalog the agent sees is whatever
    ``evals/mcp_allow.py`` left in ``tools/list`` (map, search, read). The
    deny below is a call block in case a session is started without that
    filter. It is not how the tool is hidden.
    """
    block_call = ["--deny", "MCPTool(speedread__trace)", "--deny", "MCPTool(speedread__context)"]
    if cond == "baseline":
        return False, ["--disallowed-tools", _ISOLATION, "--deny", "MCPTool(speedread__*)"]
    if cond in UNPROMPTED or cond == "preferred":
        return True, ["--disallowed-tools", _ISOLATION, *block_call]
    if cond in ("exclusive", "speedread"):
        return True, ["--disallowed-tools", _EXCLUSIVE, *block_call]
    raise ValueError(f"unknown condition {cond}")


def grok_argv(prompt, model, cwd, session_id, cond, max_turns):
    """Headless command. ``--trust`` is not optional on a fresh checkout."""
    _, extra = condition_setup(cond)
    return [
        "grok", "--no-auto-update", "--trust", "-p", prompt, "-m", model,
        "--output-format", "streaming-json", "--always-approve",
        "--cwd", str(cwd), "-s", session_id, "--max-turns", str(max_turns),
        *extra,
    ]


def tail(text, n=400):
    """Last ``n`` characters. A bare ``[-n]`` is an index and raises on short strings."""
    return (text or "")[-n:]


def initialization_failure(rec):
    """True when Grok never started the session (toolset/config), not when a trial merely erred."""
    blob = " ".join([
        rec.get("stderr_tail") or "",
        rec.get("error") or "",
        " ".join(rec.get("errors") or []),
        rec.get("stop_reason") or "",
    ]).lower()
    return any(marker in blob for marker in _INIT_MARKERS)


def prepare_grok_home(real_home, speedread_bin, variant, rss_log, use_mcp, endpoint_cfg=None):
    home = Path(tempfile.mkdtemp(prefix="speedread-grok-"))
    if real_home.is_dir():
        for name in ("auth.json", "mcp_credentials.json"):
            src = real_home / name
            if src.exists():
                (home / name).symlink_to(src)
    if use_mcp:
        text = desc_variant.grok_config(
            speedread_bin, variant, rss_log=rss_log, variant_out=home / "description.json",
        )
    else:
        text = "# baseline trial: no MCP servers, so speedread is not on the tool list\n"
    if endpoint_cfg:
        text += endpoint.model_sections(endpoint_cfg)
    (home / "config.toml").write_text(text)
    return home


def grok_usage(session_id, env):
    try:
        proc = subprocess.run(
            ["grok", "usage", session_id], capture_output=True, text=True, env=env, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as ex:
        return {}, f"grok usage failed: {ex}"
    if proc.returncode != 0:
        return {}, tail(proc.stderr or proc.stdout or "grok usage failed")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}, "grok usage did not return JSON"
    return payload.get("session") or {}, ""


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
    use_mcp, _extra = condition_setup(cond)
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
    home = prepare_grok_home(
        real_home, args.speedread, args.description, rss_path if use_mcp else None, use_mcp,
        endpoint_cfg=getattr(args, "endpoint", None),
    )
    env = {**os.environ, "GROK_HOME": str(home)}
    cmd = grok_argv(prompt, model, repo, session_id, cond, args.max_turns)
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
    session, usage_err = grok_usage(session_id, env)
    usage = normalize_usage(session=session or None, end=None if session else parsed["end"])
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
        stderr_tail=tail(err), usage_error=usage_err,
        bad_lines=parsed["bad_lines"], errors=parsed["errors"][:3],
        peak_rss_kib=read_rss(rss_path) if use_mcp else None,
        **adopt, **{k: usage.get(k) for k in (
            "input_tokens", "input_includes_cache", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "reasoning_tokens", "total_tokens", "cost_usd_ticks",
            "cost_usd", "cost_is_partial", "usage_is_incomplete", "usage_model", "usage_source")},
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
            if initialization_failure(rec):
                pool.shutdown(wait=False, cancel_futures=True)
                raise SystemExit(
                    "session failed to initialize "
                    f"({rec.get('task')} {rec.get('condition')} {rec.get('model')}): "
                    f"{rec.get('stderr_tail') or rec.get('error')}"
                )
            if rec.get("condition") == "baseline" and (
                rec.get("used_speedread") or rec.get("other_speedread_calls")
            ):
                pool.shutdown(wait=False, cancel_futures=True)
                raise SystemExit(
                    "baseline trial called a speedread tool; the deny leaked "
                    f"({rec.get('task')} {rec.get('model')}): "
                    f"{rec.get('tools')}"
                )
            print(
                f"[{n:>3}/{len(jobs)}] {rec.get('task','?'):<28} {rec.get('condition','?'):<9} "
                f"{rec.get('model','?'):<12} {rec.get('status')} pass={int(bool(rec.get('pass')))} "
                f"sr={int(bool(rec.get('used_speedread')))} t={rec.get('wall_s')}",
                flush=True,
            )
    if root:
        shutil.rmtree(root, ignore_errors=True)
    return out_dir


def speedread_names(text):
    """Speedread catalog names from a grok streaming-json session.

    Walks ``available_commands`` / ``availableCommands`` and also any
    ``speedread__*`` token in the raw stream, which is where grok 1.0.92
    listed ``speedread__trace`` during the smoke.
    """
    names = []

    def walk(obj):
        if isinstance(obj, dict):
            for key in ("available_commands", "availableCommands"):
                value = obj.get(key)
                if not isinstance(value, list):
                    continue
                for item in value:
                    if isinstance(item, str):
                        names.append(item)
                    elif isinstance(item, dict):
                        for field in ("name", "tool"):
                            if isinstance(item.get(field), str):
                                names.append(item[field])
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            walk(json.loads(line))
        except json.JSONDecodeError:
            continue
    names.extend(re.findall(r"speedread__[A-Za-z0-9_]+", text or ""))
    return names


def _leaf(name):
    if "speedread__" in name:
        return name.split("speedread__", 1)[1]
    if name.startswith("speedread-"):
        return name.split("-", 1)[1]
    return None


def catalog_problem(text):
    """None when the session stream offered exactly map, search, and read."""
    leaves = {leaf for leaf in (_leaf(name) for name in speedread_names(text)) if leaf}
    if not leaves:
        return "no speedread tool in the session stream; cannot prove the catalog"
    extra = sorted(leaves - _CATALOG)
    missing = sorted(_CATALOG - leaves)
    if extra or missing:
        return f"catalog leaves {sorted(leaves)}; wanted exactly map, search, read"
    return None


def make_probe_repo():
    repo = Path(tempfile.mkdtemp(prefix="speedread-probe-"))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("probe\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=probe@example.com", "-c", "user.name=probe", "commit", "-q", "-m", "probe"],
        cwd=repo, check=True,
    )
    return repo


def run_probe(cond, speedread, model, repo):
    """One max-turns=1 session. Does not write a baseline trial."""
    real_home = Path(os.environ.get("GROK_HOME", Path.home() / ".grok"))
    home = prepare_grok_home(real_home, speedread, "current", None, True)
    env = {**os.environ, "GROK_HOME": str(home)}
    cmd = grok_argv(_PROBE_PROMPT, model, repo, str(uuid.uuid4()), cond, 1)
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env, timeout=180)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return proc


def live_check(speedread, model):
    """Prove the filtered catalog hides trace and that exclusive starts.

    This is the command to run on a machine with grok 1.0.92. It is not the
    baseline and it does not score a task.
    """
    root = Path(tempfile.mkdtemp(prefix="speedread-catalog-"))
    try:
        names = mcp_allow.preflight(speedread, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"filtered tools/list {names}", flush=True)
    repo = make_probe_repo()
    problems = []
    try:
        for cond in ("dropin", "exclusive"):
            proc = run_probe(cond, speedread, model, repo)
            blob = f"{proc.stderr or ''}\n{proc.stdout or ''}"
            init = initialization_failure({"stderr_tail": blob, "errors": []})
            if proc.returncode != 0 or init:
                problems.append(
                    f"{cond}: session did not start (exit {proc.returncode}): {tail(proc.stderr or proc.stdout)}"
                )
                continue
            problem = catalog_problem(proc.stdout)
            if problem:
                problems.append(f"{cond}: {problem}")
                continue
            print(f"{cond}: started, catalog is map/search/read", flush=True)
    finally:
        shutil.rmtree(repo, ignore_errors=True)
    if problems:
        for line in problems:
            print(line, file=sys.stderr, flush=True)
        return 1
    print("live check ok: trace is not in the catalog, exclusive started", flush=True)
    return 0


def plan_jobs(tasks, conditions, trials, models, seed):
    jobs = [(t, c, i, m) for m in models for t in tasks for c in conditions for i in range(trials)]
    random.Random(seed).shuffle(jobs)
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", type=Path)
    ap.add_argument("--tasks", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument(
        "--live-check", action="store_true",
        help="two short sessions: dropin catalog is map/search/read, and exclusive starts. Not the baseline.",
    )
    ap.add_argument("--conditions", default="baseline,dropin")
    ap.add_argument("--trials", type=int, default=5)
    endpoint.add_arguments(ap, default_models=endpoint.GROK_EVAL_DEFAULT_MODELS)
    ap.add_argument("--preflight", action="store_true",
                    help="GET {base}/models and exit. Does not start a trial or print the key.")
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
    ep = endpoint.resolve(
        args.base_url, args.models, args.api_key_env,
        default_models=endpoint.GROK_EVAL_DEFAULT_MODELS,
    )
    args.endpoint = ep
    args.models = ",".join(ep["models"])
    if args.preflight:
        if not ep["base_url"]:
            raise SystemExit(f"preflight needs --base-url or {endpoint.ENV_BASE_URL}")
        sys.stdout.write(endpoint.preflight_report(ep))
        return 0
    if args.live_check:
        model = next(m for m in args.models.split(",") if m)
        return live_check(args.speedread, model)
    if not args.bench or not args.tasks or not args.out:
        ap.error("--bench, --tasks, and --out are required")
    if args.description != "current":
        desc_variant.load_variant(args.description)
    if any(condition_setup(c)[0] for c in args.conditions.split(",") if c):
        catalog_root = Path(tempfile.mkdtemp(prefix="speedread-catalog-"))
        try:
            names = mcp_allow.preflight(args.speedread, catalog_root)
        finally:
            shutil.rmtree(catalog_root, ignore_errors=True)
        print(f"filtered tools/list {names}", flush=True)
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
