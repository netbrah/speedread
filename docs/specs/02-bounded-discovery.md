# 02 — Bounded discovery and residency

Status: implementing. Invariants: 2, 4, 5.

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
