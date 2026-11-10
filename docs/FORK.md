# Fork additions

This fork (`netbrah/speedread`, branch `feat/bounded-context-upgrades`) keeps speedread's
shape — a stateless reader, no index, no daemon — and adds what a long-lived agent tool on
large or messy trees needs. Specs, plan and measurements: [docs/specs/](specs/README.md).
Nothing here adds a persistent index, a graph store or a background process.

| Addition | How to use it | Default? | Spec |
|---|---|---|---|
| Working-directory independence | `--base-dir <DIR>`, `--no-cwd-root` (`SPEEDREAD_NO_CWD_ROOT=1`); MCP `base_dir` on every tool | on; flags optional | [01](specs/01-path-and-scope-contract.md) |
| Bounded discovery | `--max-walk <FILES>`, `--deadline-ms <MS>` (`SPEEDREAD_MAX_WALK`, `SPEEDREAD_DEADLINE_MS`); MCP cancellation stops walks | on (1,000,000 files, 10 s) | [02](specs/02-bounded-discovery.md) |
| Bounded memory | `--cache-mb <MIB>` (`SPEEDREAD_CACHE_MB`) | on (256 MiB) | [02](specs/02-bounded-discovery.md) |
| Loop/branch context for hits | `search --output scopes` / `output="scopes"` | opt-in | [03](specs/03-ancestor-context.md) |
| Structural search | `search --ast '$X.unwrap()' [--lang rust]` / `ast=true`, `lang` | opt-in | [04](specs/04-structural-search.md) |
| Closure and value-use marks in `trace` | nothing; `λ` and `→` appear in `trace` output | on | [05](specs/05-relationship-kinds.md) |

## What changes if you do nothing

- Answers that were cut short say so. A request that hits its file limit or its deadline
  (10 s, walking and parsing together), or is cancelled, ends the answer with
  `incomplete: …` instead of claiming there is nothing more. `--deadline-ms 30000` waits
  longer; `0` waits forever, as upstream does.
- A text search no longer buffers a line of any length: past 4 MiB it stops reading that
  file and says `not fully searched: …`. One 200 MiB single-line bundle used to cost 1 GiB.
- `trace` reads the tree once per level instead of three times, and a trace that reaches
  its 20,000-line limit keeps the call sites nearest the definition, the same on every run.
- Reading a named file never walks the tree, however many untracked files surround it.
- A server started without a root no longer adopts whatever directory it was launched from
  when `--no-cwd-root` is set; it asks for absolute paths or a `base_dir`.
- Definition and usage lookups run in the root that contains the target, not only the first
  root.
- A long-lived server holds at most `--cache-mb` (256 MiB) of file contents and outlines. On
  a working set larger than that it re-parses instead of growing. Upstream's outline cache
  has no byte limit; `--cache-mb 4096` approximates that.
- `trace` marks call sites inside closures with `λ` and lists places a function is passed,
  assigned or returned as a value with `→`.

Everything else is byte-identical to upstream `37abea2` on the 15-query reference set
([eval log](specs/eval.md)).

## Examples

Structural search — code with holes, matched against syntax. `$NAME` is one node, `$$$NAME`
any number; captures follow the line.

```
$ speedread search --ast '$M.insert($K, $V)' crates/ignore/src
3 matches in 2 files for structural `$M.insert($K, $V)` (rust)
==> crates/ignore/src/dir.rs @47ed5bdd2c88951c (1 match)
[191-262] pub(crate) fn add_parents<P: AsRef<Path>>(&self, path: P) -> (Ignore, Option<Error>) (in Ignore)
256	            compiled.insert(	$M=compiled	$K=parent.as_os_str().to_os_string()	$V=Arc::downgrade(&ig_arc)
```

It parses every file that contains the pattern's words, so it costs more than a text search
(about 80–210 ms on ripgrep and zod, on two cores). Rust `macro_rules!` bodies cannot be
matched; the answer counts the lines it could not see.

Loop and branch context — the header lines between the symbol and the hit:

```
$ speedread search 'is_match\(' crates/searcher/src/searcher/core.rs --output scopes
[475-519] fn find_by_line_fast(&mut self, buf: &[u8]) -> Result<Option<Range>, S::Error> (in Core)
484	        while !buf[pos..].is_empty() {
488	            match self.matcher.find_candidate_line(&buf[pos..]) {
505	                Ok(Some(LineMatchKind::Candidate(i))) => {
511	                    if self.is_match(&buf[line])? {
```

A server that never assumes a working directory:

```
speedread mcp --no-cwd-root            # roots come from the client, or each call passes base_dir
speedread --base-dir /repo/services/api search 'TODO'   # relative scope without cd
```

## Not changed

- The MCP tool descriptions and server instructions (model-facing text) are untouched apart
  from the new argument descriptions. The bundled skill is untouched. Whether models pick
  the new arguments unprompted is not measured; upstream's agent evals need API keys.
- The macOS fast walker compiles with the limits but was not run (Linux sandbox only).

## Relationship to upstream

Based on `brennengreen/speedread` `37abea2`. The plan for what to offer upstream, and in
what order, is in [00-research.md](specs/00-research.md#pr-or-fork).
