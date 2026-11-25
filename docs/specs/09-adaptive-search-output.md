# 09 — Adaptive search output

Experiment. `output=auto` is opt-in. The default stays `matches`. This note is the
decision record: the shape, the numbers, and what was measured before any default
changed. Raw rows are in [`evals/results/search-output-auto/`](../../evals/results/search-output-auto/).

## Question

Should `search` default to `output=symbols`?

`matches` (the default) prints hit lines under an enclosing-symbol header
(`[20-80] fn parse_header(...)`). The next step is `read path#parse_header`.
`symbols` inlines each enclosing body whose span is at most 200 lines
(`end - start <= 200` in `src/search.rs`). That saves the follow-up read and
spends the token budget, so fewer files fit in the same answer.

## Shapes

Both shapes keep one rendering of a hit. Neither invents a third layout.

**A. Whole-answer switch (shipped).** After the scan, one function prices the
symbols-mode answer and collapses `auto` to either `matches` or `symbols`. The
bytes the caller sees are that mode's bytes, including the `==> path @etag`
header. Text search and JSON search call the same function.

**B. Mixed answer (rejected).** Inline the cheapest symbols until K% of the
budget, then emit match lines for the rest. One response would have two layouts.
A caller would have to know which symbols were inlined. The budget interaction
would be a second packer beside the one `render_file` already has. Rejected:
each task has one way to do it, and a caller of `auto` should need to know nothing
that a caller of `matches` or `symbols` does not already know.

A per-file switch was the same idea one level down. N is a property of the query.
Inlining the first file of a wide query is how `symbols` spends the budget.

## Types

```rust
pub struct AutoCensus {
    pub distinct: usize,          // enclosing symbols, including ones too long to inline
    pub inlineable: usize,        // span <= SYMBOL_INLINE_MAX_SPAN (200, pre-existing)
    pub body_tokens: usize,       // numbered body lines only
    pub response_tokens: usize,   // preamble + headers + bodies + hit lines
    pub per_file_overflow: bool,  // symbols rendering dropped a later hit in a file
    pub complete: bool,           // every hit file was priced
}

pub struct AutoPolicy { pub max_symbols: usize, pub budget_percent: usize }

pub fn choose_auto(c: &AutoCensus, budget_tokens: usize, p: AutoPolicy) -> AutoChoice
```

`AutoChoice::Inline` renders with the existing `Output::Symbols` path.
`AutoChoice::Matches` renders with the existing `Output::Matches` path.
`resolve_auto` in `src/search.rs` builds the census and calls `choose_auto`.
`search_request` and `json::search` both call `resolve_auto`. `render_file` does
not know about `auto`.

Inline when all of these hold, and matches otherwise:

1. `complete` — the census saw every hit file. Gates only grow as files are added
   (more symbols, more tokens, overflow sticks), so stopping at the first failure
   is the same choice as pricing every file. `SPEEDREAD_AUTO_CENSUS=1` does not
   stop; it prints the totals for the sweep. That variable and `SPEEDREAD_AUTO_N`
   / `SPEEDREAD_AUTO_K` are the sweep's probe, not a supported flag.
2. `!per_file_overflow` — the existing per-file cap `(max_bytes / 2).max(2000)`
   would have dropped a later hit. Not a new threshold.
3. `inlineable > 0` — a span over 200 is not inlined by `symbols` either, so it
   counts in `distinct` and not in `body_tokens`.
4. `distinct <= N`.
5. `body_tokens * 100 <= budget_tokens * K`.
6. `response_tokens <= budget_tokens`.

Gate 6 is the budget contract, not a tuned K. `enforce_budget` cuts a finished
answer at budget + 5%. Inlining a response that only fits by that slack clips the
tail of a body, which is the failure this mode exists to avoid. The check uses
`Engine::estimate` (the content-aware counter in `src/tokens.rs`, scaled by the
engine's tokenizer). It does not use the client's tokenizer. Default scale is 1
(legacy). Claude's scale is 1.4, which makes the gate stricter, not looser.

## Etag

The decision does not read or write an etag. Both branches print `Source::etag`
on the file header, the 64-bit xxh3 of the bytes. `remember` still runs for every
file the chosen rendering shows, so a later `read path@etag` diffs against the
same snapshot it would have after `matches` or `symbols`. Inlining a body is not
a substitute for that diff.

## What is not tuned

| Number | Where it comes from |
|---|---|
| 200-line span | Already the `symbols` inline cap (`end - start <= 200`). `auto` counts only bodies `symbols` would emit. |
| Per-file cap `(max_bytes / 2).max(2000)` | Already how `search_request` stops a single file. Overflow means that renderer dropped a hit. |
| Budget default 8000, clamp 200..=max (10000) | Existing `Engine::budget`. |
| Estimate, not tiktoken | Existing budget counter. The eval below also reports o200k, which is what `evals/tool_eval.py` counts. The gate does not. |

N and K are the tuned pair. They are `AUTO_MAX_SYMBOLS` and `AUTO_BUDGET_PERCENT`.

## How N and K are chosen (written before the sweep)

Grid: N ∈ {1, 2, 3, 4, 6, 8, 12}, K ∈ {25, 40, 50, 60, 75, 100}.

For each task, run `matches` and `symbols` once at the default budget and one
`auto` census (`SPEEDREAD_AUTO_CENSUS=1`). A pair inlines on that task when
`choose_auto` says so on that census at budget 8000. The cost of a pair is the
measured cost of the mode it picks (search output plus the follow-up reads that
mode still needs). It is not a re-run per cell.

Tasks, two families, on flask, gin, ripgrep, zod (shallow clones) and this repo:

- **Narrow.** Information goal: every enclosing symbol of a hit. A symbol with
  span ≤ 200 must show every non-empty source line; a longer one must show every
  hit line (what `symbols` itself shows).
- **Wide.** Information goal: the same, restricted to the three files with the
  most matches. This is the "read the calling code" step in `evals/tool_eval.py`.

Follow-ups use only what that mode's own output contains. A symbol header gives
`read path:startLine`. A file named only in the trailer gives `read path`. The
oracle that lists the required symbols is a matches search at `--max-budget
200000 --budget 200000`, and its tokens are not counted (the agent does not run it).

A pair is eligible when it inlines at least one task and stays on matches for at
least one task, and every task succeeds. Among eligible pairs, minimize total
o200k tokens of search plus follow-ups. There is no sampling noise; this script
is deterministic. Pairs within 2% of the best token total tie. Ties break to
smaller N, then smaller K: a wrong inline spends the budget, a wrong match costs
one extra read.

The first sweep vetoed every pair, because two tasks failed under both fixed
modes and the rule required every task to succeed. A threshold cannot pass a
task both modes fail. The rule used below is the original one with that case
carved out: a task both modes fail stays in the token total and does not veto;
a task one mode passes must still pass under the mode the pair picks. The
follow-up was also wrong on the first run (`path:line` re-resolves the enclosing
symbol, which is not always the header's range; a heading inside a Go doc
comment is the case that showed it). The follow-up is the header's `path:start-end`.
The file-header parser treated `1 match` as not a header (`matches?` is `matche`
or `matches`). Both are fixed in `evals/search_output_eval.py`. The tables are
from the run after those fixes, on the binary that ships N=3 and K=25.

The flip decision is separate and is below. This sweep only picks the pair the
opt-in mode uses.

## How the default decision is made (written before the run)

Recommend flipping the default to `auto` only if all of these hold on the task
set above, using the shipped pair, and the giant-tree run does not contradict it:

1. `auto` succeeds on every task `matches` succeeds on and every task `symbols`
   succeeds on.
2. `auto`'s total o200k tokens are at least 10% below the worse of the two fixed
   modes, and not more than 5% above the better one.
3. On the giant tree, median search latency and peak RSS of `auto` are within
   +10% of `matches` (the current default). The gap has to clear the run-to-run
   range. Five runs, modes interleaved, release binary, warm page cache. One
   extra profiled run names the limiter and is not one of the five.

Otherwise keep `matches` as the default. `auto` stays opt-in either way in this
change; the recommendation is whether a follow-up should flip it.

## Giant-tree measurement

Claim under test: on a kernel-scale tree, `output=auto` is within +10% of
`output=matches` on median wall time and peak RSS.

Corpus: Linux at a depth-1 clone (the fork's own large-tree corpus; a full
Chromium checkout is the other one in `docs/specs/eval.md` and is not practical
here when the clone does not finish). Queries that must actually match, plus one
that must not: a rare symbol, a common allocator, a comment marker, and a string
that occurs nowhere. Each query checked for exit 0 and for the expected heading
(`match` or `No matches`).

Machine, run count, range, and the limiter are recorded with the numbers.
`scripts/fork-big-eval.py` is the existing harness; `evals/search_output_perf.py`
is this comparison. `ru_maxrss` from `wait4` is the child's peak, in KiB on Linux,
reported in MiB.

## Results

Checkouts: speedread at this commit; flask `49943b5`, gin `0f09c3a`, ripgrep
`3e85c6c`, zod `d40c329` (all depth-1). Tokens are o200k_base. The gate inside
the binary is the content-aware estimate at the default scale (1.0), not o200k.
One run; the script is deterministic. Raw rows:
[`evals/results/search-output-auto/eval.json`](../../evals/results/search-output-auto/eval.json).

`auto` on this binary inlines flask `dispatch_request` and gin
`AbortWithStatusJSON`, and is byte-for-token the matches answer on the other
six. That is what N=3, K=25 does. N from 3 through 8, at every K in the grid,
makes those same two choices. N=12 and K≥60 also inlines ripgrep
`build_parallel` (10 symbols, 4,055 body tokens, 51% of the 8,000 budget).

| Task | Distinct | matches tokens / calls | symbols tokens / calls | auto tokens / calls |
|---|---:|---:|---:|---:|
| narrow speedread `render_file` | 8 | 5,436 / 8 ok | 5,108 / 1 ok | 5,436 / 8 matches |
| narrow flask `dispatch_request` | 3 | 1,388 / 4 ok | 1,273 / 1 ok | 1,273 / 1 symbols |
| narrow gin `AbortWithStatusJSON` | 3 | 954 / 4 ok | 804 / 1 ok | 804 / 1 symbols |
| narrow ripgrep `build_parallel` | 10 | 3,594 / 11 ok | 3,125 / 1 ok | 3,594 / 11 matches |
| wide flask `url_for` | 48 | 7,709 / 18 ok | 10,624 / 3 fail | 7,709 / 18 matches |
| wide gin `AbortWithStatus` | 16 | 3,043 / 10 ok | 4,523 / 1 ok | 3,043 / 10 matches |
| wide ripgrep `ignore` | 149 | 25,595 / 47 fail | 17,925 / 5 fail | 25,595 / 47 matches |
| wide zod `safeParse` | 764 | 6,088 / 1 fail | 6,064 / 1 fail | 6,088 / 1 matches |
| **Total** | | **53,807 / 103** | **49,446 / 14** | **53,542 / 97** |

Success is 6/8 for matches and for auto, 5/8 for symbols. symbols fails
`url_for`: it spends the budget on the first bodies and two follow-ups do not
recover the top-three files' ranges. matches and auto do, in 18 calls.

The two tasks every mode fails are not a vote between modes. zod's busiest file
(`string.test.ts`, 183 matches) never appears before the answer hits the token
cut (`truncated: dense content`), so there is no follow-up target. ripgrep
`ignore` names 149 symbols; dozens of range reads still miss some whose headers
were already cut and whose whole-file read comes back as a skeleton. Those two
tasks are 31,683 of matches' 53,807 tokens. They are the cost of not finishing.

On the five tasks every mode passes, the totals are matches 14,415 / 37 calls,
symbols 14,833 / 5, auto 14,150 / 31. auto is 265 tokens under matches (1.8%)
and 6 calls under matches. All 265 tokens and all 6 calls are the two queries
with exactly 3 symbols. `build_parallel` would add another 469 tokens and 10
calls if it were inlined (symbols 3,125 / 1 versus matches 3,594 / 11). That is
the N=12, K=60 cell: 53,073 tokens, 87 calls, also 6/8 success. It is 0.9% under
the N=3 cells. The pre-registered 2% tie band treats that as a tie, and the tie
breaks to smaller N, then smaller K. **Shipped pair: N=3, K=25.**

K is not identified. At N=3 every K from 25 to 100 makes the same choices. No
task had 3 or fewer symbols whose bodies sat between 25% and 100% of the 8,000
budget (the two inlined bodies are 1,615 and 870 tokens). K=25 is the
conservative end of that tie, not a measured optimum. The response-fits-budget
gate still refuses an answer `enforce_budget` would clip, whatever K is.

`render_file` has 8 symbols and 6,394 body tokens (under the 8,000 budget) but
`per_file_overflow` is set, so auto stays on matches even at N=8, K=100. symbols
mode still held every required line in one call. The overflow gate is stricter
than this grader: it refuses to inline if any later hit in a file was dropped,
including a hit the task did not need.

### Does the default flip?

No.

1. auto's success equals matches and beats symbols (the `url_for` failure).
2. The 10% token bar is not met. Against matches, the current default, auto is
   0.5% down on the full set and 1.8% down on the tasks everyone passes. symbols'
   lower full-set total (49,446) is not a win: it includes the failed `url_for`
   answer and fewer follow-ups on the two tasks nobody finishes.
3. On the kernel, auto is not a latency or RSS regression past 10%. See below.

The gain from flipping the default is those two 3-symbol queries. Everywhere
else in this set, auto already is matches.

### Kernel latency and RSS

Not Chromium. A depth-1 Linux checkout at `4dce97c6c` (97,284 files searched on
the empty query) is the tree this fork already uses for the large-tree gate, and
it is what was measured. Machine: 4 cores, load 0.80 at the start of the timed
runs, rustc 1.100.0, release binary, warm page cache. The first warmup of
`vfs_read` matches was 10.0 s and is not in the table; that was the cold cache.
Five later runs, modes interleaved. Failures: 0. Every timed answer contained
the expected heading (`match`, or `No matches` for the absent pattern). Raw log:
[`evals/results/search-output-auto/perf.txt`](../../evals/results/search-output-auto/perf.txt).

| Query | matches ms (min–max) | symbols ms | auto ms | matches RSS MiB | auto RSS MiB |
|---|---:|---:|---:|---:|---:|
| `vfs_read` (198 matches, 32 files) | 283 (281–287) | 281 (281–294) | 294 (292–295) | 66.7 (62.4–76.5) | 62.7 (58.6–68.9) |
| `kmalloc` (3,739 matches, 1,900 files) | 271 (269–276) | 265 (262–269) | 277 (271–279) | 67.0 (65.2–75.3) | 63.2 (53.4–67.4) |
| `TODO` (stopped at 5,000) | 172 (152–192) | 181 (161–199) | 195 (159–198) | 51.5 (45.4–55.7) | 49.6 (49.3–55.5) |
| absent pattern | 255 (253–258) | 255 (253–261) | 255 (251–275) | 45.3 (43.2–47.3) | 45.2 (41.2–47.1) |

`vfs_read` auto is the same 17,082 bytes as matches on all five runs (a later
comparison of the two files was identical). The census decided not to inline,
then the renderer did the same work. The median is 11 ms higher and the ranges
do not overlap, so the difference is real. 11/283 is 4%, inside the 10% bar.

`kmalloc` ranges overlap (matches 269–276, auto 271–279). `TODO` ranges overlap,
and the matches spread (40 ms) is larger than the median gap (23 ms): no
measurable difference. The absent pattern is the same 255 ms. RSS for auto is
not higher on any query.

**Limiter, from runs that are not in the table.** `wait4` on one extra pass:
`vfs_read` matches 296 ms wall and 1,094 ms CPU (482 user, 613 system);
auto 298 ms wall and 1,061 ms CPU (436 user, 625 system). The absent scan was
265 ms wall and 1,027 ms CPU. CPU/wall is about 3.7 on 4 cores, and system time
exceeds user time. The search is a parallel walk of the tree. A mode change
cannot double it: the walk that finds nothing is already ~255 of the ~280 ms,
and auto's extra CPU on `vfs_read` is inside the run-to-run spread of that
profile pair. `strace` and `perf` are not installed here, and installing them
needed root; the counters are `wait4`, not a syscall profile. That is enough to
name the limiter and to reject "the census made it twice as slow." It is not a
per-function profile.

`TODO` byte counts move between 21,201 and 21,244 across runs. The walk stops at
5,000 matches, and which file it stops in is not identical every run. Every run
still exited 0 and reported the stop. That jitter is not an auto-versus-matches
gap: matches itself moves between 21,224 and 21,228.

## Recommendation

Keep `matches` as the default. Ship `output=auto` as opt-in with N=3 and K=25.

`symbols` as a default is the wrong way around for a wide query: on `url_for` it
costs more tokens than matches and still fails the top-three-file goal. `auto`
avoids that, and it is cheaper than matches on the two queries where three small
bodies fit in a quarter of the budget. That saving is 265 o200k tokens and 6
calls on this set, about 2% of the tokens on the tasks everyone can finish, and
under 1% if the unfinished tasks are left in the total. It is not 10%.

Do not read K=25 as a tuned optimum. The grid could not tell 25 from 100 at N=3.
The next measurement that would move K is a query with at most 3 enclosing
symbols whose bodies are between 2,000 and 8,000 estimated tokens. The next
measurement that would move N is whether inlining `build_parallel`-shaped
queries (about 10 symbols, about half the budget, and symbols mode already
cheaper) is worth giving up the conservative tie-break. On this set it was
worth 469 tokens, inside the band that was written down as a tie.
