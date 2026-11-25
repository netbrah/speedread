#!/usr/bin/env python3
"""Wall time and peak RSS of search output modes on one large tree.

Five measured runs, modes interleaved (matches, symbols, auto), after one
unmeasured warmup of every query in every mode. Release binary. Warm page
cache: the warmup is what fills it, and it is not in the table.

Peak RSS is ru_maxrss from wait4 (KiB on Linux) reported in MiB.
A query that exits non-zero, or whose stdout lacks the expected heading,
is a failure and is not a latency sample.

  python3 evals/search_output_perf.py target/eval-repos/linux target/release/speedread
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time

MODES = ["matches", "symbols", "auto"]
ROUNDS = 5

# (name, args after the binary's --root, expected substring)
QUERIES = [
    ("vfs_read", ["search", "-w", "vfs_read"], "match"),
    ("kmalloc", ["search", "-w", "kmalloc"], "match"),
    ("TODO", ["search", "TODO"], "match"),
    ("absent", ["search", "zzz_not_there_zzz"], "No matches"),
]


def once(binary, root, args):
    t0 = time.perf_counter()
    p = subprocess.Popen(
        [binary, "--root", root, *args],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    out = p.stdout.read()
    _pid, status, ru = os.wait4(p.pid, 0)
    wall_ms = (time.perf_counter() - t0) * 1000
    ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    rss_mib = ru.ru_maxrss / 1024.0
    return wall_ms, rss_mib, ok, out


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    root, binary = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    print(f"warmup {root}", flush=True)
    for name, args, expect in QUERIES:
        for mode in MODES:
            wall, rss, ok, out = once(binary, root, [*args, "--output", mode])
            head = out.split(b"\n", 1)[0].decode(errors="replace")[:80]
            good = ok and expect.encode() in out
            print(f"  warmup {name} {mode}: {wall:.0f} ms {rss:.0f} MiB ok={good} | {head}", flush=True)
            if not good:
                sys.exit(f"warmup failed: {name} {mode}")
    samples = {q[0]: {m: [] for m in MODES} for q in QUERIES}
    failures = 0
    for rnd in range(ROUNDS):
        for name, args, expect in QUERIES:
            for mode in MODES:
                wall, rss, ok, out = once(binary, root, [*args, "--output", mode])
                good = ok and expect.encode() in out
                if not good:
                    failures += 1
                samples[name][mode].append((wall, rss, good, len(out)))
                print(f"  r{rnd} {name} {mode}: {wall:.0f} ms {rss:.1f} MiB {len(out)} B ok={good}", flush=True)
    print()
    print("| Query | Mode | median ms | min–max ms | median RSS MiB | min–max RSS | median bytes | failures |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for name, _, _ in QUERIES:
        for mode in MODES:
            rows = samples[name][mode]
            walls = [r[0] for r in rows]
            rsss = [r[1] for r in rows]
            byts = [r[3] for r in rows]
            fails = sum(not r[2] for r in rows)
            print(
                f"| {name} | {mode} | {statistics.median(walls):.0f} | {min(walls):.0f}–{max(walls):.0f} | "
                f"{statistics.median(rsss):.1f} | {min(rsss):.1f}–{max(rsss):.1f} | "
                f"{statistics.median(byts):.0f} | {fails} |"
            )
    if failures:
        sys.exit(f"{failures} failed samples")


if __name__ == "__main__":
    main()
