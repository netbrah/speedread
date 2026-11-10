# 04 — Structural search

Status: shipped, opt-in (`ast=true` / `--ast`). Invariants: 4, 5, 8. Borrowed from ast-grep.

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

## As shipped

Differences from the text above, and details it left open (`src/structural.rs`):

- **Summary line:** `N matches in M files for structural \`pattern\` (rust, typescript)` — the
  languages it matched in. With no match the answer says what was examined:
  `No structural matches for … (K files containing \`word\`, P parsed). Ran for: rust.`
- **Hit line:** the line of the pattern's own literal, not of one inside a captured node — for
  `$V.unwrap()` over a chain spanning lines, the `.unwrap()` line. With nothing captured
  (`$_.unwrap()`, `wrap($$$)`), the last occurrence when the literal follows the metavariables,
  else the first.
- **Captures:** `\t$NAME=value` after the line, value on one line clipped to 60 bytes;
  `$$$NAME=` for a multi capture; `$_X` and anonymous `$$$` are not shown. Two matches on one
  line show both sets, in match order. Counts are matches; hit lines are capped at 400 a file
  like every search.
- **Prefilter:** the longest literal word narrows files through the ordinary text scan; a file
  is parsed only if it contains every literal word of the pattern. Numbers are not used as
  literals. `SPEEDREAD_AST_NO_PREFILTER=1` turns both off (tests compare the two).
- **Rust macros:** macro arguments are token trees with no call nodes, so the first version
  missed `assert!(x.unwrap())` — 119 code lines on ripgrep. Arguments are now parsed as call
  arguments, statements or items, and matched (recursively, depth 4). Bodies that do not parse
  that way — chiefly `macro_rules!` rules — are not matched, and the answer says so:
  `\`unwrap\` also appears on 13 lines inside macro bodies that do not parse as code; those were
  not matched (a text search shows them).`
- **A pattern must parse as one syntax node** in a language to run there. `Some($X) => $Y` (a
  Rust match arm) is not a node on its own and finds nothing; with `lang` given that is an
  error naming the language, without it the no-match answer says the pattern parsed nowhere.
- **Limits:** files over 1 MiB are not parsed and are counted in the answer; the request
  deadline and match cap of spec 02 apply (`incomplete: stopped after …`).
- `lang` takes names, extensions and common aliases (`ts`, `py`, `rs`, `golang`, …); `ts` also
  covers `.tsx`. A `.lua` file parsed as Luau is matched with the Luau pattern.
- JSON output (`--json`) carries captures in `text` after a tab, and no caveat lines.
- Not done: relational rules (`inside`, `has`), rewrite, C/C++ macro bodies, embedded
  languages (script blocks in HTML/Vue).

Measured result and the ship decision: [eval.md](eval.md#t5--structural-search-evalsforkt5-asttsv-evalsforkt5-ast-shapestsv).
The wall-time gate above was not met as written; see there.
