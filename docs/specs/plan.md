# Implementation plan

> Resuming in a new session: read `README.md` (invariants, ship gate), then this file top to
> bottom. Check boxes are the source of truth for progress. Work on branch
> `feat/bounded-context-upgrades`; commit after every green step and push (`git push origin HEAD`).

**Goal:** add CWD-independent scoping, bounded discovery, ancestor context, structural search
and evidence-labelled relationships to speedread without a persistent index.

**Tech:** Rust 2024 (MSRV 1.90), tree-sitter 0.27, ripgrep crates, rmcp 3.4. Tests: `cargo test --all-targets`.

**Verification for every task:**

```sh
. ~/.cargo/env
cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test --all-targets
```

## Global constraints

- Hard invariants 1–8 in `README.md`.
- Existing CLI/MCP output for existing arguments stays byte-identical unless a spec says otherwise
  (upstream snapshot and MCP tests must keep passing unmodified).
- Model-facing strings in `src/server.rs` change only where a spec adds an argument; token cost noted in `eval.md`.
- New integration tests live in `tests/fork_*.rs` and drive the built binary
  (`env!("CARGO_BIN_EXE_speedread")`) with explicit CWDs, so CWD independence is tested for real.

## Tasks

### T0 — Foundations
- [x] Fork, branch, specs committed.
- [ ] Gate macOS-only walker helpers (`IgnoreNode`, `is_ignored`, `load_matcher`, `global_node`,
      `initial_stack` in `src/walk.rs`) behind `#[cfg(target_os = "macos")]` so clippy `-D warnings` passes on Linux.
- [ ] `scripts/fork-eval.sh`: clone pinned reference repos (Rust: `BurntSushi/ripgrep`; TS: `colinhacks/zod`)
      into `target/eval-repos`, run the query set, record wall time, peak RSS (`/usr/bin/time -v`), output bytes
      to `evals/fork/<label>.tsv`. Record baseline (`evals/fork/baseline-37abea2.tsv`).

### T1 — Path and scope contract (spec 01)
Files: `src/workspace.rs`, `src/engine.rs`, `src/search.rs`, `src/trace.rs`, `src/read.rs`, `src/map.rs`,
`src/json.rs`, `src/server.rs`, `src/main.rs`; tests `tests/fork_paths.rs`.
- [ ] Failing tests first (list in spec 01 → Tests).
- [ ] `Workspace { base, cwd_root }`, `with_base`, `require_base`, `lookup_root`; unit tests for `.git` dir/file discovery.
- [ ] `Engine { ws, cfg, shared: Arc<Shared> }`, `Engine::scoped(base_dir)`; `file_list(root)` cached per root.
- [ ] Lookups use the target's lookup root: `find_definitions(e, root, query, limit)`,
      `trace::{defs_named, scan_names}`.
- [ ] Rootless guard for path-less `search`/`map`/`#Name`/`trace`.
- [ ] CLI `-C/--base-dir`, `--no-cwd-root`; MCP `base_dir` on all tools.
- [ ] Verify, commit, push.

### T2 — Bounded discovery and residency (spec 02)
Files: `src/walk.rs`, `src/engine.rs`, `src/search.rs`, `src/trace.rs`, `src/map.rs`, `src/read.rs`, `src/main.rs`;
tests `tests/fork_bounds.rs`.
- [ ] Failing tests first (spec 02 → Tests), including the untracked-growth test.
- [ ] `walk::Limits`, `walk::STATS`, both walkers call `admit()`.
- [ ] Incomplete reporting in `search`, `#Symbol` lookup, `trace`, `map`.
- [ ] `Config.cache_bytes`, `--cache-mb`, `--max-walk`, `--deadline-ms` (+ env).
- [ ] Measure RSS at cache caps; decide default; log in `eval.md`.
- [ ] Verify, commit, push.

### T3 — Ancestor context (spec 03)
Files: `src/context.rs` (new), `src/lib.rs`, `src/search.rs`, `src/json.rs`, `src/main.rs`, `src/server.rs`;
tests `tests/fork_scopes.rs`, fixtures `tests/fixtures/fork/`.
- [ ] Failing tests first. - [ ] `ScopeIndex`. - [ ] `Output::Scopes` rendering. - [ ] Ship-gate measurement. - [ ] Verify, commit, push.

### T4 — Relationship kinds (spec 05)
Files: `src/trace.rs`; tests `tests/fork_trace.rs`.
- [ ] Failing tests first. - [ ] `OccKind::Value`, `Occ.in_closure`, rendering, summary counts. - [ ] Verify, commit, push.

### T5 — Structural search (spec 04)
Files: `Cargo.toml`, `src/structural.rs` (new), `src/search.rs`, `src/main.rs`, `src/server.rs`; tests `tests/fork_ast.rs`.
- [ ] Spike: `ast-grep-core` builds against tree-sitter 0.27 with speedread's grammars; binary size delta recorded.
- [ ] Failing tests first. - [ ] `Language` impl, prefilter, rendering. - [ ] Ship-gate measurement. - [ ] Verify, commit, push.

### T6 — Wrap-up
- [ ] README/skill docs for new flags and arguments; CHANGELOG "Fork" section.
- [ ] `eval.md` complete; decide per slice: default, opt-in, or dropped.
- [ ] Open upstream issues/PRs per `00-research.md#pr-or-fork` (only after user confirms).
