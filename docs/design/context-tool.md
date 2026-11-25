# `context`: one question, one budgeted bundle

Status: design and four-tool baseline. No `context` implementation in this revision.

The fifth tool answers a natural-language question about a repository. It chooses a mix of the existing `map`, `search`, `trace`, and `read` primitives, spends one token budget across that mix, and returns one bundle. The four primitives stay as they are: same behavior, same descriptions, same instructions string.

## Decision, written before the baseline run

Adopt `context` only if, on the fixed questions in [`evals/context_tasks.json`](../../evals/context_tasks.json):

- its strict pass rate is not below the four-tool policy (`quad`);
- its median result-token count is not higher than `quad` by more than `quad`'s own run-to-run range;
- its external call count is less than or equal to `quad`.

A median latency increase above 50%, or a peak RSS above 2×, on the Chromium questions is a negative performance result. It is not a win, and it is not by itself a reason to drop the tool if the three gates above hold. A gap smaller than the 5-run range is no measurable difference.

Noise floor: 5 fresh processes per question. Pass/fail and token counts are exact (deterministic output, tiktoken `o200k_base`). Latency and RSS move with the machine.

## What the baseline is

`quad` is a deterministic policy in [`evals/context_eval.py`](../../evals/context_eval.py). It is not a live model. This environment has no Copilot CLI and no model API, so success is Suite 1's criterion: every grader regex matches the tool stdout. Anchors check the pinned files before any timed run, so a wrong checkout cannot score.

The policy, frozen before the 5-run:

| Question shape | One call |
|---|---|
| "overview" / "layout" and no file path | `map --symbols` |
| callers, callees, implementations, or "who calls" | `trace` |
| a file path and a symbol, and the question asks for a body (`keys`, `how`, `what does`, `put in`) | `read path#symbol` |
| a file path and no symbol | `read` of that file |
| otherwise | one `search` |

A single class, struct, interface, function, or method name is searched as a definition pattern (`class\s+(?:\w+\s+)*Name`), not as every mention of the word. `output=symbols` when the question asks what, how, keys, or methods; `output=matches` otherwise. Filename extensions are not symbols (`go.mod` is not a search for `mod`). Each call gets `--budget 8000`, the CLI default.

Two earlier pilots were thrown out because the policy was mis-parsing filenames (`app.py` became symbol `py`, `context.go` became the trace target). Those runs are not the baseline. The policy text above is the one that was measured.

## Baseline numbers

Raw run: [`evals/results/context-baseline.json`](../../evals/results/context-baseline.json).

- Binary: `target/fast/speedread`, `cargo build --profile fast`, rustc 1.90.0. This is not the fat-LTO release binary the crate ships. No latency win will be claimed from this profile. The later comparison will rebuild both arms the same way; a `--release` remeasure is still owed before any latency claim.
- Machine: 4 cores, load average 0.05 before and after. A Chromium blob fetch was stopped for the run.
- N = 5 fresh processes per question, sequential, one arm only (nothing to interleave yet).
- Page cache was already warm from the pilots. Caches were not dropped. Run 1 is a fresh process on a warm cache, not a cold disk.
- Encoder: tiktoken `o200k_base`, same as Suite 1. Tokens are stdout only.
- Peak RSS: `wait4` `ru_maxrss` (kernel peak for that process). An earlier sampler that slept 2 ms recorded 388 KiB for a 3 ms read whose real peak was 13 MiB. That sampler is not used.
- Errors: 0 non-zero exits, 0 empty outputs. Every question's stdout was identical across the 5 runs.
- The graded facts were in the stdout (strict pass). The profile run below shows the Chromium walk actually visited the tree.

| Question | Pass | Calls | Result tokens median (min–max) | Latency median s (min–max) | Run 1 s | RSS median KiB |
|---|:---:|---:|---:|---:|---:|---:|
| flask-shell-keys | yes | 1 | 129 (129–129) | 0.007 (0.006–0.009) | 0.009 | 99780 |
| flask-dispatch-lines | yes | 1 | 2799 (2799–2799) | 0.008 (0.007–0.010) | 0.008 | 99780 |
| flask-overview | yes | 1 | 3027 (3027–3027) | 0.024 (0.023–0.031) | 0.031 | 99780 |
| gin-go-version | yes | 1 | 657 (657–657) | 0.002 (0.002–0.003) | 0.002 | 99780 |
| gin-abort-signature | yes | 1 | 240 (240–240) | 0.008 (0.008–0.008) | 0.008 | 99780 |
| gin-abort-callers | yes | 1 | 555 (555–555) | 0.040 (0.037–0.044) | 0.040 | 99780 |
| gin-abort-who-calls | yes | 1 | 555 (555–555) | 0.039 (0.038–0.041) | 0.039 | 99780 |
| gin-render-methods | yes | 1 | 116 (116–116) | 0.006 (0.005–0.007) | 0.006 | 99780 |
| speedread-map-budget | yes | 1 | 689 (689–689) | 0.010 (0.009–0.011) | 0.011 | 99780 |
| chromium-filepath | yes | 1 | 1474 (1474–1474) | 0.038 (0.037–0.040) | 0.038 | 99780 |
| chromium-basename | yes | 1 | 159 (159–159) | 0.005 (0.004–0.005) | 0.004 | 99780 |

Aggregate over the 11 questions, all of which passed:

- strict pass **11/11**
- external calls **1.00** mean (1 on every question)
- result tokens per answered question: **mean 945**, **median 555** (o200k)
- latency: median of the per-question medians **7.9 ms**; the slow questions are the walks (gin trace ~40 ms, Chromium class search 38 ms, flask map 24 ms)
- peak RSS: **99780 KiB** (97.4 MiB) on every question except one flask-shell run at 99652 KiB

### Why the numbers look like this

**Calls are already 1.** The recorded agent eval on code questions averaged 1.20 speedread calls ([`evals/results/agent-claude-sonnet-5/summary.md`](../../evals/results/agent-claude-sonnet-5/summary.md)). `quad` was written as one well-chosen primitive, and on this set that is always enough. `context` cannot reduce external calls below 1 here. A call-count win has to come from a question this policy would split, and this set has none. That is a property of the set, reported rather than patched after the fact.

**Tokens.** Mean 945 is pulled up by two questions. `flask-overview` is a symbol map of flask (3027). `flask-dispatch-lines` asks only for three line numbers, but the policy sees the word "methods" and returns symbol bodies (2799). A thrown-out pilot of the same question with `output=matches` was 349 tokens and also passed. The frozen policy's cost is 2799. `context` should not copy that over-read; the gate is "not higher than 2799 by more than the run range", and the run range on that question is 0.

**Latency limiter (Chromium class search).** A separate profile run, not one of the five, took 0.039 s and printed `walks=1 entries=5712 stopped=0 cancelled=0`. The search visited every file in the snapshot and did not hit the 10 s deadline or the walk cap. Halving 38 ms means not walking every file; there is no index that would allow that. `perf` is not installed, so this does not separate CPU from warm-cache disk. The 5-run range is 37–40 ms, so a later change inside that band is not a difference. `--version`, which exits in clap, is not this workload.

**RSS limiter.** `speedread --version` peaks at 12360 KiB. Every real command peaks at 99780 KiB, including a 2 ms read of `go.mod` and the 5712-file search. The high-water mark is process startup (engine and thread pool), and the walk does not raise it on this snapshot. Per-question RSS differences below that floor are invisible. A 2× regression would have to clear ~195 MiB.

**Not measured.** Live-model answer sentences, preamble re-send (the README's 18–21k tokens per turn), and fat-LTO latency. Multiplying calls by that preamble would not change this baseline, because every question is already one call. It will matter only if `context` and `quad` diverge on call count.

## Chromium snapshot

Not a depot_tools tree and not `third_party`. A full working-tree checkout of `github.com/chromium/chromium` at `8c02738014f004d4917273d70a6596094938f870` (depth 1, blobless) was still downloading blobs, and measuring a tree that was growing would not repeat. The timed tree is a copy of the sparse cones `base`, `net/base`, and `content/browser/renderer_host`: **5712 files, 72 MiB**, no `.git`. Questions and anchors are in the task file. The class-definition question is the scale case in this baseline: one walk of all 5712 files, 1474 result tokens, 38 ms median, strict pass (the definition line in `base/files/file_path.h` is in the output).

## Two shapes

### A. Pipeline (chosen)

Three pure steps, one owner each.

```rust
pub struct ContextRequest {
    pub question: String,
    pub path: Option<String>,   // file or directory scope
    pub budget: Option<usize>,
}

pub enum Intent { Orient, Relate, Read, Search }

pub struct Route {
    pub intent: Intent,
    pub direction: Option<Direction>, // trace::Direction
    pub depth: usize,
    pub targets: Vec<String>,         // read targets, or the trace target
    pub pattern: String,              // search pattern; empty if unused
    pub literal: bool,
    pub word: bool,
    pub output: Output,               // search::Output
    pub scope: Vec<String>,
}

pub enum Step {
    Map { path: Option<String>, symbols: bool, budget: usize },
    Search { pattern: String, paths: Vec<String>, literal: bool, word: bool, output: Output, budget: usize },
    Read { targets: Vec<String>, mode: Mode, budget: usize },
    Trace { target: String, direction: Direction, depth: usize, budget: usize },
}

pub struct Plan {
    pub intent: Intent,
    pub steps: Vec<Step>,
}

pub fn route(question: &str, path: Option<&str>) -> Route;
pub fn plan(route: &Route, budget: usize) -> Plan;
pub fn context(engine: &Engine, req: &ContextRequest) -> String;
```

`route` owns the decision and reads no files. `plan` owns the budget split and does no I/O. `context` owns execution: it calls `map::map`, `search::search`, `trace::trace`, and `read::read`, and nothing else walks the tree. A later step may take read targets parsed from an earlier search header (`[start-end] label` under `==> path`). That binding is a pure function of the search text. There is one planner.

### B. Open loop (rejected)

A loop reclassifies the growing transcript and picks the next primitive until the budget is gone. The next action then lives in both the classifier and the loop, so the same question has two ways to be answered, and a bad stop rule walks the tree again. Harder to golden-test. Rejected.

`search`'s `output=auto` prototype is not on `main`. `context` will pass `Output::Matches` or `Output::Symbols` explicitly and will not grow a second auto mode.

## Routing

Priority, first match. Scope from `path` replaces paths mentioned in the question.

1. **Orient.** The question says overview or layout, and names no file. `map` with `symbols=true`.
2. **Relate.** Callers, callees, implementations, references, or "who calls". One `trace`. Target is `Type.method` when the question has one, with `::` normalized to `.`, otherwise `#Name`. Direction from the same words. Depth 2 only for "two hop", "exactly one intermediate", or "depth 2"; otherwise 1.
3. **Read.** An explicit `path#Symbol`, `#Symbol`, or `path:line`, or a bare file path, or a file plus a symbol when the question asks for a body. One `read`, mode `auto`.
4. **Search.** Otherwise one `search`. A named class, struct, interface, function, or method uses a definition pattern; several names use a word-boundary alternation; one name is a literal word. `output=symbols` when the question asks what the body contains; `output=matches` when it asks which line or which file.

Chain only after a `matches` search, and only when the question asked for a body and named no file to read directly: parse up to three enclosing-symbol headers and `read` those with the remaining budget. If the search printed no headers, stop. Do not search again.

This router is not the Python policy. The eval compares them. Copying `quad`'s "methods" → symbol-body over-read on a line-number question would spend 2799 tokens where 349 also passed.

## Budget

The call budget is `engine.budget(requested)` (default 8000, clamped 200..`max_budget`), the same clamp as the other tools. One step gets all of it. A chain gives the search 20% (at least 200) and the read the rest. Shares are computed before either step runs; the read does not grow if the search underspends, and it does not shrink if the search overshoots its estimate. The final text is passed through `engine.enforce_budget` so the bundle stays within budget + 5%, the same slack `read` already uses. No blind cut: a step that does not fit is that primitive's own degraded view.

## Determinism

Same question, scope, budget, and tree produce the same bytes. No clock, no random choice, no hash iteration order in the plan. Identifier extraction is ASCII, stable, and ordered by the question, not by the filesystem. `map` and `search` already sort paths. The plan line is part of the output, so the model can see what was consulted:

```
context intent=search budget=8000
plan: search 8000
--- search 'class\s+(?:\w+\s+)*FilePath\b' ---
<primitive output>
```

A skipped chain says so: `plan: search 1600; read skipped: no symbol headers`.

## Failure modes

| Case | Behavior |
|---|---|
| Empty or whitespace question | `context: empty question`, no walk |
| Question longer than 4_000 bytes | `context: question too long`, no walk |
| No symbol, path, or orient/relate cue | `context: no symbol or path in the question`, no walk |
| Path or pattern rejected by the primitive | That primitive's existing error line, then stop. Do not invent a second target |
| Search with zero matches | Return that text. Do not read an unrelated file |
| Symbol read reports `no symbol` | Return that text. Do not guess a different name |
| Trace `?` or closure marks | Left as `trace` emitted them |
| Walk deadline or entry cap | `engine.note_incomplete` once on the bundle |
| Budget under 200 | Clamped, same as the other tools |
| `output=auto` | Not used |

## Tool surface

Name: `context`.

Description, the whole model-facing string. It lives in `src/context_tool.rs` as its own const. `src/server.rs` gains one `#[tool]` function and does not change `READ_DESC`, `SEARCH_DESC`, `MAP_DESC`, `TRACE_DESC`, or `INSTRUCTIONS`.

```
Use instead of grep or cat when you have a natural-language question and do not want to choose among map, search, trace, and read. Returns one context bundle for that question (a map, search hits, symbol source, or relationships) trimmed to the token budget (default 8000 tokens).
```

Arguments:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `question` | string | yes | The question. |
| `path` | string | no | File or directory scope. |
| `budget` | integer | no | Token budget for the whole bundle. Default 8000. |
| `base_dir` | string | no | Same absolute-directory override the other four tools take. |

CLI, mirroring the tool: `speedread context <question> [--path PATH]`. `--budget` stays the global flag.

## Tests

- `route` and `plan` are pure: intent, pattern, and budget shares for the question shapes above, including empty input, a filename that must not become a symbol, and a class-definition pattern.
- One fixture question against `tests/fixtures` returns the symbol body and stays within budget.
- Two calls with the same input return the same bytes.
- An MCP `tools/call` round trip, beside the existing four, in `tests/mcp.rs`.
- The existing four tools' tests stay green without edits to their behavior.

## Reproduce the baseline

```bash
cargo build --profile fast    # rustc 1.90+
python3 evals/context_eval.py \
  --bench /tmp/bench --self . \
  --speedread target/fast/speedread \
  --arm quad --runs 5 \
  --json evals/results/context-baseline.json
```

`<bench>` must contain `flask` at `49943b5a94413a530761350f04e896f76e8d620e`, `gin` at `0f09c3a9b4626d9fe9979cf4d0521d6ae32ab646`, and `chromium-subset` as the 5712-file snapshot of Chromium `8c02738014f004d4917273d70a6596094938f870` described above. The speedread question is anchored to the text of `DEFAULT_MAP_BUDGET`, not to whatever commit is checked out later. `tiktoken` is required.
