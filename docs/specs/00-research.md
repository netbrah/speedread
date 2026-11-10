# 00 — Research and decision record

Date: 2026-11-10. Method: source inspection of upstream `37abea2`, a local Linux build and
test run, crates.io metadata for candidate dependencies, and the earlier design discussion.

## Decision

**Adapt** speedread in a fork, composing three borrowed ideas at query time — structural
matching (ast-grep), ancestor context (grep-ast), evidence-labelled relationships (graph tools) —
on top of two foundations speedread lacks: a CWD-independent path/scope contract and
bounded discovery. **Reject** adopting any persistent index, graph store or resident service.

## Workload and the three questions

Consumers: many concurrent coding agents (CLI calls and long-lived stdio MCP servers) over
TypeScript and Rust repositories, in arbitrary working directories, including worktrees and
directories with large untracked trees. The system must answer:

1. *Where is this, with enough surrounding structure to understand it?* (search → context → exact source)
2. *What code has this shape?* (structural match)
3. *What is syntactically related to this symbol, and how sure are we?* (bounded relationships)

## Authority lanes (what stays authoritative for what)

| Query class | Authoritative lane | speedread's role | Must not replace |
|---|---|---|---|
| Text discovery | ripgrep engine over current files | owns it | — |
| Structure of a file | tree-sitter parse of current bytes | owns it (derived, exact for syntax) | — |
| Cross-file symbol identity, types, overloads, macros, trait dispatch | compiler / LSP (rust-analyzer, tsserver) | candidates only, labelled heuristic | LSP diagnostics and compiler truth |
| "All references", safe-delete, blast radius | compiler / LSP | never claims it | — |
| Change history | Git objects | reads on demand (backlog) | — |

## Findings from upstream source (`37abea2`)

- Baseline on Linux x86_64, rustc 1.99.1: `cargo test --all-targets` passes (28 unit, 5 MCP, 1 outline snapshot).
- **Workspace lookups are pinned to the primary root.** `search::find_definitions`
  (`roots().take(1)`), `trace::defs_named` and `trace::scan_names` (`ws.primary()`), and
  `Engine::file_list` all use `roots[0]`, which defaults to the process CWD. A target in a
  secondary root or another repository is resolved, but its callers/definitions are then
  searched in the wrong tree. → spec 01.
- **Rootless servers can walk `/`.** `rootless()` is only consulted by `read`; a `search` or
  `map` without a path on a server started in `/` uses `primary()` = `/` as scope. → spec 01.
- **Silent caps.** `find_definitions` stops collecting at 5,000 candidate files and
  `Engine::file_list` at 250,000 entries without telling the caller; only `max_matches` is
  reported. → spec 02.
- **Resident memory is capped, but high and fixed.** Source cache 512 MiB, snapshot cache
  256 MiB, per process, not configurable. Trace and `#Symbol` lookups load every candidate
  through the source cache, so a long-lived MCP server trends toward the cap. → spec 02.
- **Walks include untracked, non-ignored files** (grep semantics; needed so agents see files
  they just created) and skip a fixed list (`node_modules`, `.venv`, …). There is no entry or
  time bound on a walk. → spec 02.
- **Search context is one level.** Hits are grouped under the innermost outline symbol only;
  loops, conditions and match arms between the symbol header and the hit are not shown. → spec 03.
- **Relationship kinds are `Call | Ref | Import`.** Closures are attributed to the enclosing
  named function and callables passed as values are plain refs (upstream ROADMAP
  "Calls inside closures"). → spec 05.

## Dependency facts (crates.io, 2026-11-10)

| Crate | Version | Relevant requirement | Verdict |
|---|---|---|---|
| `ast-grep-core` | 0.51.1 | `tree-sitter ^0.27.0` (optional feature); otherwise `bit-set`, `regex`, `thiserror` | **Compatible** with speedread's `tree-sitter 0.27`; small. Use for spec 04 without `ast-grep-language` (which bundles its own grammar set). |
| `sem-core` | 0.30.0 | `tree-sitter ^0.26`, plus `ciborium`, `serde_yaml`, `toml`; optional `git2`, `rusqlite`, `oxc_*` | **Do not depend.** Tree-sitter major mismatch and a second grammar stack. Borrow the matching algorithm (identity → structural hash → fuzzy), not the crate. |
| `gix` | 0.90.0 | ~35 required `gix-*` crates before features | **Defer.** Too heavy for "list tracked paths"; evaluate `gix-index` alone or a minimal `.git/index` reader behind a 3-function interface (backlog). |

## Sources

- https://github.com/brennengreen/speedread — upstream; README, ROADMAP ("Calls inside closures", Linux items), CONTRIBUTING (model-facing text rule, PR rules). Basis for all "findings" above.
- https://github.com/ast-grep/ast-grep — pattern syntax (`$VAR`, `$$$ARGS`), relational rules (`inside`/`has`); `ast-grep-core` is the embeddable matcher. Why it matters: structural search without inventing a rule language.
- https://crates.io/crates/ast-grep-core — dependency footprint and tree-sitter requirement checked here; decides feasibility of spec 04.
- https://github.com/Aider-AI/grep-ast — the "show the syntax-tree scopes above a match" idea; algorithm is language-agnostic (line → enclosing node start lines), which fits 20 grammars without per-language tables.
- https://github.com/Ataraxy-Labs/sem — entity matching phases and structural-vs-cosmetic change classification; also the operational failure we are designing against (resident process growth with large untracked trees — reported by the user, not reproduced here).
- https://crates.io/crates/sem-core — shows the tree-sitter 0.26 pin and optional heavy features; why we borrow ideas, not code.
- https://github.com/GitoxideLabs/gitoxide — `gix` status/untracked traversal controls; relevant only if we add a tracked-paths input (backlog).

## Scale evidence and unknowns

- Upstream benchmarks are macOS-first; the Linux portable walker (`ignore` crate) is what we run.
  Our numbers come from `scripts/fork-eval.py` on this sandbox (2 vCPU, 7 GiB) — small-machine
  evidence, not fleet evidence. Concurrent-agent behavior on real NetApp-scale checkouts is **unvalidated**.
- sem's memory growth under untracked trees is **not reproduced**; spec 02 is justified by
  speedread's own unbounded walks and fixed caches, independent of sem's root cause.

## PR or fork

Recommendation: **carry the fork; upstream opportunistically.** Upstream has one maintainer,
one merged PR, and asks for an issue before anything larger than a fix.

| Slice | Upstreamable? | Why |
|---|---|---|
| Linux clippy `dead_code` gating | Yes, now | Listed as an open S item in upstream ROADMAP. |
| Closure / passed-as-value relationship marks (spec 05) | Yes, after an issue | Listed as an open M item in upstream ROADMAP. |
| Rootless `search`/`map` must not walk `/`; silent caps reported | Yes | Bug fixes with tests. |
| Lookups scoped to the target's root (spec 01, part) | Likely | Fixes multi-root behavior upstream already supports. |
| `base_dir`, strict no-CWD mode, discovery limits, cache knobs | Maybe | New surface; needs maintainer buy-in. |
| Ancestor context, structural search | Fork first | Model-facing text changes require eval numbers in upstream's harness. |
