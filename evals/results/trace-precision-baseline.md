# Trace precision baseline

Syntactic `trace` only. The precise layer is not in this binary.

A precise layer is worth building if micro precision of reference lines or of
call-site lines on the repo oracle is below 0.90, or if site recall of call
edges on queries with no display cap and no search truncation is below 0.80.
Do not change the default path unless a later precise run is within 2× the
warm syntactic p50 and falls back when no index exists. A latency gap smaller
than the run-to-run range is not a difference.

Binary: `/agent/repos/speedread/target/release/speedread` (release, speedread 0.2.0-netbrah.3).
Machine: 4 cores, load `0.15 0.10 0.10 1/302 15077`, {'MemTotal': '16387328 kB', 'MemAvailable': '4702284 kB'}.
Estimator: python estimate() matched rustc of the tokens.rs formula on 3 samples.
Parser: parser matched the fork_trace only_called shape and a callee arrow.

## Fixture (hand labels)

| Comparison | Kind | Precision | Recall | tp | fp | fn | near-miss |
|---|---|---:|---:|---:|---:|---:|---:|
| speedread_vs_hand | refs | 0.875 | 1.000 | 7 | 1 | 0 | 0 |
| speedread_vs_hand | callers | 0.800 | 1.000 | 4 | 1 | 0 | 0 |
| speedread_vs_hand | value | 1.000 | 1.000 | 1 | 0 | 0 | 0 |
| speedread_vs_hand | callees | 0.667 | 0.667 | 2 | 1 | 1 | 0 |
| speedread_vs_hand | impls | 1.000 | 1.000 | 2 | 0 | 0 | 0 |
| ra_vs_hand | refs | 1.000 | 1.000 | 3 | 0 | 0 | 0 |
| ra_vs_hand | callers | 0.800 | 1.000 | 4 | 1 | 0 | 0 |
| ra_vs_hand | callees | 1.000 | 1.000 | 3 | 0 | 0 | 0 |
| ra_vs_hand | impls | 1.000 | 1.000 | 2 | 0 | 0 | 0 |

rust-analyzer on the fixture: quiescent=True in 0.44s, encoding utf-16.

## ripgrep `3e85c6c148fdce38a99ceeb9d2780dc7b03b4a4f`

rust-analyzer quiescent=True in 6.39s. Symbols sampled: 12 (seed 1).

Quality is the full-budget resolver (`--budget 200000 --deadline-ms 0`). Site recall counts printed lines only; function recall counts a gold line that falls inside a printed enclosing range, so the 6-site display cap does not hide it.

| Kind | Site precision | Site recall | Function precision | Function recall | tp/fp/fn sites | hidden sites | truncated queries |
|---|---:|---:|---:|---:|---:|---:|---:|
| refs | 0.035 | 0.500 | 0.041 | 0.500 | 8/218/8 | 0 | 0 |
| callers | 0.047 | 0.438 | 0.059 | 0.500 | 7/142/9 | 0 | 0 |
| callees | 0.364 | 0.125 | — | — | 4/7/28 | 0 | 3 |
| impls | 1.000 | 1.000 | — | — | 13/0/0 | 0 | 0 |

Latency is the default agent budget (trace default 4000 tokens, deadline 10s), page cache already warm from the quality pass, load at start `1.40 0.39 0.19 3/315 16558` and end `1.37 0.40 0.20 1/303 18237`. One warmup panel is discarded. Percentile is nearest-rank. CLI `--version` p50 is the process-startup floor.

| Query | CLI p50 ms | CLI p95 ms | CLI range ms | RSS p50 MiB | tokens p50 | MCP warm p50 ms | MCP warm p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| `callers d1 basename_regex_parent_component (1 defs)` | 14.2 | 15.8 | 13.5–15.8 | 23.8 | 99 | 7.5 | 7.8 |
| `refs d1 basename_regex_parent_component (1 defs)` | 14.2 | 15.0 | 13.6–15.0 | 23.8 | 87 | 7.7 | 8.8 |
| `callers d1 fmt (23 defs)` | 43.3 | 45.2 | 40.9–45.2 | 46.9 | 453 | 20.0 | 22.1 |
| `refs d1 fmt (23 defs)` | 43.6 | 49.0 | 40.9–49.0 | 46.9 | 1860 | 20.2 | 21.2 |
| `callees d1 overrides (3 defs)` | 27.4 | 29.4 | 25.6–29.4 | 34.4 | 154 | 8.7 | 10.2 |
| `callers d2 fmt (23 defs)` | 59.5 | 61.2 | 55.9–61.2 | 46.7 | 1826 | 34.0 | 38.3 |
| `speedread --version` | 1.6 | 2.5 |  |  |  |  |  |

Limiter probe (one unreported run):

```
perf not available. one run wall 14.8 ms, rss 23.8 MiB, status 0. Limiter not identified from a profile.
```

## Scale: chromium base+net+content

Full Chromium needs depot_tools/gclient and a multi-gigabyte checkout. This is a sparse checkout of the github.com/chromium/chromium commit's base, net and content trees: the directories the repo's own large-tree eval already uses for trace. Not the whole browser.

Files 27028, C/C++ 11593, bytes 245439430, commit `8c02738014f004d4917273d70a6596094938f870`. Default deadline and budget. n=7. Load `1.37 0.40 0.20 1/303 18238` → `2.84 0.86 0.36 1/303 19016`.

| Query | p50 ms | p95 ms | range ms | RSS p50 MiB | tokens p50 | incomplete |
|---|---:|---:|---:|---:|---:|---:|
| `trace PostTask` | 388 | 407 | 377–407 | 126 | 3647 | 7/7 |
| `trace DidCommitNavigation` | 472 | 498 | 437–498 | 117 | 681 | 0/7 |
| `trace PostTask --depth 2` | 2217 | 2371 | 2202–2371 | 249 | 3407 | 7/7 |

## Reproduce

```bash
evals/trace_precision.py --speedread /agent/repos/speedread/target/release/speedread --rust-analyzer /usr/local/rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin/rust-analyzer --repo /tmp/eval-repos/ripgrep --repo-name ripgrep --scale-repo /tmp/eval-repos/chromium --scale-name chromium base+net+content --runs 15 --scale-runs 7 --symbols 12 --seed 1 --out evals/results/trace-precision-baseline.json
```
