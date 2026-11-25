# Fork additions

[netbrah/speedread](https://github.com/netbrah/speedread) is a fork of [https://github.com/brennengreen/speedread](https://github.com/brennengreen/speedread) by Brennen Green. The MIT license is preserved ([LICENSE](../LICENSE)). netbrah maintains the fork.

This fork keeps speedread's
shape — a stateless reader, no index, no daemon — and adds what a long-lived agent tool on
large or messy trees needs. Specs, plan and measurements: [docs/specs/](specs/README.md).
Nothing here adds a persistent index, a graph store or a background process.

| Addition | How to use it | Default? | Spec |
|---|---|---|---|
| An unfenced server | nothing; `speedread mcp` reads any path a call names. `--restricted` confines it to its roots | on | [08](specs/08-unfenced-server.md) |
| Working-directory independence | `--base-dir <DIR>`, `--no-cwd-root` (`SPEEDREAD_NO_CWD_ROOT=1`); MCP `base_dir` on every tool | on; flags optional | [01](specs/01-path-and-scope-contract.md) |
| Bounded discovery | `--max-walk <FILES>`, `--deadline-ms <MS>` (`SPEEDREAD_MAX_WALK`, `SPEEDREAD_DEADLINE_MS`); MCP cancellation stops walks | on (1,000,000 files, 10 s) | [02](specs/02-bounded-discovery.md) |
| Bounded memory | `--cache-mb <MIB>` (`SPEEDREAD_CACHE_MB`) | on (256 MiB) | [02](specs/02-bounded-discovery.md) |
| Loop/branch context for hits | `search --output scopes` / `output="scopes"` | opt-in | [03](specs/03-ancestor-context.md) |
| Structural search | `search --ast '$X.unwrap()' [--lang rust]` / `ast=true`, `lang` | opt-in | [04](specs/04-structural-search.md) |
| Closure and value-use marks in `trace` | removed with the `trace` tool | — | [rejected](rejected/precise-trace.md) |

## What changes if you do nothing

- Answers that were cut short say so. A request that hits its file limit or its deadline
  (10 s, walking and parsing together), or is cancelled, ends the answer with
  `incomplete: …` instead of claiming there is nothing more. `--deadline-ms 30000` waits
  longer; `0` waits forever, as upstream does.
- A text search no longer buffers a line of any length: past 4 MiB it stops reading that
  file and says `not fully searched: …`. One 200 MiB single-line bundle used to cost 1 GiB.
- `trace` was removed. Callers and callees are a `search` for the name, then one `read` of the enclosing symbols.
- The MCP server no longer answers `Outside the workspace`. A server registered with
  `--root DIR`, or launched inside a project, used to refuse paths outside it; now the
  folder only says where relative paths and path-less calls resolve. Add `--restricted` to
  keep the old limit. Commands (`speedread read …`) are limited as before.
- Reading a named file never walks the tree, however many untracked files surround it.
- A server started without a root no longer adopts whatever directory it was launched from
  when `--no-cwd-root` is set; it asks for absolute paths or a `base_dir`.
- Definition and usage lookups run in the root that contains the target, not only the first
  root.
- A long-lived server holds at most `--cache-mb` (256 MiB) of file contents and outlines. On
  a working set larger than that it re-parses instead of growing. Upstream's outline cache
  has no byte limit; `--cache-mb 4096` approximates that.
- A long-lived server gives memory back when a request ends. Tree-sitter's C library used the
  system allocator, which kept what each parse freed: upstream stays at the high-water mark
  of the largest file it ever parsed (340 MiB after one 7 MiB file; 33 MiB here).
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

## Install the fork

One plugin, `speedread-netbrah`, covers Claude Code, Claude Cowork, Cursor, Grok Build and
Perplexity Computer; an `.mcpb` bundle covers Claude Desktop chat. Commands for each are in
[DISTRIBUTION.md](DISTRIBUTION.md). None of them asks for a folder: the server is started
with `--no-cwd-root` (and `--unrestricted`, which a server no longer needs), reads by
absolute path or `base_dir`, and refuses a
path-less call rather than searching wherever it was launched. It installs beside
upstream's `speedread` rather than over it; both offer tools named `map`, `read` and
`search`, so enable one at a time.

Fork releases are versioned `<upstream minor + 1>.0-netbrah.N` (`speedread --version`, the
MCP server info, the bundle and the plugin agree). The GitHub Actions release job publishes
a full release of this fork (not a pre-release) and registers it as `io.github.netbrah/speedread`.
Upstream's registry listing stays `io.github.brennengreen/speedread`.

`scripts/fork-release.sh TAG` cross-compiles every asset from Linux (macOS arm64 and
x86_64, static Linux x86_64 and arm64, the bundle, checksums) and pins the plugin launcher
to them. Binaries built that way for a platform the build machine cannot run have not been
executed, and the release notes must say which. With Actions enabled, pushing a `v*` tag
builds all four targets and the bundle (`.github/workflows/ci.yml`).

## Not changed

- The MCP tool descriptions and server instructions (model-facing text) are untouched apart
  from the new argument descriptions. [`skills/speedread/SKILL.md`](../skills/speedread/SKILL.md)
  keeps upstream's workflow and its model-facing description; it now says this repository
  is netbrah's fork of speedread by Brennen Green. The plugin carries its own copy with
  the fork's arguments, including `--unrestricted` on the command-line route. A server started with no root appends one sentence to its
  instructions (spec [07](specs/07-distribution.md)). Whether models pick
  the new arguments unprompted is not measured; upstream's agent evals need API keys.
- The macOS fast walker compiles and lints clean for `aarch64-apple-darwin` with the limits
  (cross-compiled), but has not been run: development and every measurement were on Linux.

## Relationship to upstream

Based on [https://github.com/brennengreen/speedread](https://github.com/brennengreen/speedread) by Brennen Green at `37abea2`. The MIT license is preserved. The plan for what to offer upstream, and in
what order, is in [00-research.md](specs/00-research.md#pr-or-fork).
