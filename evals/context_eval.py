#!/usr/bin/env python3
"""Suite 5 — context tool vs a fixed four-tool policy.

The four-tool arm (`quad`) is a deterministic reading policy over the existing
`map`, `search`, `trace`, and `read` commands. It is not a live model: this
environment has no Copilot CLI and no model API, so answer success is Suite 1's
criterion (the tool output contains the graded facts), not a model's prose.

The `context` arm is one `speedread context` call. It is scored by the same
graders. Run `quad` before that command exists; that run is the baseline.

What is measured, per question, over N fresh processes (default 5):

- strict pass: every grader matches the concatenated stdout
- grader fraction: matched graders / graders
- calls: process invocations (the external tool-call count)
- result tokens: tiktoken o200k_base of stdout, the same encoder as Suite 1
- latency: sum of wall-clock seconds of those processes
- peak RSS: max VmHWM (kB) sampled from /proc/<pid>/status during the processes

Page cache is not dropped (no permission). Run 1 is reported separately as the
cold-cache run. Every process is a new process. A gap inside the run range is
not a difference. Token counts and pass/fail do not depend on load; latency and
RSS do.

Usage:
  python3 evals/context_eval.py --bench /tmp/bench --self . \\
      --speedread target/fast/speedread --arm quad --runs 5 \\
      --json evals/results/context-baseline.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import threading
import time
from pathlib import Path

import tiktoken

ENC = tiktoken.get_encoding("o200k_base")

# Lowercased words that are not search terms. A token with '_' or an internal
# capital is kept even if it is listed here.
STOP = {
    "the", "this", "that", "what", "which", "where", "when", "who", "how",
    "does", "did", "with", "from", "into", "onto", "about", "give", "list",
    "name", "file", "files", "line", "lines", "method", "methods", "function",
    "functions", "class", "struct", "interface", "defined", "define",
    "defines", "definition", "start", "starting", "number", "numbers",
    "repository", "repo", "code", "non", "test", "other", "than", "plain",
    "call", "calls", "called", "calling", "directly", "before", "after",
    "running", "under", "holds", "package", "directory", "structural",
    "overview", "integer", "default", "token", "budget", "constant", "tool",
    "return", "returns", "require", "required", "requires", "signature",
    "parameter", "parameters", "body", "not", "and", "for", "are", "its",
    "put", "keys", "dict", "built", "shell", "context", "processors",
    "created", "answer", "one", "each", "site", "sites", "type", "types",
    "version", "declare", "module", "python", "forward", "declaration",
    "include", "include", "holding", "holds",
}

# A dotted match whose second half is one of these is a filename, not Type.method.
EXT = {
    "py", "go", "rs", "h", "cc", "hh", "cpp", "hpp", "ts", "tsx", "js", "swift",
    "toml", "json", "md", "mod",
}
PATH_RE = re.compile(
    r"(?<![\w/])((?:[\w.-]+/)*[\w.-]+\.(?:py|go|rs|h|cc|hh|cpp|hpp|ts|tsx|js|swift|toml|json|md)|go\.mod|Cargo\.toml|package\.json)\b"
)
DOTTED_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)(?:\.|::)([A-Za-z_][A-Za-z0-9_]*)\b"
)
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
RELATE_RE = re.compile(
    r"\b(callers?|who calls|call sites?|callees?|implementations?|implements|who uses|references of)\b",
    re.I,
)
# "which files call X" has words between "files" and "call".
CALL_RE = re.compile(r"\bcalls?\b", re.I)
QUESTION_RE = re.compile(r"\b(what|how|when|why)\b", re.I)
ORIENT_RE = re.compile(r"\b(overview|layout|orient)\b", re.I)
# "does" is not here: "on which line does X start" is a location question.
BEHAVIOR_RE = re.compile(
    r"\b(what|how|why|when|keys|methods|signature|parameter|return|default)\b",
    re.I,
)
# Facts that sit in a function body, not on the declaration line.
BODY_RE = re.compile(r"\b(keys|how|what does|what do|put in)\b", re.I)
LINE_RE = re.compile(
    r"\b(on which line|which line|starting line|line number|declared|defined)\b",
    re.I,
)
# When the question names a grammatical role, search for a definition shape
# instead of every mention of the word.
ROLE_RE = (
    ("class", r"class\s+(?:\w+\s+)*{name}\b"),
    ("struct", r"struct\s+(?:\w+\s+)*{name}\b"),
    ("interface", r"(?:interface|type)\s+{name}\b"),
    ("function", r"(?:function|func|fn|def)\s+(?:\([^)]*\)\s*)?{name}\b"),
    ("method", r"(?:func|fn|def)\s+(?:\([^)]*\)\s*)?{name}\b"),
)


def tokens(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def internal_upper(token: str) -> bool:
    return any(c.isupper() for c in token[1:])


def unique(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def dotted_matches(question: str) -> list[re.Match]:
    found = []
    for match in DOTTED_RE.finditer(question):
        if match.group(2).lower() in EXT:
            continue
        if match.start() > 0 and question[match.start() - 1] == "/":
            continue
        found.append(match)
    return found


def symbols(question: str) -> list[str]:
    """Identifiers the policy will read or search, in a stable order."""
    dotted = [match.group(2) for match in dotted_matches(question)]
    if dotted:
        return unique(dotted)
    extensions = {path.rsplit(".", 1)[-1] for path in paths_of(question) if "." in path}
    raw = [token for token in IDENT_RE.findall(question) if token not in extensions]
    specific = [
        token
        for token in raw
        if ("_" in token or internal_upper(token)) and token.lower() not in STOP
    ]
    if specific:
        return unique(specific)
    plain = [token for token in raw if token.lower() not in STOP]
    if not plain:
        return []
    plain.sort(key=lambda token: (-len(token), token))
    return [plain[0]]


def paths_of(question: str) -> list[str]:
    return unique(PATH_RE.findall(question))


def intent(question: str) -> str:
    if ORIENT_RE.search(question) and not paths_of(question):
        return "orient"
    if RELATE_RE.search(question) or (
        CALL_RE.search(question) and not QUESTION_RE.search(question)
    ):
        return "relate"
    files = paths_of(question)
    names = symbols(question)
    if files and not names:
        return "read"
    if files and names and BODY_RE.search(question):
        return "read"
    return "search"


def trace_target(question: str) -> str | None:
    matches = dotted_matches(question)
    if matches:
        match = matches[0]
        # trace accepts Type.method. C++ Type::method is the same shape.
        return f"{match.group(1)}.{match.group(2)}"
    names = symbols(question)
    if not names:
        return None
    return "#" + names[0]


def direction(question: str) -> str:
    if re.search(r"\b(callees?|what it calls|what .{0,30} calls)\b", question, re.I):
        return "callees"
    if re.search(r"\b(implementations?|implements|implement)\b", question, re.I):
        return "impls"
    if re.search(r"\b(references of|who uses)\b", question, re.I):
        return "refs"
    return "callers"


def depth_of(question: str) -> int:
    if re.search(r"\b(two[- ]hop|exactly one intermediate|depth 2)\b", question, re.I):
        return 2
    return 1


def definition_pattern(question: str, names: list[str]) -> str | None:
    if len(names) != 1:
        return None
    for word, template in ROLE_RE:
        if re.search(rf"\b{word}\b", question, re.I):
            return template.format(name=re.escape(names[0]))
    return None


def search_command(base: list[str], question: str, names: list[str], search_paths: list[str]) -> list[str]:
    role = definition_pattern(question, names)
    if role is not None:
        pattern, flags = role, []
    elif len(names) == 1:
        pattern, flags = names[0], ["-F", "-w"]
    else:
        pattern = "|".join(rf"\b{re.escape(name)}\b" for name in names)
        flags = []
    # A question can say both "where is it defined" and "what methods". The
    # body is what answers the second part, so behavior wins over a line-only view.
    output = "symbols" if BEHAVIOR_RE.search(question) else "matches"
    cmd = base + ["search", pattern, *flags, "--output", output]
    if search_paths:
        cmd.extend(search_paths)
    return cmd


def quad_commands(binary: str, root: str, question: str, budget: int, scope: str | None) -> list[list[str]]:
    """One way an agent uses the four tools. Fixed before any context run.

    Orient → map. Relate → trace. A bare path → read that file. A path plus a
    symbol → read `path#symbol` when the question asks for a body (keys, how);
    otherwise search that file, because line numbers and signatures are on the
    matching line and C++ methods are often not their own outline symbol.
    No path → one search. A named class/struct/interface/function/method is
    searched as a definition pattern, not as every mention of the word.
    """
    kind = intent(question)
    base = [binary, "--root", root, "--budget", str(budget)]
    if kind == "orient":
        cmd = base + ["map", "--symbols"]
        if scope:
            cmd.append(scope)
        return [cmd]
    if kind == "relate":
        target = trace_target(question)
        if target is None:
            cmd = base + ["map", "--symbols"]
            if scope:
                cmd.append(scope)
            return [cmd]
        return [base + [
            "trace",
            target,
            "--direction",
            direction(question),
            "--depth",
            str(depth_of(question)),
        ]]
    files = [scope] if scope else paths_of(question)
    names = symbols(question)
    if files and names and BODY_RE.search(question):
        if len(files) == 1:
            return [base + ["read", *[f"{files[0]}#{name}" for name in names]]]
        return [base + ["read", *files]]
    if files and not names:
        return [base + ["read", *files]]
    if files and names:
        return [search_command(base, question, names, files)]
    if not names:
        cmd = base + ["map", "--symbols"]
        if scope:
            cmd.append(scope)
        return [cmd]
    return [search_command(base, question, names, files)]


def context_commands(binary: str, root: str, question: str, budget: int, scope: str | None) -> list[list[str]]:
    cmd = [binary, "--root", root, "--budget", str(budget), "context", question]
    if scope:
        cmd += ["--path", scope]
    return [cmd]


def run_cmd(cmd: list[str]) -> dict:
    """Run one process. Peak RSS is ru_maxrss from wait4, not a /proc sample.

    A 2 ms sample missed the high-water mark of a 3 ms read (388 KiB recorded,
    13 MiB actual). wait4 is the kernel's peak for that process and does not
    take a core from the timed work.
    """
    started = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    captured: dict[str, bytes] = {}

    def drain(stream, key: str) -> None:
        captured[key] = stream.read()

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, "out")),
        threading.Thread(target=drain, args=(proc.stderr, "err")),
    ]
    for thread in threads:
        thread.start()
    _pid, status, usage = os.wait4(proc.pid, 0)
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started
    text = captured.get("out", b"").decode("utf-8", errors="replace")
    err = captured.get("err", b"").decode("utf-8", errors="replace")
    return {
        "cmd": cmd,
        "exit": os.waitstatus_to_exitcode(status),
        "seconds": elapsed,
        "rss_kb": usage.ru_maxrss,
        "stdout": text,
        "stderr": err,
        "tokens": tokens(text),
        "empty": text.strip() == "",
    }


def check_anchors(root: Path, task: dict) -> None:
    for anchor in task.get("anchors", []):
        path = root / anchor["file"]
        if not path.is_file():
            raise SystemExit(f"{task['id']}: missing anchor file {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if "line" in anchor:
            lines = text.splitlines()
            number = anchor["line"]
            if number < 1 or number > len(lines):
                raise SystemExit(f"{task['id']}: {path} has no line {number}")
            haystack = lines[number - 1]
        else:
            haystack = text
        if not re.search(anchor["pattern"], haystack):
            raise SystemExit(
                f"{task['id']}: anchor failed in {path}: {anchor['pattern']!r} not in {haystack[:180]!r}"
            )


def grade(text: str, graders: list[str]) -> tuple[bool, int]:
    matched = sum(1 for pattern in graders if re.search(pattern, text))
    return matched == len(graders), matched


def median(values: list[float]) -> float:
    return float(statistics.median(values))


def summarize_task(task: dict, runs: list[dict]) -> dict:
    passes = []
    fractions = []
    token_values = []
    latency = []
    rss = []
    digests = []
    errors = 0
    for run in runs:
        text = "".join(step["stdout"] for step in run["steps"])
        ok, matched = grade(text, task["graders"])
        passes.append(ok)
        fractions.append(matched / len(task["graders"]))
        token_values.append(sum(step["tokens"] for step in run["steps"]))
        latency.append(sum(step["seconds"] for step in run["steps"]))
        rss.append(max(step["rss_kb"] for step in run["steps"]))
        digests.append(hashlib.sha256(text.encode()).hexdigest())
        errors += sum(1 for step in run["steps"] if step["exit"] != 0 or step["empty"])
    return {
        "id": task["id"],
        "repo": task["repo"],
        "kind": task["kind"],
        "intent": intent(task["prompt"]),
        "calls": len(runs[0]["steps"]),
        "commands": [step["cmd"] for step in runs[0]["steps"]],
        "strict_passes": passes,
        "strict_pass": all(passes),
        "grader_fraction": fractions,
        "tokens": token_values,
        "tokens_median": median(token_values),
        "tokens_min": min(token_values),
        "tokens_max": max(token_values),
        "latency_s": latency,
        "latency_median_s": median(latency),
        "latency_min_s": min(latency),
        "latency_max_s": max(latency),
        "latency_cold_s": latency[0],
        "rss_kb": rss,
        "rss_median_kb": median(rss),
        "rss_min_kb": min(rss),
        "rss_max_kb": max(rss),
        "rss_cold_kb": rss[0],
        "errors": errors,
        "deterministic": len(set(digests)) == 1,
        "stdout_sha256": digests[0],
        "stdout": "".join(step["stdout"] for step in runs[0]["steps"]),
        "stderr": "".join(step["stderr"] for step in runs[0]["steps"])[:4000],
    }


def repo_root(bench: Path, self_root: Path, spec: dict, name: str) -> Path:
    if name == "speedread":
        return self_root
    return bench / spec["dir"]


def loadavg() -> str:
    return Path("/proc/loadavg").read_text().strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default="evals/context_tasks.json")
    parser.add_argument("--bench", required=True, help="directory containing flask, gin, chromium-subset")
    parser.add_argument("--self", default=".", help="speedread checkout used as the speedread repo")
    parser.add_argument("--speedread", default="target/fast/speedread")
    parser.add_argument("--arm", choices=("quad", "context"), default="quad")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--json", required=True)
    parser.add_argument("--only", action="append", default=[], help="task id, repeatable")
    args = parser.parse_args()

    suite = json.loads(Path(args.tasks).read_text())
    binary = str(Path(args.speedread).resolve())
    bench = Path(args.bench).resolve()
    self_root = Path(args.self).resolve()
    if not Path(binary).is_file():
        raise SystemExit(f"speedread binary not found: {binary}")

    tasks = suite["tasks"]
    if args.only:
        tasks = [task for task in tasks if task["id"] in args.only]
        if not tasks:
            raise SystemExit("no tasks matched --only")

    roots = {}
    heads = {}
    for task in tasks:
        name = task["repo"]
        spec = suite["repos"][name]
        root = repo_root(bench, self_root, spec, name)
        git_dir = self_root if name == "speedread" else root
        if (git_dir / ".git").exists():
            head = subprocess.check_output(
                ["git", "-C", str(git_dir), "rev-parse", "HEAD"], text=True
            ).strip()
        else:
            head = spec["commit"]
        # Bench repos are pinned. speedread is pinned for this baseline run;
        # later commits in this checkout still have to satisfy the file anchors.
        if name != "speedread" and not head.startswith(spec["commit"]):
            raise SystemExit(f"{name}: checkout {head} != pinned {spec['commit']}")
        check_anchors(root, task)
        roots[name] = str(root)
        heads[name] = head

    builder = quad_commands if args.arm == "quad" else context_commands
    machine_before = {"nproc": os.cpu_count(), "loadavg": loadavg()}
    measured = []
    for task in tasks:
        root = roots[task["repo"]]
        commands = builder(binary, root, task["prompt"], suite["budget"], task.get("scope"))
        runs = []
        for _ in range(args.runs):
            steps = [run_cmd(cmd) for cmd in commands]
            runs.append({"steps": steps})
        measured.append(summarize_task(task, runs))

    profiles = []
    for task in tasks:
        if not task.get("profile"):
            continue
        root = roots[task["repo"]]
        commands = builder(binary, root, task["prompt"], suite["budget"], task.get("scope"))
        for cmd in commands:
            env = os.environ.copy()
            env["SPEEDREAD_STATS"] = "1"
            started = time.perf_counter()
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
            profiles.append(
                {
                    "id": task["id"],
                    "cmd": cmd,
                    "seconds": time.perf_counter() - started,
                    "exit": proc.returncode,
                    "stdout_tokens": tokens(proc.stdout),
                    "stderr": proc.stderr[-2000:],
                    "note": "not included in the N-run medians",
                }
            )

    passed = [row for row in measured if row["strict_pass"]]
    answered_tokens = [row["tokens_median"] for row in passed]
    report = {
        "suite": suite["suite"],
        "arm": args.arm,
        "decision": suite["decision"],
        "budget": suite["budget"],
        "runs": args.runs,
        "encoder": "o200k_base",
        "binary": binary,
        "repos": {
            name: {"root": roots[name], "head": heads[name], **suite["repos"][name]}
            for name in roots
        },
        "policy": "quad: orient→map, relate→trace, path+symbol or bare path→read, else one search (symbols if the question asks what/how, matches if it asks where). context: one context call. No live model.",
        "machine_before": machine_before,
        "machine_after": {"nproc": os.cpu_count(), "loadavg": loadavg()},
        "tasks": measured,
        "profile": profiles,
        "aggregate": {
            "questions": len(measured),
            "strict_pass": len(passed),
            "strict_pass_rate": len(passed) / len(measured),
            "calls_mean": sum(row["calls"] for row in measured) / len(measured),
            "calls_per_answered": (
                sum(row["calls"] for row in passed) / len(passed) if passed else None
            ),
            "tokens_median_of_question_medians": median([row["tokens_median"] for row in measured]),
            "tokens_mean_per_answered": (
                sum(answered_tokens) / len(answered_tokens) if answered_tokens else None
            ),
            "tokens_median_per_answered": median(answered_tokens) if answered_tokens else None,
            "latency_median_of_question_medians_s": median(
                [row["latency_median_s"] for row in measured]
            ),
            "rss_median_of_question_medians_kb": median([row["rss_median_kb"] for row in measured]),
            "rss_max_of_question_max_kb": max(row["rss_max_kb"] for row in measured),
            "errors": sum(row["errors"] for row in measured),
            "nondeterministic": [row["id"] for row in measured if not row["deterministic"]],
        },
    }
    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Full stdout is the audit trail. Drop it from the human summary only.
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(markdown(report))
    print(f"\nwrote {out}")


def markdown(report: dict) -> str:
    lines = [
        f"arm `{report['arm']}`, {report['runs']} runs, budget {report['budget']}, encoder {report['encoder']}",
        "",
        "| Question | Pass | Calls | Tokens median (min–max) | Latency median s (min–max) | Cold s | RSS median KiB (min–max) | Errors | Stable |",
        "|---|:---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for row in report["tasks"]:
        flag = "yes" if row["strict_pass"] else "no"
        lines.append(
            f"| {row['id']} | {flag} | {row['calls']} | {row['tokens_median']:.0f} ({row['tokens_min']:.0f}–{row['tokens_max']:.0f}) | "
            f"{row['latency_median_s']:.3f} ({row['latency_min_s']:.3f}–{row['latency_max_s']:.3f}) | {row['latency_cold_s']:.3f} | "
            f"{row['rss_median_kb']:.0f} ({row['rss_min_kb']:.0f}–{row['rss_max_kb']:.0f}) | {row['errors']} | "
            f"{'yes' if row['deterministic'] else 'NO'} |"
        )
    agg = report["aggregate"]
    lines.append("")
    lines.append(
        f"strict pass {agg['strict_pass']}/{agg['questions']} ({agg['strict_pass_rate']:.0%}); "
        f"mean calls {agg['calls_mean']:.2f}; "
        f"mean result tokens per answered question {agg['tokens_mean_per_answered']}; "
        f"median latency {agg['latency_median_of_question_medians_s']:.3f}s; "
        f"median RSS {agg['rss_median_of_question_medians_kb']:.0f} KiB; "
        f"max RSS {agg['rss_max_of_question_max_kb']:.0f} KiB; "
        f"errors {agg['errors']}"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
