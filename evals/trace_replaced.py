#!/usr/bin/env python3
"""Are trace questions answerable with search and one batched read?

Usage: python3 evals/trace_replaced.py AFTER_BINARY [BEFORE_BINARY] [BENCH]

BENCH is a directory with gin/, flask/ and ripgrep/ checkouts. The relationship
section records those commits. Pass means every labeled snippet, or every task
grader regex, is present in the tool output. Tokens are src/tokens.rs estimates.

Compares, on the hand-labeled fixture and (when the repos are present) the
relationship tasks:

  trace     one `trace` call per question (the removed tool)
  search    one whole-word search; hits are grouped under enclosing symbols
  batch     that search, then ONE `read` of every enclosing symbol

Pass means every labeled line (or grader regex) is present in the answer.
Tokens are speedread's content-aware estimate (src/tokens.rs).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

W_DIGITS, W_PUNCT, W_WS_RUNS = 0.2309, 0.5142, 0.1415
W_NEWLINES, W_NON_ASCII, W_WORDS = 3.1389, 1.8830, 1.1830
W_DIGIT_RUNS, W_CASE_BREAKS, W_DENSE_RUNS = 1.3637, 1.3540, 0.1716
OTHER, LOWER, UPPER, DIGIT, SPACE = 0, 1, 2, 3, 4


class Counter:
    def __init__(self):
        self.digits = self.punct = self.ws_runs = self.newlines = 0
        self.non_ascii = self.words = self.digit_runs = self.case_breaks = self.dense = 0
        self.prev = OTHER
        self.run_len = 0
        self.run_alpha = self.run_digit = False

    def feed(self, data: bytes):
        for c in data:
            lower, upper, digit = 97 <= c <= 122, 65 <= c <= 90, 48 <= c <= 57
            if lower or upper or digit or c in (43, 47, 61, 95, 45):
                self.run_len += 1
                self.run_alpha = self.run_alpha or lower or upper
                self.run_digit = self.run_digit or digit
            else:
                self.end_run()
            if lower or upper:
                if self.prev not in (LOWER, UPPER):
                    self.words += 1
                elif upper and self.prev == LOWER:
                    self.case_breaks += 1
                self.prev = LOWER if lower else UPPER
            elif digit:
                self.digits += 1
                if self.prev != DIGIT:
                    self.digit_runs += 1
                self.prev = DIGIT
            elif c == 10:
                self.newlines += 1
                self.prev = OTHER
            elif c in (32, 9, 13):
                if self.prev != SPACE:
                    self.ws_runs += 1
                self.prev = SPACE
            elif c >= 0xC0:
                self.non_ascii += 1
                self.prev = OTHER
            elif c < 0x80:
                self.punct += 1
                self.prev = OTHER
            else:
                self.prev = OTHER

    def end_run(self):
        if self.run_len >= 16 and self.run_alpha and self.run_digit:
            self.dense += self.run_len
        self.run_len = 0
        self.run_alpha = self.run_digit = False

    def tokens(self) -> float:
        self.end_run()
        return (
            self.digits * W_DIGITS
            + self.punct * W_PUNCT
            + self.ws_runs * W_WS_RUNS
            + self.newlines * W_NEWLINES
            + self.non_ascii * W_NON_ASCII
            + self.words * W_WORDS
            + self.digit_runs * W_DIGIT_RUNS
            + self.case_breaks * W_CASE_BREAKS
            + self.dense * W_DENSE_RUNS
        )


def estimate(text: str) -> float:
    c = Counter()
    c.feed(text.encode())
    return c.tokens()


def run(binary: str, root: Path, args: list[str]) -> str:
    p = subprocess.run(
        [str(Path(binary).resolve()), "--root", str(root), "--no-cwd-root", *args],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-400:] or p.stdout[-400:])
    return p.stdout


def enclosing_targets(search_out: str) -> list[str]:
    """`path:start` for each `[start-end]` group, one per symbol, for one read."""
    path = None
    out = []
    for line in search_out.splitlines():
        if line.startswith("==> "):
            path = line[4:].split(" @", 1)[0].split(" (", 1)[0].strip()
            continue
        m = re.match(r"\[(\d+)-\d+\]", line.strip())
        if m and path:
            target = f"{path}:{m.group(1)}"
            if target not in out:
                out.append(target)
    return out


def name_of(case: dict, root: Path) -> str:
    if case.get("target", "").startswith("src/") and "#" in case["target"]:
        return case["target"].split("#", 1)[1]
    text = (root / case["target_file"]).read_text()
    line = next(i for i, l in enumerate(text.splitlines(), 1) if case["target_line_contains"] in l)
    if case.get("target_line_contains_next"):
        line = next(
            i
            for i, l in enumerate(text.splitlines(), 1)
            if i > line and case["target_line_contains_next"] in l
        )
    src = text.splitlines()[line - 1]
    m = re.search(r"\b(?:fn|trait|struct)\s+(\w+)", src)
    return m.group(1) if m else case.get("ra_name", "alpha")


def expect_snippets(case: dict) -> list[str]:
    bits = [item["contains"] for item in case.get("expect") or []]
    for item in case.get("expect_ranges") or []:
        bits.append(item["start_contains"])
    for item in case.get("expect_edges") or []:
        bits.append(item["call_contains"])
    return bits


def covers(text: str, snippets: list[str]) -> bool:
    return all(s in text for s in snippets)


def fixture(binary_trace: str | None, binary_after: str, root: Path) -> dict:
    truth = json.loads((root.parent / "trace_oracle_truth.json").read_text())
    rows = []
    for case in truth["cases"]:
        if case["direction"] == "value":
            continue  # the value line is inside the callers/refs cases
        name = name_of(case, root)
        snippets = expect_snippets(case)
        row = {"id": case["id"], "name": name, "snippets": len(snippets)}
        if binary_trace:
            args = ["trace", _trace_target(case, root)]
            if case["direction"] != "callers":
                args += ["--direction", case["direction"]]
            out = run(binary_trace, root, args)
            row["trace"] = {"calls": 1, "tokens": round(estimate(out), 1), "pass": covers(out, snippets)}
        searched = run(binary_after, root, ["search", "-w", "-F", name])
        targets = enclosing_targets(searched)
        # One read of every enclosing symbol the search named.
        batched = ""
        if targets:
            batched = run(binary_after, root, ["read", *targets[:40], "--budget", "20000", "--max-budget", "20000"])
        both = searched + "\n" + batched
        # The two source files, one call: the whole graph is small enough to batch.
        files = run(binary_after, root, ["read", "src/lib.rs", "src/other.rs", "--budget", "20000", "--max-budget", "20000"])
        row["search"] = {"calls": 1, "tokens": round(estimate(searched), 1), "pass": covers(searched, snippets), "symbols": len(targets)}
        row["search_plus_batched_read"] = {
            "calls": 1 + int(bool(targets)),
            "tokens": round(estimate(both), 1),
            "pass": covers(both, snippets),
            "read_targets": len(targets),
        }
        row["one_batched_read"] = {"calls": 1, "tokens": round(estimate(files), 1), "pass": covers(files, snippets)}
        rows.append(row)
    return {"cases": rows, "one_read_tokens": rows[0]["one_batched_read"]["tokens"] if rows else None}


def _trace_target(case: dict, root: Path) -> str:
    if case.get("target"):
        return case["target"]
    text = (root / case["target_file"]).read_text()
    line = next(i for i, l in enumerate(text.splitlines(), 1) if case["target_line_contains"] in l)
    if case.get("target_line_contains_next"):
        line = next(i for i, l in enumerate(text.splitlines(), 1) if i > line and case["target_line_contains_next"] in l)
    return f"{case['target_file']}:{line}"


def grade(text: str, graders: list[str]) -> dict:
    misses = [g for g in graders if not re.search(g, text)]
    return {
        "tokens": round(estimate(text), 1),
        "graders": len(graders),
        "passed": len(graders) - len(misses),
        "pass": not misses,
        "misses": misses,
    }


def go_funcs(search_out: str) -> list[str]:
    names = []
    for line in search_out.splitlines():
        m = re.match(r"\[\d+-\d+\]\s+func\s+(?:\([^)]*\)\s+)?(\w+)", line.strip())
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def relationships(binary_trace: str | None, binary_after: str, bench: Path) -> dict:
    """Fixed search-then-one-read procedures for the four relationship questions.

    Repos are the default-branch tips at the end of 2026-09-24 (the suite's run
    date). The suite never recorded its commits; flask's definition lines match
    the graders at this flask commit.
    """
    tasks = json.loads((Path(__file__).resolve().parent / "relationship_tasks.json").read_text())
    gin, flask, rg = bench / "gin", bench / "flask", bench / "ripgrep"
    commits = {
        "gin": subprocess.check_output(["git", "-C", str(gin), "rev-parse", "HEAD"], text=True).strip(),
        "flask": subprocess.check_output(["git", "-C", str(flask), "rev-parse", "HEAD"], text=True).strip(),
        "ripgrep": subprocess.check_output(["git", "-C", str(rg), "rev-parse", "HEAD"], text=True).strip(),
    }
    by_id = {t["id"]: t["graders"] for t in tasks["tasks"]}
    rows = []

    def row(task_id, root, trace_args, searches, reads):
        graders = by_id[task_id]
        item = {"id": task_id}
        if binary_trace:
            out = run(binary_trace, root, ["trace", *trace_args])
            item["trace"] = {"calls": 1, **grade(out, graders)}
        parts = []
        for args in searches:
            parts.append(run(binary_after, root, ["search", *args]))
        searched = "\n".join(parts)
        targets = []
        for part in parts:
            for t in enclosing_targets(part):
                if t not in targets:
                    targets.append(t)
        # A caller-following question names the next hop from the first search.
        if task_id == "gin-abort-two-hop" and len(searches) == 1:
            names = [n for n in go_funcs(parts[0]) if n != "AbortWithStatus"]
            if names:
                hop = run(
                    binary_after,
                    root,
                    ["search", "-w", "-g", "*.go", "-g", "!*_test.go", "|".join(names)],
                )
                parts.append(hop)
                searched = "\n".join(parts)
                for t in enclosing_targets(hop):
                    if t not in targets:
                        targets.append(t)
        read_out = ""
        if reads:
            read_out = run(binary_after, root, ["read", *reads])
        elif targets:
            read_out = run(binary_after, root, ["read", *targets[:40]])
        n_search = len(parts)
        n_read = int(bool(read_out))
        item["search"] = {"calls": n_search, **grade(searched, graders)}
        item["search_plus_batched_read"] = {
            "calls": n_search + n_read,
            "read_targets": len(reads) if reads else min(len(targets), 40),
            **grade(searched + "\n" + read_out, graders),
        }
        rows.append(item)

    row(
        "gin-render-impls",
        gin,
        ["render/render.go#Render", "--direction", "impls"],
        [["-g", "!*_test.go", r"_ Render|func \(r [A-Za-z0-9]+\) Render\(", "render"]],
        [],
    )
    row(
        "ripgrep-sink-impls",
        rg,
        ["crates/searcher/src/sink.rs#Sink", "--direction", "impls"],
        [[r"impl(<[^>]*>)? Sink for"]],
        [],
    )
    # Callees are named in the function body; their definition lines are one batched read.
    body = run(binary_after, flask, ["read", "src/flask/app.py#full_dispatch_request"])
    methods = []
    for name in re.findall(r"self\.(\w+)\(", body):
        if name not in methods:
            methods.append(name)
    defs = [f"src/flask/app.py#{name}" for name in methods]
    graders = by_id["flask-dispatch-callees"]
    flask_row = {"id": "flask-dispatch-callees"}
    if binary_trace:
        out = run(binary_trace, flask, ["trace", "src/flask/app.py#full_dispatch_request", "--direction", "callees"])
        flask_row["trace"] = {"calls": 1, **grade(out, graders)}
    batched = run(binary_after, flask, ["read", *defs]) if defs else ""
    both = body + "\n" + batched
    flask_row["read_body"] = {"calls": 1, **grade(body, graders)}
    flask_row["search"] = flask_row["read_body"]
    flask_row["search_plus_batched_read"] = {
        "calls": 1 + int(bool(defs)),
        "read_targets": len(defs),
        "methods": methods,
        **grade(both, graders),
    }
    rows.append(flask_row)
    row(
        "gin-abort-two-hop",
        gin,
        ["Context.AbortWithStatus", "--depth", "2"],
        [["-w", "-F", "AbortWithStatus", "-g", "*.go", "-g", "!*_test.go"]],
        [],
    )
    return {"commits": commits, "tasks": rows}


def tool_schema(binary: str) -> dict:
    p = subprocess.Popen(
        [str(Path(binary).resolve()), "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    n = 0

    def call(method, params):
        nonlocal n
        n += 1
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params}) + "\n").encode())
        p.stdin.flush()
        while True:
            msg = json.loads(p.stdout.readline())
            if msg.get("id") == n:
                return msg

    init = call(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "schema", "version": "0"}},
    )["result"]
    p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    p.stdin.flush()
    tools = call("tools/list", {})["result"]["tools"]
    p.kill()
    blob = json.dumps(tools)
    trace = next((t for t in tools if t["name"] == "trace"), None)
    instructions = init.get("instructions") or ""
    return {
        "names": [t["name"] for t in tools],
        "tools_tokens": round(estimate(blob), 1),
        "trace_tool_tokens": round(estimate(json.dumps(trace)), 1) if trace else 0,
        "instructions_tokens": round(estimate(instructions), 1),
    }


def main():
    here = Path(__file__).resolve().parent.parent
    after = sys.argv[1]
    before = sys.argv[2] if len(sys.argv) > 2 else ""
    bench = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    root = here / "evals" / "fixtures" / "trace_oracle"
    report = {
        "binary_bytes": {"after": os.path.getsize(Path(after).resolve())},
        "fixture": fixture(before or None, after, root),
        "schema_after": tool_schema(after),
    }
    if before:
        report["binary_bytes"]["before"] = os.path.getsize(Path(before).resolve())
        report["schema_before"] = tool_schema(before)
        b, a = report["schema_before"]["tools_tokens"], report["schema_after"]["tools_tokens"]
        report["definition_tokens_saved_per_call"] = round(b - a, 1)
        ib, ia = report["schema_before"]["instructions_tokens"], report["schema_after"]["instructions_tokens"]
        report["instruction_tokens_saved_per_call"] = round(ib - ia, 1)
    if bench:
        report["relationships"] = relationships(before or None, after, bench)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
