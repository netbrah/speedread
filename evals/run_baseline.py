#!/usr/bin/env python3
"""One command for the description-baseline run.

On a machine with Grok CLI, a login or ``XAI_API_KEY``, and enough disk:

    python3 evals/run_baseline.py --bench /path/to/bench

That builds ``target/fast/speedread`` (the repo's ``fast`` profile: release
optimizations without LTO), fetches every pinned repo including Chromium, and
runs the pre-registered baseline in docs/specs/description-ab.md. It prints
every artifact path it writes.

This environment check comes first. If ``grok`` is not on ``PATH``, the
command does not invent a baseline. It runs the fixture self-check and exits.
The live command above is what has to be run where Grok is installed.

``--dry-run`` forces that self-check even when ``grok`` exists.
"""

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PENDING = HERE / "results" / "description-baseline" / "PENDING.md"

# Pre-registered in docs/specs/description-ab.md. Do not shrink this for cost.
PRIMARY = "grok-4.7"
REPLICATION = "grok-4.7-build-fast"
QA_TRIALS = 5
EXCLUSIVE_TRIALS = 1
CODING_TRIALS = 4


def machine_spec():
    mem = "?"
    try:
        mem = subprocess.run(["free", "-h"], capture_output=True, text=True).stdout.splitlines()[1]
    except OSError:
        pass
    load = "?"
    try:
        load = Path("/proc/loadavg").read_text().strip()
    except OSError:
        pass
    nproc = subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip() or "?"
    return {
        "os": platform.platform(),
        "nproc": nproc,
        "mem_line": mem,
        "loadavg": load,
        "python": platform.python_version(),
    }


def have_grok():
    return shutil.which("grok") is not None


def grok_version():
    proc = subprocess.run(["grok", "version"], capture_output=True, text=True)
    if proc.returncode != 0:
        proc = subprocess.run(["grok", "--version"], capture_output=True, text=True)
    return (proc.stdout or proc.stderr).strip().splitlines()[:2]


def models_present(wanted):
    proc = subprocess.run(["grok", "models"], capture_output=True, text=True)
    text = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit(f"`grok models` failed:\n{text[-800:]}")
    missing = [m for m in wanted if m not in text]
    if missing:
        raise SystemExit(
            "grok models did not list " + ", ".join(missing)
            + ".\nPass --models with ids it does list. Refusing to substitute.\n"
            + text[-800:]
        )


def build_fast():
    subprocess.run(["cargo", "build", "--profile", "fast"], cwd=ROOT, check=True)
    binary = ROOT / "target" / "fast" / "speedread"
    if not binary.is_file():
        raise SystemExit(f"build did not produce {binary}")
    return binary


def run_checked(cmd, cwd=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def self_check():
    print("fixture self-check (not a live baseline)", flush=True)
    proc = subprocess.run([sys.executable, str(HERE / "test_description_harness.py")], cwd=ROOT)
    return proc.returncode


def print_plan(spec):
    print("description baseline plan", flush=True)
    print(json.dumps({
        "models": [PRIMARY, REPLICATION],
        "arms": ["baseline", "dropin"],
        "exclusive_control_trials_per_task": EXCLUSIVE_TRIALS,
        "qa_trials_per_task": QA_TRIALS,
        "coding_trials_per_task": CODING_TRIALS,
        "tasks": {
            "suite3": "evals/agent_tasks.json (10)",
            "pin": "evals/pin_tasks.json (13)",
            "chromium": "evals/chromium_tasks.json (14)",
            "coding_descriptive": "evals/coding_tasks.json (8), not in the ship rule",
        },
        "decision_trials_per_arm_per_model": 22 * QA_TRIALS,
        "estimated_spend_usd": "about 500, range 200-1200; see docs/specs/description-ab.md",
        "profile": "fast",
        "machine": spec,
    }, indent=2))


def live(args, spec):
    print_plan(spec)
    print("grok:", *grok_version(), flush=True)
    models = [m for m in args.models.split(",") if m]
    models_present(models)
    binary = build_fast()
    print(f"binary {binary}", flush=True)
    run_checked([sys.executable, str(HERE / "fetch_corpora.py"), "--dest", str(args.bench)])
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = HERE / "results" / f"description-baseline-{stamp}"
    out.mkdir(parents=True)
    (out / "machine.json").write_text(json.dumps({"grok": grok_version(), **spec, "binary": str(binary),
                                                   "profile": "fast", "speedread": subprocess.run(
                                                       [str(binary), "--version"], capture_output=True, text=True).stdout.strip()}, indent=2))
    print(f"machine {out / 'machine.json'}", flush=True)
    common = [
        sys.executable, str(HERE / "grok_eval.py"),
        "--bench", str(args.bench), "--speedread", str(binary),
        "--description", "current", "--models", ",".join(models),
        "--workers", str(args.workers),
    ]
    suites = [
        ("suite3", HERE / "agent_tasks.json"),
        ("pin", HERE / "pin_tasks.json"),
        ("chromium", HERE / "chromium_tasks.json"),
    ]
    for name, tasks in suites:
        dest = out / name
        run_checked(common + [
            "--tasks", str(tasks), "--trials", str(QA_TRIALS),
            "--conditions", "baseline,dropin", "--out", str(dest),
        ])
        # Solvability control, same directory so the transcripts stay with the arm they control.
        run_checked(common + [
            "--tasks", str(tasks), "--trials", str(EXCLUSIVE_TRIALS),
            "--conditions", "exclusive", "--out", str(dest),
        ])
        print(f"trials {dest / 'trials.jsonl'}", flush=True)
        print(f"transcripts {dest / 'transcripts'}", flush=True)
        run_checked([sys.executable, str(HERE / "adoption.py"), "check", str(dest)])
        run_checked([sys.executable, str(HERE / "stats.py"), str(dest), "--adoption", "--arm", "dropin"])
    venv = args.venvs
    if venv and shutil.which("go"):
        dest = out / "coding"
        run_checked(common + [
            "--tasks", str(HERE / "coding_tasks.json"), "--trials", str(CODING_TRIALS),
            "--conditions", "baseline,available", "--coding", "--venvs", str(venv), "--out", str(dest),
            "--timeout", "1800", "--max-turns", "80",
        ])
        print(f"trials {dest / 'trials.jsonl'}", flush=True)
        print(f"diffs {dest / 'diffs'}", flush=True)
    else:
        note = out / "coding-SKIPPED.txt"
        note.write_text("Suite 4 was not started. It needs `go` on PATH and --venvs pointing at a flask virtualenv.\n")
        print(f"skipped coding; wrote {note}", flush=True)
    cli = out / "cli-chromium.txt"
    cli.write_text(
        "CLI comparison, fast profile, description override compiled in and unset.\n"
        "Run from the repo root after the Chromium checkout exists.\n"
        "Five is the run count; report median and range. Wall is noisy when loadavg exceeds nproc.\n"
        f"python3 scripts/fork-big-eval.py chromium {binary}\n"
        "Compare to evals/fork/big-chromium.txt only if chromium/CHECKOUT.txt reports 474447 files.\n"
        "Otherwise label the comparison inconclusive — different tree.\n"
    )
    print(f"cli instructions {cli}", flush=True)
    print(f"run directory {out}", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", type=Path, help="directory of pinned checkouts (created if missing)")
    ap.add_argument("--dry-run", action="store_true", help="fixture self-check only")
    ap.add_argument("--models", default=f"{PRIMARY},{REPLICATION}")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--venvs", type=Path, default=None)
    args = ap.parse_args()
    spec = machine_spec()
    print("machine", json.dumps(spec), flush=True)
    if args.dry_run or not have_grok():
        if not have_grok():
            print("grok is not on PATH. Live baseline was not started.", flush=True)
        print(f"pending note {PENDING}", flush=True)
        print("live command:", flush=True)
        print("  python3 evals/run_baseline.py --bench /path/to/bench", flush=True)
        print(f"design {ROOT / 'docs' / 'specs' / 'description-ab.md'}", flush=True)
        print(f"fixture {HERE / 'fixtures' / 'grok_streaming' / 'dropin_sample.jsonl'}", flush=True)
        return self_check()
    if not args.bench:
        raise SystemExit("--bench is required for the live run")
    return live(args, spec)


if __name__ == "__main__":
    sys.exit(main())
