# Precise trace

Optional exact references, overrides, and call edges behind `trace`. The
syntactic trace stays the default and the fallback. Nothing in this design
requires an index or a language server to be installed.

The measurement harness is [`evals/trace_precision.py`](../../evals/trace_precision.py).
Numbers below are from that script; the raw run is
[`evals/results/trace-precision-baseline.json`](../../evals/results/trace-precision-baseline.json).

## Decision the baseline is allowed to change

A precise layer is worth building when reference-line or call-site precision
against a checked oracle is below 0.90, or when call-site recall on queries
that were not display-capped and not search-truncated is below 0.80. It is not
a reason to change the default path. The default stays syntactic unless a
later precise run is within 2× the warm syntactic p50 and falls back when no
index exists. A latency gap smaller than the run-to-run range is not a
difference.

## What was measured, before any precise code

Two oracles, both line-granular (syntactic `trace` does not emit columns; a
line ±1 is counted as a near-miss and is not a hit):

- A closed hand label,
  [`evals/fixtures/trace_oracle_truth.json`](../../evals/fixtures/trace_oracle_truth.json),
  over the crate in [`evals/fixtures/trace_oracle`](../../evals/fixtures/trace_oracle).
  The label file sits outside the crate so it is not itself a hit.
- rust-analyzer 1.100 (`textDocument/references` with the declaration excluded,
  `callHierarchy` incoming/outgoing `fromRanges`, `textDocument/implementation`)
  on that crate and on ripgrep `3e85c6c148fdce38a99ceeb9d2780dc7b03b4a4f`.
The hand label is how the oracle was checked. In the committed run,
rust-analyzer matched the hand-labeled references it was able to answer
(3 queries, 3/3 lines), every direct callee edge (3/3), and both `Speak`
impls. One references request (`alpha`) returned LSP `content modified` and
was not scored. A fixture-only rerun of the same binary, same oracle, scored
those references 7/7. rust-analyzer also reported `let f = alpha` as an
incoming call. The hand label does not. Incoming-call recall of the hand's
call sites was 1; precision against that stricter label was 0.80 because of
that one binding.

Quality runs use `--budget 200000 --max-budget 200000 --deadline-ms 0`, so a
truncated answer is not counted as a missed edge. ripgrep has 105 Rust files,
under the 800-file scan cap. Latency and token cost use the agent default
(trace's 4000-token budget, 10s deadline). Percentiles are the inclusive
nearest rank, `sorted[ceil(p/100·n) − 1]`. Token counts are
`speedread::tokens::estimate`. The Python port was checked against a verbatim
`rustc` of that function on three strings before any number was kept.

### Hand label, syntactic trace

| Kind | Precision | Recall | tp | fp | fn |
|---|---:|---:|---:|---:|---:|
| references | 0.875 | 1.000 | 7 | 1 | 0 |
| call sites | 0.800 | 1.000 | 4 | 1 | 0 |
| value uses | 1.000 | 1.000 | 1 | 0 | 0 |
| callee edges | 0.667 | 0.667 | 2 | 1 | 1 |
| impls (range overlap) | 1.000 | 1.000 | 2 | 0 | 0 |

The reference false positive and the call-site false positive are the same
line: `b.push(2)` on `Bag`, reported for `Buf::push` and marked `?` (receiver
unknown). The callee miss is `other::helper(2)` on the same line as `helper(1)`;
both arrows point at `lib::helper`. Recall of the lines that do exist is
complete. Near-misses: 0.

That is the gap a precise layer can close: same simple name, two definitions,
syntax cannot see the type.

### ripgrep `3e85c6c`, syntactic trace against rust-analyzer

12 symbols sampled from 1,817 functions and methods in 60 `src` files
(seed 1), stratified into rare, middling, and common names, plus 4 traits.
rust-analyzer cache priming finished in 6.4 s. Full budget, no deadline.
No query was display-capped (`hidden sites = 0`). Three callee queries were
search-truncated.

| Kind | Site precision | Site recall | tp | fp | fn |
|---|---:|---:|---:|---:|---:|
| references | 0.035 | 0.500 | 8 | 218 | 8 |
| call sites | 0.047 | 0.438 | 7 | 142 | 9 |
| callee edges | 0.364 | 0.125 | 4 | 7 | 28 |
| impls | 1.000 | 1.000 | 13 | 0 | 0 |

The micro precision is low because common names dominate the site count, and
that is the number an agent sees. Checked against the source, not only the
totals:

- `quit_early` and `sink_break_context` (one definition each) matched
  rust-analyzer line for line.
- `Display::fmt` for `HyperlinkFormatError` (`crates/printer/src/hyperlink/mod.rs:314`,
  one of 23 `fmt` definitions) has no references according to rust-analyzer.
  Syntactic trace printed 39 reference lines, in other crates' `fmt` methods
  (`human.rs`, `process.rs`, …). Same pattern for `kind` (68 lines, oracle 0)
  and `default` (49 lines, oracle 1).
- `find_candidate_line` is a one-line deref forwarder. The name on the next
  line is the inner method, which rust-analyzer does not count as a reference
  of the forwarder. Syntactic trace does.
- `class` misses the `class('a','a')` lines inside `syntax!` macro arguments
  (oracle lines 1376–1382) and also prints `class` from `strip.rs`.
- Trait impls (`Matcher`, `ParallelVisitor`, `ParallelVisitorBuilder`,
  `SinkError`) matched 13/13. On this sample the syntactic impl walk is
  already exact. The loss is references and calls.

Callee recall 0.125 is mostly unresolved calls (prediction empty, oracle
non-empty), including inside tests, plus five ambiguous arrows. Three of the
twelve callee queries stopped early (`not examined` / the search cap).

### Latency, RSS, tokens

Release binary, page cache already warm, load about 1.4 on 4 cores during
the ripgrep panel (the indexer had just exited). One warmup discarded, then
15 interleaved runs. Within a query the range is a few milliseconds, smaller
than the gap between a unique name and `fmt`. `perf` is not installed, so
the limiter was not profiled. Process startup (`speedread --version`) is
1.6 ms p50, against 14–60 ms for a trace, so startup is not the trace.
Warm MCP is the same server answering again.

| Query (ripgrep, default 4000-token budget) | CLI p50 | CLI p95 | CLI range | RSS p50 | tokens p50 | MCP p50 | MCP p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| callers, 1 definition | 14.2 ms | 15.8 ms | 13.5–15.8 | 23.8 MiB | 99 | 7.5 ms | 7.8 ms |
| refs, 1 definition | 14.2 ms | 15.0 ms | 13.6–15.0 | 23.8 MiB | 87 | 7.7 ms | 8.8 ms |
| callers, `fmt` (23 definitions) | 43.3 ms | 45.2 ms | 40.9–45.2 | 46.9 MiB | 453 | 20.0 ms | 22.1 ms |
| refs, `fmt` | 43.6 ms | 49.0 ms | 40.9–49.0 | 46.9 MiB | 1860 | 20.2 ms | 21.2 ms |
| callees, `overrides` | 27.4 ms | 29.4 ms | 25.6–29.4 | 34.4 MiB | 154 | 8.7 ms | 10.2 ms |
| callers depth 2, `fmt` | 59.5 ms | 61.2 ms | 55.9–61.2 | 46.7 MiB | 1826 | 34.0 ms | 38.3 ms |

Full-budget quality answers (the precision pass, not what an agent is sent)
had median estimate 405 tokens for references and 395 for callers across the
12 symbols. The agent-facing cost is the default-budget column above: a
common name's reference list is ~1,860 tokens, still inside 4,000.

### Chromium subset

Sparse checkout of `github.com/chromium/chromium` at
`8c02738014f004d4917273d70a6596094938f870`, directories `base`, `net`, and
`content` only. 27,028 files, 11,593 C/C++, 245,439,430 bytes. A full
Chromium tree needs depot_tools and a multi-gigabyte `gclient` sync; these
are the directories the repo's own large-tree eval already uses for `trace`
(`docs/specs/eval.md`). Not the whole browser. No C++ oracle: there is no
`compile_commands.json` here, so this block is latency, RSS, and tokens of
the syntactic trace only.

Default budget and deadline, n=7, load 1.4 rising to 2.8. Errors: 0.
`incomplete` means the answer itself said the scan stopped (`not examined`,
`incomplete:`, `stopped after`, or `truncated:`).

| Query | p50 | p95 | range | RSS p50 (max) | tokens p50 | incomplete | warm MCP p50 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `trace PostTask` | 388 ms | 407 ms | 377–407 | 126 MiB (134) | 3647 | 7/7 | 279 ms |
| `trace DidCommitNavigation` | 472 ms | 498 ms | 437–498 | 117 MiB (121) | 681 | 0/7 | 266 ms |
| `trace PostTask --depth 2` | 2217 ms | 2371 ms | 2202–2371 | 249 MiB (259) | 3407 | 7/7 | 543 ms |

`PostTask` stops because the default budget fills (~3,600 of 4,000 tokens),
not because the 10 s deadline fires. Depth 2 is the same, at 2.2 s and a
higher RSS. The MCP server's high-water mark across that panel was 308 MiB.
A precise fallback that walked this tree looking for an index would be a
regression; discovery in this design is a fixed list of paths.

Git freshness, timed once on this machine while otherwise idle: `git
diff-index --quiet HEAD` was 19 ms on ripgrep and 614 ms on the Chromium
subset. Both trees were clean.

## Two shapes

**Shape A. One resolver in-process.** `trace` grows an optional precision
mode. A small module owns discovery, staleness, and the timeout. A SCIP file
and a language-server session are two backends behind one trait. Both return
the same occurrence and edge types. The existing renderer prints them, and
the summary line names the layer. Callers (CLI, MCP) pass one enum.

**Shape B. A helper process per request.** Shell out to `rust-analyzer scip`
or an LSP just-in-time, parse its text, and paste it into the answer.

Shape A is the one to build. The precise state (the loaded index, the child
process) has one owner, the request path has one policy, and the CLI and the
server do not each grow a process supervisor. Shape B makes startup part of
every answer. On the fixture, rust-analyzer's initialize alone is cheap, but
cache priming is the actual index: 0.4 s on the tiny crate when its target
directory was already warm, 2.1 s when it was not, and 6.4 s on ripgrep. Paying that inside a request that already has a 10 s
deadline, and paying it again on the next CLI invocation, is the failure mode
Shape B cannot avoid.

## Types, signatures, module boundaries

```rust
pub enum Precision {
    Syntactic, // default. Output stays byte-identical to today.
    Auto,      // try precise, fall back
}

pub struct TraceRequest {
    pub target: String,
    pub direction: Direction,
    pub depth: usize,
    pub budget: Option<usize>,
    pub precision: Precision,
}

pub enum Layer {
    Syntactic,
    Scip,
    Lsp { command: String },
}

pub struct LayerNote {
    pub layer: Layer,
    pub fallback: Option<String>, // set when syntactic ran after a failed attempt
}

pub struct SymRef {
    pub path: PathBuf,
    pub line0: u32,       // definition line, 0-based
    pub name: String,
}

pub enum Role {
    Definition,
    Reference,
    Call,
    Import,
}

pub struct Occurrence {
    pub path: PathBuf,
    pub line0: u32,
    pub role: Role,
}

pub struct CallEdge {
    pub call_path: PathBuf,
    pub call_line0: u32,
    pub callee_path: PathBuf,
    pub callee_start0: u32,
    pub callee_end0: u32,
}

pub enum PreciseError {
    Missing,
    Stale(String),
    Timeout,
    Failed(String),
}

pub trait PreciseBackend {
    fn references(&mut self, sym: &SymRef) -> Result<Vec<Occurrence>, PreciseError>;
    fn implementations(&mut self, sym: &SymRef) -> Result<Vec<Occurrence>, PreciseError>;
    fn incoming(&mut self, sym: &SymRef) -> Result<Vec<CallEdge>, PreciseError>;
    fn outgoing(&mut self, sym: &SymRef) -> Result<Vec<CallEdge>, PreciseError>;
}
```

Modules:

- `precise/mod.rs` owns the policy. It decides whether to try, which backend,
  and the `LayerNote`. It does not render and it does not walk the tree.
- `precise/scip.rs` owns the loaded index and its fingerprint (path, mtime,
  size). Lookup tables are built once per process.
- `precise/lsp.rs` owns at most one child process per workspace and the
  JSON-RPC buffer. The MCP server is long-lived, so the child can be too.
  The CLI starts one only for that process.
- `trace.rs` calls the policy only when `precision != Syntactic`. Rendering
  stays in `trace.rs`, so the answer shape does not fork.

`Engine` does not gain a language server. The server struct holds an optional
`PreciseSession` next to the engine, and the CLI builds one for the command.
That keeps the read/search/map path from linking the session into its hot
state.

## SCIP versus LSP

| | SCIP index | LSP session |
|---|---|---|
| References | occurrence with a symbol and a role bit (definition, import, read, write) | `textDocument/references` |
| Overrides | `SymbolInformation.relationships.is_implementation` | `textDocument/implementation` |
| Call edges | not in the SCIP spec. Classify each reference occurrence with the existing tree-sitter call check. The symbol is exact; only call-versus-mention is syntactic | `callHierarchy/incomingCalls` and `outgoingCalls` |
| When it is ready | after one file load | after the server has primed. 0.4–2.1 s on the fixture crate, 6.4 s on ripgrep, this machine |
| Staleness | the file is a snapshot | the server is live if it watches the workspace; a server we attach to and do not drive can be stale |
| Failure | missing, undecodable, symbol absent, too big to load in the budget | timeout, crash, still indexing, binary missing |
| Memory | the index stays resident | the child's RSS is outside our process. Our client holds the documents it has opened |

Call edges are the reason both exist. A SCIP-only answer can be exact about
*which* symbol a mention is, which is the `Buf::push` / `Bag::push` bug, and
still use tree-sitter to decide that the mention is a call. An LSP answer can
name the callee of `other::helper(2)` the way rust-analyzer did and the
syntactic trace did not. Auto mode prefers a fresh SCIP index for references
and impls, and uses LSP for call hierarchy only when a server answers inside
the timeout. One answer uses one layer, so the summary line is unambiguous.
Mixing "these refs are SCIP, these calls are LSP" in one reply is a second
mode and is not in this design.

Language coverage follows whoever produced the artifact. rust-analyzer emits
SCIP and speaks the three LSP methods. clangd does the same for C and C++
when `compile_commands.json` exists. This design does not ship an indexer and
does not generate `compile_commands.json`.

## Discovery

No walk of the tree. On a 27k-file checkout a search for `index.scip` is
itself a trace-sized request.

In order:

1. `SPEEDREAD_SCIP`, if set: a file, or a directory whose `index.scip` is used.
2. `<workspace root>/index.scip`
3. `<workspace root>/.scip/index.scip`

LSP, only when precision is `auto` or a forced precise mode, and only if no
fresh SCIP index was used:

1. `SPEEDREAD_LSP`, a command line (split on whitespace, no shell).
2. Else `Cargo.toml` at the root and `rust-analyzer` on `PATH`.
3. Else `compile_commands.json` at the root and `clangd` on `PATH`.

The child is spawned with its working directory at the workspace root and
stdio as JSON-RPC. A missing binary is `PreciseError::Missing`, not a crash.

## Staleness

A stale index is not served as precise.

- If the workspace is a git checkout: run `git diff-index --quiet HEAD` with
  a 700 ms cap (the Chromium subset took 614 ms; ripgrep took 19 ms). The
  index is fresh when that command exits 0 and the index mtime is at or after
  the committer time of `HEAD`. A dirty tree, a non-zero exit, or a timeout
  is stale.
- If it is not a git checkout: the target file and the root manifest
  (`Cargo.toml`, `go.mod`, `compile_commands.json`, `package.json`) must not
  be newer than the index. When the index lists at most 2,000 documents, stat
  those paths inside the same budget. When it lists more and git cannot
  answer, freshness is unknown, which is treated as stale.

The blind spot of the git check is an index generated from a dirty tree that
was later cleaned without updating the file's mtime. Unknown freshness falls
back rather than guessing.

## Timeouts and fallback

The precise attempt has its own budget, default 1500 ms, overridable with
`SPEEDREAD_PRECISE_TIMEOUT_MS` and `--precise-timeout-ms`. It is not added on
top of the request deadline; whatever remains of the 10 s deadline is an
upper bound too.

Inside that budget, in order: discover, freshness, load (SCIP) or one RPC
(LSP). A server that has not finished cache priming fails this request with
`Timeout` even if the child is left running for a later request in the same
process. The CLI usually will not see a warm rust-analyzer. That is
acceptable: the CLI answer still comes back, from syntax, with the reason.

Fallback always produces the syntactic answer. Reasons that must be visible
in the summary, one clause, no stack trace:

- `precise unavailable: no index or language server`
- `precise unavailable: index stale (<why>)`
- `precise unavailable: timed out after <ms> ms`
- `precise unavailable: <short io/decode error>`

`Precision::Syntactic` does not look for an index and does not spawn anything.
That is the default for the CLI and for an omitted MCP parameter.

## Output compatibility

Default `trace` output stays byte-identical. The summary already ends with
`· syntactic: ...`. That line is unchanged when precision is off.

When a precise backend answers, the same headers, file groups, `[start-end]`
ranges, and `line<TAB>text` sites are used. The summary clause becomes
`· scip` or `· lsp (<command name>)` in place of the syntactic clause, and
states the one thing that backend actually decided (exact symbol, or exact
call hierarchy). The 6-site-per-group cap, the budget, and `… N more` stay,
so token cost does not jump just because the layer did.

When precise was requested and syntax ran instead, the syntactic clause stays
and gains `precise unavailable: ...` before it. Tests that snapshot today's
default output must not pass `--precision`.

## server.rs

Agreed with the tool-definition work, and binding for the implementation:

- Do not edit `READ_DESC`, `SEARCH_DESC`, `MAP_DESC`, `TRACE_DESC`, or
  `INSTRUCTIONS` in place.
- The new parameter's schema text lives in its own const, used only by the
  `precision` property on `TraceArgs`.
- `TRACE_DESC` stays as it is. The parameter description is the place that
  says when to set it. If a later measurement says the description itself
  must mention the parameter, append one sentence and list that sentence in
  the PR. Do not rewrite the paragraph.
- Description style for that property: when to pick this over grep or a
  syntactic trace, what comes back, and that the token budget still applies.
  Terse, no examples.

Proposed property text, and nothing else in the tool description:

> Set only when a SCIP index or language server is already available and
> same-named symbols must be kept apart. Returns the same trace, within the
> same token budget, tagged with the layer that answered. Omit it for the
> syntactic trace.

The parameter is optional, string, `auto` or `syntactic`. Omitted means
syntactic. No new required field.

## What will be tested

- Default trace output on the existing trace fixtures stays byte-identical.
- A hand-built SCIP document (one symbol, two occurrences, one impl
  relationship) answers references and impls, and a missing file falls back
  with the stale/missing clause.
- A stub LSP script on stdio answers `initialize` and `textDocument/references`
  and is killed on drop.
- The fixture crate above, with a real `index.scip` from `rust-analyzer scip`
  when that command finishes inside a few minutes. If generating the index
  fails or exceeds memory, that negative result is recorded and the stub
  index still covers the decoder.
- `evals/trace_precision.py` grows a `--precision auto` pass over the same
  symbols. Same oracles, same latency panel, alternating syntactic and
  precise so a busy machine hits both.

## Risks

- Call hierarchy is the weak half of SCIP. Shipping SCIP as "precise calls"
  would over-claim. The summary has to say when the call-versus-mention bit
  is still syntactic.
- A warm language server inside the MCP process holds memory the syntactic
  server does not. The design caps that by not starting it unless precision
  was requested, and by the 1500 ms give-up. RSS of the child is not our
  `ru_maxrss`; the results table has to say which process was measured.
- rust-analyzer's incoming calls include some value bindings. Scoring a
  precise LSP against that oracle without the fixture's hand label would
  hide that. The harness keeps both comparisons.
- On the Chromium subset there will be no SCIP index and no
  `compile_commands.json`. The precise path's job there is to notice that
  quickly and fall back. A slow discovery walk would be a regression; the
  discovery list above is fixed paths so it cannot be.
