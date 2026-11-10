# 03 — Ancestor context for search hits

Status: implementing. Invariants: 4, 5. Borrowed from grep-ast.

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
