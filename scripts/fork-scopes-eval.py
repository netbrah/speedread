#!/usr/bin/env python3
"""Ship-gate metric for spec 03: what ancestor context costs and what it replaces.

usage: scripts/fork-scopes-eval.py [BINARY]

For a fixed set of searches on the pinned reference repositories (see fork-eval.py), prints
the bytes returned by output=matches, output=scopes and output=symbols with an unlimited
budget, and the median wall time of matches and scopes.
"""
import os, statistics, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS = os.path.join(ROOT, "target", "eval-repos")
QUERIES = [
    ("ripgrep", "is_match"), ("ripgrep", "Searcher"), ("ripgrep", r"\.unwrap\(\)"),
    ("ripgrep", "line_number"), ("ripgrep", "return Err"), ("ripgrep", "binary_detection"),
    ("zod", "addIssue"), ("zod", "safeParse"), ("zod", "throw new Error"),
    ("zod", "_def"), ("zod", "ctx.common"), ("zod", "instanceof Promise"),
]


def run(binary, repo, pattern, output):
    cmd = [binary, "--root", os.path.join(REPOS, repo), "--budget", "400000", "--max-budget", "400000",
           "search", pattern, "--output", output]
    walls, data = [], b""
    for _ in range(5):
        t = time.perf_counter()
        data = subprocess.run(cmd, cwd="/", capture_output=True, check=True).stdout
        walls.append((time.perf_counter() - t) * 1000)
    return len(data), statistics.median(walls), data


def main():
    binary = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "target", "fast", "speedread"))
    tot = {"matches": 0, "scopes": 0, "symbols": 0}
    wall = {"matches": 0.0, "scopes": 0.0}
    print("repo\tpattern\tmatches_bytes\tscopes_bytes\tsymbols_bytes\tscopes_overhead\tmatches_ms\tscopes_ms")
    for repo, pat in QUERIES:
        m, mt, _ = run(binary, repo, pat, "matches")
        s, st, _ = run(binary, repo, pat, "scopes")
        y, _, _ = run(binary, repo, pat, "symbols")
        tot["matches"] += m; tot["scopes"] += s; tot["symbols"] += y
        wall["matches"] += mt; wall["scopes"] += st
        print(f"{repo}\t{pat}\t{m}\t{s}\t{y}\t{(s - m) / m:+.0%}\t{mt:.0f}\t{st:.0f}")
    m, s, y = tot["matches"], tot["scopes"], tot["symbols"]
    print(f"total\t\t{m}\t{s}\t{y}\t{(s - m) / m:+.0%}\t{wall['matches']:.0f}\t{wall['scopes']:.0f}")
    print(f"scopes is {s / y:.0%} of symbols bytes")


if __name__ == "__main__":
    main()
