# 04 — Structural search

Status: specified. Invariants: 4, 5, 8. Borrowed from ast-grep.

## Behavior

`search` gains `ast=true` (CLI `--ast`): `pattern` is code with metavariables — `$NAME` one
node, `$$$ARGS` zero or more — matched against syntax trees, e.g. `$VALUE.unwrap()`,
`console.log($ARG)`, `router.$METHOD($PATH, $$$HANDLERS)`.

- `lang` (CLI `--lang`) selects the grammar. Omitted: the pattern is compiled once per
  language present among candidate files and silently skipped for languages where it does
  not parse; the response lists the languages it ran for.
- Results flow through the existing renderer: grouped by enclosing symbol, `output=scopes`
  and `output=symbols` work unchanged, budgets apply.
- Captures are shown after the hit line when a pattern has named metavariables:
  `97	send_request(session)?;	$ARG=session`.

## Design

- Dependency: `ast-grep-core` 0.51 with its `tree-sitter` feature (requires `tree-sitter ^0.27`,
  the version speedread already links; other deps: `bit-set`, `regex`, `thiserror`). Not
  `ast-grep-language`: speedread implements `ast_grep_core::Language` for its own `LangId`
  (grammar, metavariable expando character per language) in `src/structural.rs`.
- **Text prefilter keeps cost proportional to matches:** the longest literal identifier in the
  pattern becomes a ripgrep literal scan; only matching files are parsed. A pattern with no
  literal (e.g. `$A == $A`) requires an explicit path/glob scope and is subject to spec 02 limits.
- Read-only: no rewrite, no YAML rules. Relational predicates (`inside`, `has`) are a later
  extension through one optional `rule` JSON argument, only if the simple form proves insufficient.

## Tests

- Rust: `$V.unwrap()` matches method calls, not the word `unwrap` in comments/strings.
- TypeScript/TSX: `console.log($A)` matches one-argument calls only; `console.log($$$A)` matches all.
- `lang` omitted over a mixed tree runs for each parseable language and reports them.
- Prefilter equivalence: results with and without the prefilter are identical on fixtures.
- Invalid pattern → message naming the language and the parse failure.

## Ship gate metric

Precision against regex on the reference repos for three shapes (comment/string false
positives removed), and wall time within 2× of the equivalent literal `search`.
