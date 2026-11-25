#!/usr/bin/env python3
"""matches vs symbols vs auto: success, tokens, follow-up reads.

The decision rules are fixed in docs/specs/09-adaptive-search-output.md.
Tokens are o200k_base, the encoder evals/tool_eval.py uses. The oracle search
that lists required symbols is not counted: an agent does not run it.

  python3 evals/search_output_eval.py --bench target/eval-repos \\
      --speedread target/release/speedread --calibrate \\
      --json evals/results/search-output-auto/eval.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

import tiktoken

ENC = tiktoken.get_encoding("o200k_base")
BUDGET = 8000
NS = [1, 2, 3, 4, 6, 8, 12]
KS = [25, 40, 50, 60, 75, 100]
SPAN_CAP = 200  # SYMBOL_INLINE_MAX_SPAN; header end-start, not a new threshold

CENSUS_RE = re.compile(
    r"auto-census distinct=(\d+) inlineable=(\d+) body_tokens=(\d+) "
    r"response_tokens=(\d+) overflow=(\d) complete=(\d)"
)
# `matches?` is "matche" or "matches", not "match". One hit is "1 match".
FILE_RE = re.compile(r"^==> (\S+) @([0-9a-f]{16}) \((\d+) match(?:es)?\)$")
SYM_RE = re.compile(r"^\[(\d+)-(\d+)\] ")


def tok(s: str) -> int:
    return len(ENC.encode(s, disallowed_special=()))


def run(binary, root, args, env=None, timeout=180):
    p = subprocess.run(
        [binary, "--root", root, *args],
        cwd="/",
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return p.returncode, p.stdout, p.stderr


def file_lines(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_matches(out: str):
    """Files in display order, each with its match count and symbol headers."""
    files = []
    cur = None
    for line in out.splitlines():
        m = FILE_RE.match(line)
        if m:
            cur = {
                "path": m.group(1),
                "matches": int(m.group(3)),
                "symbols": [],
                "hit_lines": [],
            }
            files.append(cur)
            continue
        if cur is None:
            continue
        sm = SYM_RE.match(line)
        if sm:
            cur["symbols"].append(
                {"start": int(sm.group(1)), "end": int(sm.group(2)), "hits": []}
            )
            continue
        if line[:1].isdigit() and "\t" in line:
            n_s, _ = line.split("\t", 1)
            if n_s.isdigit() and cur["symbols"]:
                n = int(n_s)
                cur["hit_lines"].append(n)
                sym = cur["symbols"][-1]
                if sym["start"] <= n <= sym["end"]:
                    sym["hits"].append(n)
                else:
                    # A hit before the next header belongs to the previous one
                    # only when it falls in range. Otherwise leave it on the file.
                    pass
    return files


def oracle_symbols(root, files, family):
    ranked = sorted(files, key=lambda f: (-f["matches"], f["path"]))
    chosen = ranked if family == "narrow" else ranked[:3]
    required = []
    for f in chosen:
        disk = os.path.join(root, f["path"])
        if not os.path.isfile(disk):
            continue
        src = file_lines(disk)
        for s in f["symbols"]:
            span = s["end"] - s["start"]
            rows = []
            if span <= SPAN_CAP:
                for n in range(s["start"], s["end"] + 1):
                    if 1 <= n <= len(src) and src[n - 1].strip():
                        rows.append((n, src[n - 1]))
            else:
                for n in s["hits"]:
                    if 1 <= n <= len(src):
                        rows.append((n, src[n - 1]))
            if rows:
                required.append(
                    {"path": f["path"], "start": s["start"], "end": s["end"], "rows": rows}
                )
    return required


def rows_present(blob: str, rows) -> bool:
    for n, text in rows:
        key = f"{n}\t{text}" if len(text) <= 300 else f"{n}\t{text[:300]}"
        if key not in blob:
            return False
    return True


def header_shown(search_out: str, path: str, start: int) -> bool:
    parts = search_out.split(f"==> {path} ")
    if len(parts) < 2:
        return False
    section = parts[1].split("\n==> ", 1)[0]
    return f"[{start}-" in section


def answer(binary, root, search_out, required):
    """Follow-ups an agent can issue from this output alone."""
    blob = search_out
    calls = 1
    pieces = [search_out]
    failed = []
    seen_reads = set()
    for sym in required:
        if rows_present(blob, sym["rows"]):
            continue
        target = None
        # The header's range, not `path:line`. `path:line` re-resolves the
        # enclosing symbol and can return a different span than the header
        # (a markdown heading inside a Go doc comment, for example).
        if header_shown(search_out, sym["path"], sym["start"]):
            target = f"{sym['path']}:{sym['start']}-{sym['end']}"
        elif sym["path"] in search_out:
            target = sym["path"]
        if target is None:
            failed.append(sym["path"])
            continue
        if target not in seen_reads:
            seen_reads.add(target)
            code, out, err = run(binary, root, ["read", target])
            if code != 0:
                failed.append(sym["path"] + " read-failed")
                continue
            calls += 1
            pieces.append(out)
            blob += "\n" + out
        if not rows_present(blob, sym["rows"]):
            # A whole-file read of a large file comes back as a skeleton.
            # One more read of the line the skeleton named, if the header is
            # now in the combined output.
            line_target = f"{sym['path']}:{sym['start']}-{sym['end']}"
            if line_target not in seen_reads and f"[{sym['start']}-" in blob:
                seen_reads.add(line_target)
                code, out, _ = run(binary, root, ["read", line_target])
                if code == 0:
                    calls += 1
                    pieces.append(out)
                    blob += "\n" + out
        if not rows_present(blob, sym["rows"]):
            failed.append(f"{sym['path']}:{sym['start']}")
    return {
        "ok": not failed,
        "calls": calls,
        "tokens": sum(tok(p) for p in pieces),
        "failed": failed[:8],
        "followups": calls - 1,
    }


def chooses_inline(c, n, k, budget=BUDGET) -> bool:
    if (not c["complete"]) or c["overflow"] or c["inlineable"] == 0 or c["distinct"] > n:
        return False
    if c["body_tokens"] * 100 > budget * k:
        return False
    if c["response_tokens"] > budget:
        return False
    return True


def load_census(err: str):
    m = CENSUS_RE.search(err)
    if not m:
        return None
    return {
        "distinct": int(m.group(1)),
        "inlineable": int(m.group(2)),
        "body_tokens": int(m.group(3)),
        "response_tokens": int(m.group(4)),
        "overflow": m.group(5) == "1",
        "complete": m.group(6) == "1",
    }


def tasks_for(bench, repo_root):
    specs = [
        ("narrow", "speedread", repo_root, "render_file", ["search", "-w", "render_file"]),
        ("narrow", "flask", os.path.join(bench, "flask"), "dispatch_request",
         ["search", "-w", "dispatch_request", "src/flask/app.py"]),
        ("narrow", "gin", os.path.join(bench, "gin"), "AbortWithStatusJSON",
         ["search", "-w", "AbortWithStatusJSON"]),
        ("narrow", "ripgrep", os.path.join(bench, "ripgrep"), "build_parallel",
         ["search", "-w", "build_parallel"]),
        ("wide", "flask", os.path.join(bench, "flask"), "url_for",
         ["search", "-w", "url_for"]),
        ("wide", "gin", os.path.join(bench, "gin"), "AbortWithStatus",
         ["search", "-w", "AbortWithStatus"]),
        ("wide", "ripgrep", os.path.join(bench, "ripgrep"), "ignore",
         ["search", "-w", "ignore", "crates/ignore"]),
        ("wide", "zod", os.path.join(bench, "zod"), "safeParse",
         ["search", "-w", "safeParse"]),
    ]
    out = []
    for family, name, root, label, args in specs:
        if not os.path.isdir(root):
            print(f"skip {name}: {root} missing", file=sys.stderr)
            continue
        out.append({"family": family, "repo": name, "root": root, "label": label, "args": args})
    return out


def evaluate_task(binary, task):
    root = task["root"]
    # Oracle: high budget so the symbol list is the scan, not the render cap.
    code, oracle, _ = run(
        binary, root, ["--max-budget", "200000", "--budget", "200000", *task["args"], "--output", "matches"]
    )
    if code != 0:
        raise SystemExit(f"oracle failed for {task['label']}: {code}")
    files = parse_matches(oracle)
    required = oracle_symbols(root, files, task["family"])
    modes = {}
    for mode in ("matches", "symbols", "auto"):
        code, out, err = run(binary, root, [*task["args"], "--output", mode])
        if code != 0:
            raise SystemExit(f"{mode} failed for {task['label']}: {err}")
        graded = answer(binary, root, out, required)
        graded["search_tokens"] = tok(out)
        graded["bytes"] = len(out.encode())
        modes[mode] = graded
    env = os.environ.copy()
    env["SPEEDREAD_AUTO_CENSUS"] = "1"
    env["SPEEDREAD_AUTO_N"] = "100000"
    env["SPEEDREAD_AUTO_K"] = "100"
    code, _, err = run(binary, root, [*task["args"], "--output", "auto"], env=env)
    census = load_census(err) if code == 0 else None
    return {
        "family": task["family"],
        "repo": task["repo"],
        "label": task["label"],
        "oracle_files": len(files),
        "oracle_truncated": "more files" in oracle or "stopped at" in oracle,
        "required_symbols": len(required),
        "modes": modes,
        "census": census,
    }


def sweep(rows):
    """Return every grid cell and the eligible winner, per the design note."""
    cells = []
    for n in NS:
        for k in KS:
            tokens = 0
            calls = 0
            success = 0
            inlined = 0
            veto = False
            for row in rows:
                c = row["census"]
                if c is not None and chooses_inline(c, n, k):
                    mode = "symbols"
                    inlined += 1
                else:
                    mode = "matches"
                g = row["modes"][mode]
                tokens += g["tokens"]
                calls += g["calls"]
                success += int(g["ok"])
                # A task both fixed modes fail cannot be saved by a threshold.
                # It stays in the token total and does not veto the pair.
                # A task one mode passes must still pass under the mode picked.
                either = row["modes"]["matches"]["ok"] or row["modes"]["symbols"]["ok"]
                if either and not g["ok"]:
                    veto = True
            cells.append({
                "n": n,
                "k": k,
                "tokens": tokens,
                "calls": calls,
                "success": success,
                "inlined": inlined,
                "matched": len(rows) - inlined,
                "eligible": inlined >= 1 and (len(rows) - inlined) >= 1 and not veto,
            })
    eligible = [c for c in cells if c["eligible"]]
    winner = None
    if eligible:
        best = min(c["tokens"] for c in eligible)
        tied = [c for c in eligible if c["tokens"] <= best * 1.02]
        tied.sort(key=lambda c: (c["n"], c["k"]))
        winner = tied[0]
        winner = {**winner, "tie_band": len(tied), "best_tokens": best}
    return cells, winner


def md_table(rows):
    lines = [
        "| Task | Symbols | matches tok / calls | symbols tok / calls | auto tok / calls | auto branch |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        c = r["census"] or {}
        branch = "n/a"
        if c:
            # The real auto output equals one of the two modes when the
            # implementation matches the census. Reported from bytes.
            if r["modes"]["auto"]["bytes"] == r["modes"]["symbols"]["bytes"] and r["modes"]["auto"]["tokens"] == r["modes"]["symbols"]["tokens"]:
                branch = "symbols" if r["modes"]["auto"]["tokens"] != r["modes"]["matches"]["tokens"] or r["modes"]["auto"]["bytes"] != r["modes"]["matches"]["bytes"] else "either"
            if r["modes"]["auto"]["bytes"] == r["modes"]["matches"]["bytes"]:
                branch = "matches" if branch != "either" else "either"
            if r["modes"]["auto"]["bytes"] == r["modes"]["symbols"]["bytes"] and r["modes"]["auto"]["bytes"] != r["modes"]["matches"]["bytes"]:
                branch = "symbols"
        def cell(mode):
            g = r["modes"][mode]
            mark = "ok" if g["ok"] else "FAIL"
            return f"{g['tokens']:,} / {g['calls']} {mark}"
        lines.append(
            f"| {r['family']} {r['repo']} `{r['label']}` | {c.get('distinct', '?')} | {cell('matches')} | {cell('symbols')} | {cell('auto')} | {branch} |"
        )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="target/eval-repos")
    ap.add_argument("--speedread", default="target/release/speedread")
    ap.add_argument("--json")
    ap.add_argument("--calibrate", action="store_true")
    a = ap.parse_args()
    binary = os.path.abspath(a.speedread)
    bench = os.path.abspath(a.bench)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    tasks = tasks_for(bench, repo_root)
    rows = []
    for t in tasks:
        print(f"task {t['family']} {t['repo']} {t['label']}", flush=True)
        rows.append(evaluate_task(binary, t))
        r = rows[-1]
        print(
            f"  symbols={r['required_symbols']} census={r['census']} "
            f"matches={r['modes']['matches']['tokens']}/{r['modes']['matches']['calls']} "
            f"symbols={r['modes']['symbols']['tokens']}/{r['modes']['symbols']['calls']} "
            f"auto={r['modes']['auto']['tokens']}/{r['modes']['auto']['calls']}",
            flush=True,
        )
    cells, winner = sweep(rows)
    print()
    print(md_table(rows))
    print()
    if a.calibrate:
        print("| N | K | tokens | calls | success | inlined | eligible |")
        print("|---:|---:|---:|---:|---:|---:|:---:|")
        for c in cells:
            mark = "yes" if c["eligible"] else ""
            print(f"| {c['n']} | {c['k']} | {c['tokens']:,} | {c['calls']} | {c['success']}/{len(rows)} | {c['inlined']} | {mark} |")
        print()
        if winner:
            print(
                f"winner N={winner['n']} K={winner['k']} tokens={winner['tokens']:,} "
                f"calls={winner['calls']} tie_band={winner['tie_band']} best={winner['best_tokens']:,}"
            )
        else:
            print("no eligible pair")
    if a.json:
        os.makedirs(os.path.dirname(a.json), exist_ok=True)
        json.dump({"rows": rows, "cells": cells, "winner": winner}, open(a.json, "w"), indent=1)
        print(f"wrote {a.json}")


if __name__ == "__main__":
    main()
