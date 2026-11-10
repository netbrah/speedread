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
