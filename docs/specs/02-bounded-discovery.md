# 02 — Bounded discovery and residency

Status: shipped (see "As shipped"; where it differs from the design below, "As shipped" wins). Invariants: 2, 4, 5.

## Problem

Walks have no entry or time bound; two caps are silent (5,000 definition candidates,
250,000 listed files); caches are fixed at 512 MiB + 256 MiB per process. With many agents
each holding an MCP server, and checkouts containing large untracked trees, the worst case is
neither bounded per request nor visible in the answer.

## Contract

| Stage | Control | Default | Override |
|---|---|---|---|
| Discovery | entries visited per request | 200,000 | `--max-walk`, `SPEEDREAD_MAX_WALK` |
| Discovery | wall-clock per request | 15 s | `--deadline-ms`, `SPEEDREAD_DEADLINE_MS` |
| Discovery | definition candidates | 5,000 (existing) | — (now reported) |
| Reading | per-file cap for search/parse | existing (`MAX_IN_MEMORY`, `MAX_PARSE`, `MAX_GROUP_BYTES`) | — |
| Retention | source cache bytes | 512 MiB (unchanged until measured) | `--cache-mb`, `SPEEDREAD_CACHE_MB` |
| Retention | snapshot cache bytes | half of source cache | derived |
| Output | token budget | existing | existing |

Rules:

1. Explicit file targets (`path`, `path:A-B`, `path:LINE`, `path#Symbol`, `path@etag`) never
   start a directory walk when the path exists.
2. A walk that hits a limit stops, and the response carries one line:
   `incomplete: stopped after N files (limit) — narrow with path/glob`. "No matches" /
   "not found" wording is replaced by "none in the N files examined (incomplete)".
3. Limits are per request and shared by every walk the request performs (one `Limits` value:
   remaining entries, deadline, cancel flag).
4. Cache caps are configuration, never correctness: any cap ≥ 1 MiB yields the same answers.
5. Untracked, non-ignored files stay visible by default (agents must see files they just
   created). Bounding is by limits and ignore rules, not by hiding untracked files.
   A tracked-only input is backlog (06).

## Design

- `walk::Limits { budget: AtomicUsize, deadline: Option<Instant>, cancel: AtomicBool, hit: AtomicU8 }`
  carried in `WalkOpts.limits: Option<Arc<Limits>>`; both walkers call `limits.admit()` per
  entry. `hit` records which limit fired (`Entries`, `Deadline`, `Cancelled`).
- `Engine::limits()` creates a request's `Limits` from config; `search::scan`,
  `find_definitions`, `map`, `trace` take/propagate it and render rule 2.
- `walk::STATS` (process-wide atomics: walks started, entries visited) for tests and
  `speedread debug --stats`.
- `Config.cache_bytes`; `Engine::new` sizes `sources` and `snapshots` from it.
- Cancellation (MCP `notifications/cancelled` → `Limits.cancel`) is wired where rmcp exposes
  the request's cancellation token; until then the deadline bounds orphaned work.

## Tests

- Untracked-tree growth: a git repo with fixed tracked sources plus 0 / 5k / 50k untracked
  files. Explicit `read path`, `read path:LINE`, `read path#Sym` perform **zero** walks at
  every size (rule 1) and return identical output.
- `--max-walk 100` over 5k files: `search` output contains `incomplete:` and never the plain
  "No matches" sentence; with a sufficient limit the same search has no `incomplete:` line.
- `--deadline-ms 1` on a large tree → `incomplete:` with the deadline reason.
- `#Symbol` lookup that stops on the limit reports incompleteness instead of "not found".
- `--cache-mb 1` vs default: identical outputs for the eval query set.

## Measurement (ship gate)

`scripts/fork-eval.py`: peak RSS and wall time for the query set, at cache caps
{default, 64 MiB}, before deciding whether the fork lowers the default.

## As shipped

| Stage | Control | Default | Override |
|---|---|---|---|
| Discovery | files delivered **per walk** | 1,000,000 | `--max-walk`, `SPEEDREAD_MAX_WALK` (0: unlimited) |
| Discovery, parsing | wall-clock **per request** | 10 s | `--deadline-ms`, `SPEEDREAD_DEADLINE_MS` (0: unlimited) |
| Discovery | client cancellation per request | on (MCP `notifications/cancelled`, or the handler being dropped) | — |
| Discovery | files remembered as mentioning a name | 20,000 per name set | — (reported) |
| Discovery | definition candidates parsed | 5,000, counted after the definition-line prefilter | — (reported) |
| Reading | longest line a text search buffers | 4 MiB | — (reported) |
| Retention | cached content in total | 256 MiB: 1/2 file contents, 1/4 outlines, 1/4 `path@etag` snapshots | `--cache-mb`, `SPEEDREAD_CACHE_MB` |

Differences from the design above, and why:

- **The entry limit is per walk, not per request.** A depth-2 trace walks the same tree
  more than once; a shared counter made the limit depend on the tool rather than on the tree.
  The deadline and cancellation are per request (one `Limits` per `Engine::scoped` view).
- **The entry default is 1,000,000 files**, not 200,000. Upstream's own caps (400,000 listed
  files for `map`) are above 200,000, and a default must not turn a complete answer on a
  large monorepo into an incomplete one.
- **The outline cache is now bounded by bytes.** Upstream bounds it by count (8,192 outlines
  of any size). On the hostile corpus of `scripts/fork-rss.py` that held roughly 800 MiB of outlines
  for ~250 MiB of source. `--cache-mb` is therefore one total for all three caches.
- **Rule 4, precisely:** the cache ceiling never changes `search`, `read`, `trace` or `map`
  answers (files and outlines are content-addressed and re-read on a miss). The one visible
  effect: a `path@etag` diff whose snapshot was evicted comes back as a full read, as it
  already did upstream when the 256 MiB snapshot cache evicted it.
- **Wording.** The line is `incomplete: <reason>; results may be missing. Narrow with a path
  or glob.` Absence claims change to "Nothing matched … in the N files searched … before
  discovery stopped" (`search`), "not defined in the files examined … before discovery
  stopped" (`read #Name`, `trace`), "None found in the files examined before the trace
  stopped" (`trace` with a definition but no time left for its call sites). JSON Lines output
  keeps stdout clean and prints the line on stderr.
- **Cancellation is wired** (rmcp 3.4.1 hands tool handlers the request's token).

### Defaults (revised after measuring on the Linux kernel and Chromium)

The first shipped defaults were 30 s and 512 MiB. Both were chosen as backstops without a
large tree to measure on. `evals/fork/big-*.txt` (see eval.md, "Large trees") is that
measurement, on 2 vCPUs:

- **Deadline 10 s.** With a warm page cache a whole-tree text search takes 0.4–1.6 s on the
  kernel (97k files) and 2.7–3.3 s on Chromium (474k files), which is what ripgrep itself
  takes there (2.8 s). A depth-1 trace takes 2–3.5 s and 5–7 s. Nothing ordinary needs 30 s;
  what does run that long is a depth-2 trace of a name that thousands of files use, and its
  first 10 s already fill the output budget. With a cold page cache the same search of
  Chromium is I/O-bound (ripgrep: 56 s on this disk) and is cut at 10 s with the note; the
  retry reads from cache. Set `--deadline-ms 30000` to wait instead.
- **The deadline bounds the request, not only its walks.** It was checked per walked file,
  but most of the time of a definition lookup or a trace is spent parsing afterwards
  (tree-sitter C: about 4 MB/s per core). `trace mutex_lock --depth 2` on the kernel ran
  26 s under a 10 s deadline and then answered "No call sites found (it may be unused …)"
  because the call-site walk was refused. The parse loops of `find_definitions`, `defs_named`
  and `classify_file` now stop at the deadline too, and running out of time outranks a count
  limit in the note (`Limits::mark`).
- **Cache 256 MiB.** On the hostile corpus the default resident size follows the ceiling
  (512 → 511 MiB, 256 → 238 MiB); on the kernel and Chromium sessions the working set
  of hundreds of symbol reads stays under it (73 and 125 MiB resident). `--cache-mb 4096` was
  never a default: it is the setting that reproduces upstream's unbounded outline cache for
  comparison.

### Memory a request freed goes back

The cache ceiling bounds what the server keeps. It did not bound what the server held:
measured over a session on Chromium, resident memory sat at 220–260 MiB whether the ceiling
was 64, 256 or 512 MiB, upstream and fork alike. Two causes, both outside the cache:

- **Tree-sitter did not use the program's allocator.** Its C library calls the system
  `malloc`; the Rust `#[global_allocator]` (mimalloc) covers Rust code only. glibc keeps
  freed memory with the thread that used it, so every parse left its high-water mark behind.
  One `read` of a symbol in a 7 MiB source file left the server at 340 MiB for good with
  `--cache-mb 1`. `main` now gives tree-sitter mimalloc (`tree_sitter::set_allocator`, before
  anything is parsed).
- **Nothing asked for freed pages back.** After each MCP tool call the handler thread and
  every pool thread run `mi_collect(true)` (`server::AFTER_REQUEST`, set by the binary).

Same request after both: 33 MiB resident (test
`memory_a_request_freed_is_returned_to_the_system`, Linux only: it reads `/proc`). Sessions:
Chromium 250 → 125 MiB (upstream 222), kernel 73 MiB (upstream 125); request times within
3% of upstream on symbol reads, and parsing itself is about 10% faster with one allocator.
The CLI is unaffected except for that speed: it exits.

### One walk per question

A trace asked the same question of the tree three times: which files mention the name, to
find its definition (`find_definitions`), its namesakes (`defs_named`) and its call sites
(`scan`). On Chromium each walk is about 3 s. `search::mentions` now answers it once per
request and name set and keeps the list with the request (`walk::Mentions`, on `Limits`,
at most 20,000 files and 16 name sets; gone when the request ends, so there is nothing to
keep fresh):

- `trace NAME`: 3 walks → 1; `--depth 2`: 5 → 2 (test `a_trace_reads_the_tree_once_per_level`).
- A lookup of definitions alone (`read #Name`, callee trees) still reads only files in a
  known language; a trace that will also read call sites lists every searchable file.
- When the list is complete, call sites are searched from it nearest-first (non-test, then
  path proximity to the definition) in whole chunks of 256 files until the 20,000-line limit.
  A limited scan therefore keeps the files the answer would show anyway, and the same ones on
  every run; the walk-based scan kept whichever files it reached first, so the same request
  could return different sites.
- The 5,000-candidate limit now counts files left after the definition-line prefilter, not
  files that merely mention the name: 5,200 mentions with no definition is a complete "not
  found", not an incomplete one. The prefilter no longer treats a statement call
  (`name(args);`) as a definition-looking line; in C that kept every caller as a candidate.

### Long lines

A text search buffers whole lines. One 200 MiB line (a bundle, a data file) made a single
`search` peak at 1 GiB and left an MCP server at 1.0–1.3 GiB resident, upstream and fork
alike. The searcher's line buffer is now limited to 4 MiB (`MAX_LINE_BYTES`); a file with a
longer line is searched up to that line and the answer says `not fully searched: N files have
a line longer than 4 MiB and were read only up to it, e.g. <path>`. Matched lines are kept as
a 4 KiB window around the match. After: 28 MiB peak, 20–23 MiB resident
(`scripts/fork-longline-rss.py`). Chromium has three such files.

Definition lookup must not lose a definition behind such a line, so the first version counted
every file with one as mentioning every name. On Chromium that made each symbol lookup parse
two 6–7 MiB one-line JSON files that have nothing to do with the name (+60 MiB peak per
lookup, found by comparing against upstream). `search::file_has` now matches such a file
whole when it is small enough to parse (at most 8 MiB, read once) and only says "maybe" for
larger ones, which are never parsed anyway.

Known gaps:

- The macOS fast walker (`getattrlistbulk`) is not compiled or tested here. Limits are checked
  when it delivers a batch, so entry limit, deadline and cancellation all work, but a walk
  whose glob filters out every file has no delivery and runs to the end. The portable walker
  (Linux, and macOS with `SPEEDREAD_PORTABLE_WALK`) checks every entry.
- A working set larger than the ceiling is re-parsed on every access (LRU). That is the trade
  `--cache-mb` controls; see eval.md for the hostile corpus at each setting.
- Content after a line longer than 4 MiB is not searched (disclosed per request).
- A depth-2 trace on a Chromium-sized tree does not finish in 10 s on 2 cores: the second
  level looks up every caller's name, and common ones are defined in thousands of files.
  It returns the first level and what it reached of the second, with the note.
