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
| Discovery | wall-clock **per request** | 30 s | `--deadline-ms`, `SPEEDREAD_DEADLINE_MS` (0: unlimited) |
| Discovery | client cancellation per request | on (MCP `notifications/cancelled`, or the handler being dropped) | — |
| Discovery | definition candidates | 5,000 (unchanged) | — (now reported) |
| Retention | cached content in total | 512 MiB: 1/2 file contents, 1/4 outlines, 1/4 `path@etag` snapshots | `--cache-mb`, `SPEEDREAD_CACHE_MB` |

Differences from the design above, and why:

- **The entry limit is per walk, not per request.** A depth-2 trace walks the same tree
  several times; a shared counter made the limit depend on the tool rather than on the tree.
  The deadline and cancellation are per request (one `Limits` per `Engine::scoped` view).
- **Defaults are 1,000,000 files and 30 s**, not 200,000 and 15 s. Upstream's own caps
  (400,000 listed files for `map`) are above 200,000, and a default must not turn a complete
  answer on a large monorepo into an incomplete one. The defaults are a backstop against
  runaway work; tighten them per deployment.
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
  stopped" (`read #Name`, `trace`). JSON Lines output keeps stdout clean and prints the line
  on stderr.
- **Cancellation is wired** (rmcp 3.4.1 hands tool handlers the request's token).

Known gaps:

- The macOS fast walker (`getattrlistbulk`) is not compiled or tested here. Limits are checked
  when it delivers a batch, so entry limit, deadline and cancellation all work, but a walk
  whose glob filters out every file has no delivery and runs to the end. The portable walker
  (Linux, and macOS with `SPEEDREAD_PORTABLE_WALK`) checks every entry.
- A working set larger than the ceiling is re-parsed on every access (LRU). On the hostile
  corpus, re-reading 1,200 symbols takes 0.5 s when everything is cached (`--cache-mb 4096`,
  1.1 GiB resident, same as upstream) and 27 s when it is not (default, 0.5 GiB resident).
  That is the trade the flag controls; ordinary repositories fit either way.
