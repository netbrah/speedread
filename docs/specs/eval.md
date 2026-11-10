# Evaluation log

Machine: sandbox, 2 vCPU, 7 GiB RAM, Linux x86_64, rustc 1.99.1. Small-machine numbers;
see 00 for what this does and does not validate.

## Thresholds

- Wall time and peak RSS of the baseline query set: no more than +10% versus the previous
  shipped commit (median of 5 runs).
- Output bytes of unchanged commands: identical, unless the spec says otherwise.
- Each feature's own metric: stated in its spec.

## Results

(appended by each shipped slice; raw output in `evals/fork/`)

### Baseline — upstream 37abea2 (`evals/fork/baseline-37abea2.tsv`)

15 queries (9 on ripgrep @178cc02, 6 on zod @19c40d2), run from CWD `/` with `--root <repo>`,
median of 5: total wall 1455 ms, max peak RSS 65.0 MiB.

### T1 — path and scope contract (`evals/fork/t1-paths.tsv`)

| Check | Result |
|---|---|
| Output of the 15 unchanged queries | byte-identical (15/15 `out_sha` equal) |
| Total wall (median of 5) | 1455 → 1531 ms (+5.2%; within threshold, dominated by run-to-run noise on `rg-map` and `zod-search`) |
| Max peak RSS | 65.0 → 65.2 MiB |
| New tests | 12 integration (`tests/fork_paths.rs`) + 2 unit, all driving explicit CWDs |
| Walks for a request with no base | 0 (asserted through `SPEEDREAD_STATS=1`) |
| Model-facing text | +1 `base_dir` property on each of the 4 tool schemas (~25 tokens each); descriptions and instructions untouched |

Decision: ship, on by default (no behaviour change unless a target lies outside the primary
root, the server has no root, or `base_dir`/`--no-cwd-root` is used).

### T2 — bounded discovery and residency (`evals/fork/t2-bounds.tsv`)

| Check | Result |
|---|---|
| Output of the 15 unchanged queries | byte-identical (15/15 `out_sha` equal) |
| Total wall (median of 5) | 1569 ms versus 1535 ms for the upstream binary re-run in the same session (+2.2%) |
| Max peak RSS (one-shot CLI) | 65.0 → 67.2 MiB (+3.4%) |
| New tests | 8 integration (`tests/fork_bounds.rs`), 4 consecutive runs green |
| Explicit reads on a tree with 0 / 3,000 / 20,000 untracked files | 0 walks at every size, identical output |
| Walk stopped by entry limit, deadline, cancellation or candidate cap | answer ends with `incomplete:`; no absence claim |
| MCP `notifications/cancelled` | walk stops (`cancelled=1`, fewer than the 30,000 entries of the tree), next request answered in full |

Residency of a long-lived MCP server (`scripts/fork-rss.py`: 1,200 files, ~250 MiB, one symbol
read from each, twice; a deliberately hostile corpus whose outlines outweigh its source):

| Binary / setting | RSS after reads | RSS at end | First pass | Repeat pass |
|---|---|---|---|---|
| upstream 37abea2 | 1126 MiB | 1143 MiB | 28.1 s | 0.5 s |
| fork, default (512 MiB total) | 511 MiB | 524 MiB | 27.7 s | 27.4 s |
| fork, `--cache-mb 128` | 184 MiB | 205 MiB | 28.2 s | 28.1 s |
| fork, `--cache-mb 4096` | 1126 MiB | 1139 MiB | 27.5 s | 0.5 s |

Output bytes are identical in all four rows (181,380 per pass).

Reading: upstream's outline cache is bounded by count, so this corpus pins about 1.1 GiB. The
fork bounds it by bytes; the default halves resident memory, and pays for it only when the
working set exceeds the ceiling (the repeat pass re-parses). `--cache-mb 4096` reproduces
upstream behaviour exactly. This is a trade, not a free win, and it is recorded as such.

Decision: ship. Limits default to a backstop (1,000,000 files per walk, 30 s per request);
cache total defaults to 512 MiB. Not measured: macOS fast walker (not compilable here), and a
real monorepo — the corpus is synthetic.

### T3 — ancestor context (`evals/fork/t3-scopes.tsv`)

| Check | Result |
|---|---|
| Output of the 15 unchanged queries | byte-identical (15/15) |
| Total wall of the unchanged queries | 1531 ms (upstream re-run in the same session: 1535 ms) |
| New tests | 8 integration (`tests/fork_scopes.rs`): Rust, TypeScript, Python, C, TSX, depth cap, context merge, JSON, MCP schema |
| `scopes` versus `matches`, 12 searches, unlimited budget | +15% bytes (per query +1% … +53%), +9% wall |
| `scopes` versus `symbols` | 24% of the bytes |
| Model-facing text | one enum value and 11 words in the `output` property description |

Decision: ship as opt-in. Not the default: 5 of 12 queries exceed the 35% overhead bar.
