# 03 — Ancestor context for search hits

Status: shipped, opt-in (see "As shipped"). Invariants: 4, 5. Borrowed from grep-ast.

## Problem

A hit inside a long function is shown as `[20-420] fn process_batch(...)` plus the matching
line. The loop, condition or match arm that governs the hit is missing, so the agent reads
the whole function (or guesses a range) to learn it.

## Behavior

`search … output=scopes` (CLI `--output scopes`): for each hit, between the enclosing-symbol
header and the hit line, print the **header line of every syntax node that encloses the hit
and starts on an earlier line** — innermost last — then the hit. Shared ancestors print once
per file block; skipped ranges are implicit in the line numbers.

```
==> src/batch.rs @9f86… (1 match)
[12-410] fn process_batch(batch: &Batch) -> Result<()>
88	    for item in batch.items() {
91	        if let Some(session) = item.session() {
93	            match session.state() {
94	                State::Ready => {
97	                    send_request(session)?;
```

Rules:

1. Language-agnostic: an ancestor qualifies when it is a named node spanning more than one
   line whose first line is before the hit line and after the symbol's definition line.
   Several nodes starting on the same line count once.
2. Bounded: at most 8 ancestor lines per hit (outermost kept, then innermost; a `⋯` line
   marks dropped middle levels). Files above `MAX_GROUP_BYTES` (1 MiB) fall back to plain
   matches, as grouping does today.
3. Exact source lines with real line numbers; nothing synthesized.
4. No parse, no ancestors: files without a grammar render as `output=matches`.
5. `context=N` lines still apply and merge with ancestor lines by line number.

## Design

- `src/context.rs`: `ScopeIndex::build(tree, lines) -> ScopeIndex` and
  `ScopeIndex::ancestors(line0, floor_line0) -> SmallVec<u32>`. Built by one tree walk that
  records, for each multi-line named node, `(start_row, end_row)`; lookup filters intervals
  containing the row. Built only for files that are rendered (the existing lazy
  `prepare` chunking), so cost follows the output budget.
- `search::Output::Scopes`; `render_file` emits ancestor lines (deduped against lines already
  printed in the block) before each hit.
- JSON Lines output adds `"scopes": [line, …]` per match record.

## Tests

- Rust and TypeScript fixtures with a hit nested in loop → condition → match/switch arm:
  exact expected output, line numbers included.
- Two hits sharing ancestors print the shared headers once.
- Depth cap: a hit nested 12 levels prints 8 ancestor lines and one `⋯`.
- A hit at symbol top level prints no extra lines (output equals `output=matches`).
- Unsupported language file equals `output=matches`.

## Ship gate metric

On the reference repos, for hits inside functions longer than 60 lines: bytes returned by
`output=scopes` versus the cheapest existing way to see the same governing lines
(`output=symbols`), and versus `output=matches`. Ship as opt-in; it becomes the default only
if overhead versus `matches` is under 35% on the query set.

## As shipped

`search … output=scopes` (CLI `--output scopes`, MCP `output: "scopes"`, JSON Lines
`"scopes": [line, …]` plus `"scopes_omitted": n` when capped). Code: `src/context.rs`.

Refinements found while implementing:

- **Sequence containers are not ancestors.** A Python block, a Go statement list or a
  decorated definition starts where its first child starts; printing its first line would
  show a sibling statement (`acc = 0`) as if it enclosed the hit. A node is skipped when it
  begins at the same byte as its first named child and its second named child starts on a
  later line at the same or a shallower column. JSX elements pass (children are indented
  under the opening tag), so enclosing elements are shown.
- **Lines with no letter or digit are not context** (a `{` on its own line in Allman style).
- **The floor is the line before the body's interior**, not the definition line, so the tail
  of a multi-line signature is not repeated under the symbol header.
- **Ancestors are computed for context lines too**, which keeps line numbers ascending when
  `context=N` is combined with scopes.
- **Cap:** the outermost line, `⋯ N more levels`, then the 7 innermost.
- **One parse:** `Engine::outline_with_tree` returns the outline and the tree from a single
  parse when the outline is not cached. The tree is never cached.

Measured (`scripts/fork-scopes-eval.py`, 12 searches on ripgrep and zod, unlimited budget;
raw numbers in `evals/fork/t3-scopes.tsv`):

| | matches | scopes | symbols |
|---|---|---|---|
| Bytes returned | 483,007 | 556,407 (+15%) | 2,351,927 |
| Wall, sum of medians | 1320 ms | 1438 ms (+9%) | — |

Per query the overhead ranges from +1% to +53%; five of twelve exceed the 35% bar, so
`scopes` stays opt-in and `matches` stays the default. Against `symbols`, the existing way to
see the governing lines, it returns 24% of the bytes.

Not measured: whether agents finish tasks in fewer calls with it. That needs an agent eval
(upstream's `evals/`), not a byte count.
