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

### T4 — relationship kinds (`evals/fork/t4-trace.tsv`)

| Check | Result |
|---|---|
| Output of the 15 eval queries | 14 byte-identical; `zod trace #safeParse` differs only by 10 `λ` marks, the `25 inside closures` count and one legend (10,768 → 10,752 bytes: the marks displaced one trailing group within the budget) |
| Total wall (median of 5) | 1598 ms (T2: 1568 ms; upstream re-run earlier in the session: 1535 ms) |
| Max peak RSS | 65.3 MiB (upstream 65.0 MiB) |
| New tests | 7 integration (`tests/fork_trace.rs`): Rust, TypeScript, Go, Python; unchanged output without marks; constants; refs; fields, types and getters |
| 120 sampled functions/methods (60 ripgrep, 60 zod), `trace` at a 400,000-token budget, fork versus upstream | 0 site lines lost; 77 answers identical; 5 value sites added, 5 of 5 correct by inspection; 395 `λ` marks |
| First implementation, same sample | 504 value sites, mostly field reads and types — rejected, rules tightened (spec 05 "As shipped") |

Decision: ship as default. Caveat: at a fixed budget, marks and value sites take bytes, so a
truncated answer can show slightly fewer trailing sites than upstream (the truncation note
still counts them).

### T5 — structural search (`evals/fork/t5-ast.tsv`, `evals/fork/t5-ast-shapes.tsv`)

| Check | Result |
|---|---|
| Output of the 15 eval queries | byte-identical to T4 (15/15); versus upstream 14/15, the one T4 difference |
| Total wall of the unchanged queries (median of 5) | 1517 ms (T4: 1598 ms; upstream: 1455–1535 ms) |
| Max peak RSS of the unchanged queries | 65.4 MiB (upstream 65.0 MiB) |
| Binary (`--profile fast`) | 43,764,224 → 44,026,112 bytes over upstream (+0.6%); three new crates |
| New tests | 10 integration (`tests/fork_ast.rs`) + 5 unit: Rust, TypeScript, TSX, Python, Go; every grammar compiles a call pattern; prefilter on/off equal; macros; nested matches; errors; MCP |
| Model-facing text | two properties on `search` (`ast`, `lang`), 51 words; descriptions and instructions untouched |
| Deadline | `--deadline-ms 20` on zod: partial answer ending in `incomplete: stopped after 20 ms …` |

Three shapes against the regex an agent would write (`scripts/fork-ast-eval.py`, lines of
tracked files; a regex-only line is classified by what it is):

| Shape | Regex lines | Structural lines | Regex-only: comment / string | Regex-only: other | Structural-only |
|---|---|---|---|---|---|
| `$V.unwrap()` vs `\.unwrap\(\)`, ripgrep | 1,043 | 1,020 | 12 / 0 | 11, all inside `macro_rules!` rules (reported in the answer as unmatched) | 0 |
| `expect($A).toBe($B)` vs `expect\(.*\)\.toBe\(`, zod | 2,192 | 2,160 | 37 / 0 | 34, all `expect(value, message).toBe(…)` — a different shape, which the pattern rightly excludes | 39, all calls spanning lines that a line regex cannot see |
| `console.log($$$A)` vs `console\.log\(`, zod | 419 | 394 | 18 / 7 | 0 | 0 |

Wall time, median of 7 (ms):

| Shape | Structural | Literal search, default budget | Literal search grouping every hit (`output=symbols`, unlimited) |
|---|---|---|---|
| `$V.unwrap()` | 194 | 76 (2.6×) | 170 (1.1×) |
| `expect($A).toBe($B)` | 207 | 18 (11.5×) | 150 (1.4×) |
| `console.log($$$A)` | 84 | 39 (2.2×) | 76 (1.1×) |

Peak RSS of a structural search: 51–56 MiB (literal: 29–40 MiB).

Reading:

- **The wall-time gate in spec 04 ("within 2× of the equivalent literal search") is not met:**
  2.2× to 11.5×. The cause is structural: a literal search counts hits without parsing and
  parses only the files it has budget to show; a structural search has to parse every file
  that contains the pattern's words to know whether it matches. Against a literal search made
  to parse the same files the cost is 1.1–1.4×. In absolute terms the worst case here is
  207 ms on two cores for 279 files. The gate was written without that distinction; it is
  recorded here as missed rather than rewritten.
- **Precision over a regex is small on common shapes:** 1–6% of regex lines were comments or
  strings. The gains that matter are elsewhere: calls spanning lines (39 on zod), argument
  shape (`expect($A)` versus `expect($A, $B)`), captures, and patterns no regex expresses
  (`$A == $A`).
- **The first implementation was worse than a regex on Rust** — it missed 119 of 1,043 lines,
  everything inside macro arguments and chains ending on a later line. That was found by this
  measurement, fixed, and is now covered by tests; what remains unmatched (`macro_rules!`
  rules) is counted in the answer.

Decision: ship as opt-in (`ast=true`). Nothing changes unless it is asked for. Open question
for the default tool description: whether models reach for it at the right times is not
measured — that needs the agent evals upstream runs (`evals/`), which need API keys.

### Summary of decisions

| Slice | Decision | Changes default output? |
|---|---|---|
| 01 Path and scope contract | shipped, default | only for targets outside the primary root or a rootless server |
| 02 Bounded discovery and residency | shipped, default | no; long-lived servers hold less memory and may re-parse |
| 03 Ancestor context (`output=scopes`) | shipped, opt-in | no |
| 04 Structural search (`ast=true`) | shipped, opt-in; wall-time gate missed as written | no |
| 05 Relationship kinds (`λ`, `→`) | shipped, default | `trace` only: marks, value sites, legend |
| 06 Backlog (entity correspondence, import-aware lookup, Git input layer) | specified, not started | — |
