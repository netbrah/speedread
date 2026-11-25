#!/usr/bin/env python3
"""One command for the description-baseline run.

On a machine with Grok CLI, a login or ``XAI_API_KEY``, and enough disk:

    python3 evals/run_baseline.py --bench /path/to/bench

That builds ``target/fast/speedread`` (the repo's ``fast`` profile: release
optimizations without LTO), fetches every pinned repo including Chromium,
creates ``<bench>/venvs/flask`` from ``evals/flask-tests.txt`` when it is
missing, and runs the pre-registered baseline in
docs/specs/description-ab.md, including Suite 4. Go must be on ``PATH``.
It prints every artifact path it writes.

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
# Planning prices, not a measurement. Same figures as the design doc.
SMALL_TRIAL_USD = 0.30
CHROMIUM_TRIAL_USD = 1.50
CODING_TRIAL_USD = 1.00
FLASK_REQUIREMENTS = HERE / "flask-tests.txt"


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


def _task_count(name):
    return len(json.loads((HERE / name).read_text())["tasks"])


def plan_counts():
    """Trial totals from the task files, not from a hardcoded task count."""
    suite3 = _task_count("agent_tasks.json")
    pin = _task_count("pin_tasks.json")
    chromium = _task_count("chromium_tasks.json")
    coding = _task_count("coding_tasks.json")
    decision = suite3 + pin + chromium
    per_arm = decision * QA_TRIALS
    models = 2
    arms = 2
    decision_trials = per_arm * arms * models
    exclusive = decision * EXCLUSIVE_TRIALS * models
    coding_trials = coding * CODING_TRIALS * arms * models
    small_sessions = (suite3 + pin) * (QA_TRIALS * arms * models + EXCLUSIVE_TRIALS * models)
    chrom_sessions = chromium * (QA_TRIALS * arms * models + EXCLUSIVE_TRIALS * models)
    spend = small_sessions * SMALL_TRIAL_USD + chrom_sessions * CHROMIUM_TRIAL_USD + coding_trials * CODING_TRIAL_USD
    return {
        "suite3": suite3,
        "pin": pin,
        "chromium": chromium,
        "coding": coding,
        "decision_tasks": decision,
        "decision_trials_per_arm_per_model": per_arm,
        "decision_trials": decision_trials,
        "exclusive_trials": exclusive,
        "coding_trials": coding_trials,
        "total_trials": decision_trials + exclusive + coding_trials,
        "estimated_spend_usd": round(spend),
    }


def plan_payload(spec):
    counts = plan_counts()
    return {
        "models": [PRIMARY, REPLICATION],
        "arms": ["baseline", "dropin"],
        "exclusive_control_trials_per_task": EXCLUSIVE_TRIALS,
        "qa_trials_per_task": QA_TRIALS,
        "coding_trials_per_task": CODING_TRIALS,
        "tasks": {
            "suite3": f"evals/agent_tasks.json ({counts['suite3']})",
            "pin": f"evals/pin_tasks.json ({counts['pin']})",
            "chromium": f"evals/chromium_tasks.json ({counts['chromium']})",
            "coding_descriptive": f"evals/coding_tasks.json ({counts['coding']}), not in the ship rule",
        },
        "decision_trials_per_arm_per_model": counts["decision_trials_per_arm_per_model"],
        "decision_trials": counts["decision_trials"],
        "exclusive_trials": counts["exclusive_trials"],
        "coding_trials": counts["coding_trials"],
        "total_trials": counts["total_trials"],
        "estimated_spend_usd": counts["estimated_spend_usd"],
        "estimated_spend_note": (
            f"{SMALL_TRIAL_USD:.2f} per small Q&A trial, {CHROMIUM_TRIAL_USD:.2f} per Chromium trial, "
            f"{CODING_TRIAL_USD:.2f} per coding trial; not a measurement"
        ),
        "profile": "fast",
        "machine": spec,
    }


def print_plan(spec):
    print("description baseline plan", flush=True)
    print(json.dumps(plan_payload(spec), indent=2))


def ensure_flask_venv(bench, venvs):
    """Create ``<venvs>/flask`` from the pinned requirements if it is missing.

    The requirements are the flask checkout's ``uv.lock`` test group at the
    corpora pin, exported with hashes. Pytest runs with ``PYTHONPATH=src``,
    so Flask itself is the workspace copy and is not installed here.
    """
    dest = Path(venvs) / "flask"
    pytest_bin = dest / "bin" / "pytest"
    if pytest_bin.is_file():
        print(f"flask venv {dest}", flush=True)
        return dest
    if not (bench / "flask").is_dir():
        raise SystemExit(f"{bench / 'flask'} is not checked out; fetch corpora before creating the venv")
    if not FLASK_REQUIREMENTS.is_file():
        raise SystemExit(f"missing {FLASK_REQUIREMENTS}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"creating flask venv {dest}", flush=True)
    run_checked([sys.executable, "-m", "venv", str(dest)])
    run_checked([
        str(dest / "bin" / "pip"), "install", "--disable-pip-version-check",
        "--require-hashes", "-r", str(FLASK_REQUIREMENTS),
    ])
    (dest / "PIN.txt").write_text(
        "requirements evals/flask-tests.txt\n"
        "flask pin d73fa1cdcbd8b1465c151db8924ba58b1dd14e35\n"
    )
    if not pytest_bin.is_file():
        raise SystemExit(f"venv install did not produce {pytest_bin}")
    return dest


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
    if not shutil.which("go"):
        raise SystemExit("Suite 4 needs `go` on PATH. The baseline is 942 trials, including the coding arm.")
    venv = args.venvs if args.venvs else args.bench / "venvs"
    ensure_flask_venv(args.bench, venv)
    dest = out / "coding"
    run_checked(common + [
        "--tasks", str(HERE / "coding_tasks.json"), "--trials", str(CODING_TRIALS),
        "--conditions", "baseline,available", "--coding", "--venvs", str(venv), "--out", str(dest),
        "--timeout", "1800", "--max-turns", "80",
    ])
    print(f"trials {dest / 'trials.jsonl'}", flush=True)
    print(f"diffs {dest / 'diffs'}", flush=True)
    cli = out / "cli-chromium.txt"
    cli.write_text(
        "CLI comparison, fast profile, description override compiled in and unset.\n"
        "Run from the repo root after the Chromium checkout exists.\n"
        "Five is the run count; report median and range. Wall is noisy when loadavg exceeds nproc.\n"
        f"python3 scripts/fork-big-eval.py chromium {binary}\n"
        "This checkout of the pinned SHA measured 476800 files and 5.7G.\n"
        "docs/specs/eval.md records 474447 files and 4.5 GB. Those counts differ,\n"
        "so do not compare these timings to evals/fork/big-chromium.txt as the same tree.\n"
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
    ap.add_argument("--venvs", type=Path, default=None,
                    help="directory of virtualenvs (default: <bench>/venvs). flask is created there from evals/flask-tests.txt if missing")
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
