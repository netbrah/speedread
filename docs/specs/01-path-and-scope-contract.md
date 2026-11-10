# 01 — Path and scope contract (CWD independence)

Status: implementing. Invariants: 3, 7.

## Problem

- The primary root defaults to the process CWD. An MCP server's CWD is wherever the client
  launched it; agents' working directories change and cannot be guaranteed.
- Workspace-wide lookups (`#Symbol`, `trace`, "did you mean") always run in `roots[0]`, even
  when the target lives in another root or repository.
- A rootless server (`/` as implicit root) walks `/` for `search`/`map` without a path.

## Contract

Terms:

- **base** — where relative paths resolve and what a path-less `search`/`map` covers.
  `base_dir` (request) → else the primary root (`--root`, `$CLAUDE_PROJECT_DIR`, client roots,
  CWD unless disabled).
- **lookup root** — the tree searched for definitions/usages of a target: the configured
  root containing the target (longest match) → else the nearest ancestor holding `.git`
  (directory *or* file, so worktrees and submodules work) → else the target's directory.
  For a path-less target (`#Name`) the target is the base.
- **access policy** — unchanged: configured roots, dependency caches, or `--unrestricted`.

Rules:

1. Absolute paths work from anywhere the access policy allows; they never consult the CWD.
2. Every tool accepts `base_dir` (alias `cwd`); the CLI accepts `-C/--base-dir`. It must be
   absolute (or `~/…`), exist, be a directory and pass the access policy; otherwise the call
   fails with a message naming the rule. No guessing.
3. With `--no-cwd-root` (or `SPEEDREAD_NO_CWD_ROOT=1`) the CWD is never a root. Without
   configured or client roots, a relative path or a path-less `search`/`map`/`#Name` then
   **fails** with: use an absolute path or pass `base_dir`. This is the recommended harness
   mode together with `--unrestricted` or explicit roots.
4. A rootless server never walks `/`: path-less `search`/`map`/`#Name`/`trace` fail as in rule 3.
5. Lookups run in the target's lookup root, not `roots[0]`. Output names the root searched
   when it differs from the base.
6. A discovered `.git` root sets lookup scope only. It grants no access and does not widen an
   explicit path scope: `search … path=/repo/src` searches `/repo/src`.
7. `base_dir` is the default scope exactly; it is never expanded to its repository root for
   `search`/`map`.
8. Display: paths are shown relative to the base when under it, else `~/…`, else absolute.
   Round-trip invariant: any displayed path resolves to the same file when passed back with
   the same `base_dir`.
9. No call changes the process CWD. Cache keys are canonical absolute paths plus content
   stamps, so identically named files in two worktrees never collide.
10. Multi-target calls resolve each target independently; each `#Symbol`/trace target uses
    its own lookup root.

## Design

- `Workspace` gains `base: Option<PathBuf>` and `with_base(&self, dir) -> Result<Workspace, String>`
  (clones roots/policy; validates rule 2). `primary()` returns the base when set.
- `Workspace::lookup_root(&self, target: &Path) -> PathBuf` implements the lookup-root rule;
  `.git` discovery walks parents with one `symlink_metadata` per level.
- `Workspace::require_base(&self) -> Result<PathBuf, String>` returns the base or the rule 3/4
  error; `search`, `map`, `find_definitions`, `trace` call it instead of `primary()` for
  path-less scope.
- `Engine` splits into per-request view + shared caches: `Engine { ws, cfg, shared: Arc<Shared> }`
  and `Engine::scoped(&self, base_dir) -> Result<Engine, String>`. Caches, tokenizer calibration
  and counters live in `Shared`, so a scoped engine is two `Arc` clones.
- `find_definitions(e, query, limit)` → takes the lookup root; `trace::scan_names` and
  `defs_named` take it from the resolved target (`Def.src.path`).
- `Engine::file_list()` → `file_list(root)`, cached per root.
- MCP: `base_dir` on all four tools. CLI: global `-C/--base-dir`, `--no-cwd-root`.

## Tests (must fail before, pass after)

- CLI run from an unrelated CWD with absolute targets under `--unrestricted --no-cwd-root`:
  output identical to a run from inside the repo with `-C <repo>` (modulo display rule 8).
- Two sibling repos each defining `fn target()`; `trace /b/src/lib.rs#target` reports callers
  from `b`, never from `a` (the CWD/primary root).
- Two worktrees with the same relative file and different content: reads return each
  worktree's content; etags differ.
- Rootless (`--no-cwd-root`, no roots): `search foo`, `map`, `read rel/path`, `read '#Name'`
  each return the rule 3 message and perform zero directory walks (walk counter).
- `base_dir` relative, missing, a file, or outside policy → specific errors.
- MCP: same request with and without `base_dir` from a server started in `/tmp`.

## Non-goals

Inferring roots from file contents, monorepo package boundaries, or build metadata.
