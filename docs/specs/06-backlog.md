# 06 — Backlog

Specified enough to start; not scheduled until 01–05 ship.

## Entity correspondence across edits (from sem)

`path@etag` diffs label hunks by symbol using name keys. Add matching in three phases —
qualified name, then structural hash of the body with identifiers/whitespace normalized,
then bounded fuzzy similarity — within the before/after of one file only. Output gains
`moved`, `renamed (candidate)` and `cosmetic` labels. Fuzzy matches are candidates, never
identity. No dependency on `sem-core` (tree-sitter 0.26 pin; see 00).

Also from weave: account for interstitial regions (imports, attributes, module-level
statements) so changes outside named entities are never dropped from a symbol-labelled diff.

## Bounded import-aware lookup

Resolve a `#Name` / trace target through the file's own imports before falling back to
same-name candidates.

| Language | In scope | Reported unresolved |
|---|---|---|
| TypeScript | relative imports, named aliases, direct re-exports (≤ 3 hops) | package/path-mapped imports unless trivially determinable, dynamic imports, inferred receivers |
| Rust | `use` aliases, `crate`/`self`/`super` paths, file-visible modules, named `impl` blocks | macros, trait dispatch, `cfg`-dependent and build-generated modules |

Caps: hops, files parsed, elapsed time (spec 02 limits). Evidence label: `import-path`.

## Git input layer

A 3-function interface, no code-intelligence logic:

```
tracked_paths(root, scope, limits) -> iterator
changed_paths(root, base, target, scope, limits)
read_blob(root, revision, path, limits)
```

Experiment before choosing a backend: minimal `.git/index` reader vs `gix-index` vs `gix`
(0.90 pulls ~35 `gix-*` crates). Go/no-go: binary size delta < 1 MiB and cold build delta
< 30 s for the chosen backend; `tracked_paths` on a repo with 50k untracked files must not
stat untracked entries. Enables `discovery=tracked` and revision-to-revision operations
whose results cannot depend on untracked disk contents.

## Change-oriented navigation

From changed symbols (working tree vs revision) to direct usage and test candidates,
explicitly as investigation aids — no transitive blast radius, no test-skipping guarantees.
Depends on the Git input layer and spec 05.
