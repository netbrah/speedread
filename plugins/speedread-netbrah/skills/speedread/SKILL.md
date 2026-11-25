---
name: speedread
description: Read, search and navigate code with a token budget. Use for any code reading - whole files, line ranges, symbols (functions, classes, sections, keys), text or syntax-pattern search grouped by enclosing function, call graphs (callers, callees, references, implementations), repo overviews, and re-reading a file after an edit (diff only). Prefer it over cat/head/sed/grep/find and over one-file-at-a-time reads. Works on any path the machine can read; no project folder or index to set up.
license: MIT
---

# speedread

Fork of [https://github.com/brennengreen/speedread](https://github.com/brennengreen/speedread) by Brennen Green. The MIT license is preserved. This copy is the netbrah plugin skill.

speedread returns the *smallest useful unit of code* for the question, sized to a
token budget: a symbol instead of a file, a skeleton instead of a truncated page, a
diff instead of a re-read. Four tools (`map`, `search`, `trace`, `read`) or the same commands, plus `context` for a natural-language question that plans those four and returns one bundle (default budget 8000).

**Which one you have.** If the tools `read`, `search`, `trace` and `map` from the
`speedread` server are available, call them. If they are not (the host does not
start local servers), run the launcher that ships beside this file:
`sh scripts/speedread.sh --unrestricted <command> ...` (path relative to this
skill's folder). It fetches the pinned binary on first use, checks its SHA-256,
and takes the same arguments as the tools.

**Where it reads.** This plugin configures no project folder. Name files by
absolute path (`/abs/repo/src/app.ts#Server.start`), or pass `base_dir` (an
absolute directory) so relative paths and path-less `search`/`map` resolve
there. A path-less call without `base_dir` is refused rather than searching
wherever the server happened to start. The tools read any path you name. The CLI
resolves relative paths against the current directory and, unlike the tools,
refuses paths outside it (`Outside the workspace`) unless you pass
`--unrestricted` before the command, as in the launcher line above.

## The loop

1. **Orient** — `map` (or `map symbols=true`) instead of `ls`/`find`/`tree`.
2. **Locate text** — `search "pattern"`: hits come grouped under their enclosing
   function/class with its line range, e.g. `[40-72] fn parse(...)`.
3. **Locate relationships** — `trace "#parse"` (callers), `direction=callees`,
   `refs`, `impls`, `depth=2` for a call tree.
4. **Read exact evidence** — ONE `read` call with every target you need:
   `["src/app.ts#Server.start", "src/util.ts:40-80", "#parse_header"]`.
5. **After editing** — `read "path@etag"` (etag from the `==> path @etag` header):
   returns `unchanged`, the appended lines, or a diff labelled with the changed
   symbols (`body changed, signature unchanged` / `signature changed` / `added`).

## Targets (read)

| Target | Returns |
|---|---|
| `path` | whole file; too big for the budget → skeleton (signatures, docs; bodies collapsed as `A-B ⋯`) |
| `path:120-180`, `path:120` | a range; a line returns the function/class enclosing it |
| `path#Name`, `path#Type.method`, `#Name` | a symbol's full source incl. docs; `#Name` searches the workspace |
| `README.md#Install`, `package.json#scripts` | a Markdown section, a JSON/YAML/TOML key |
| `src/**/*.test.ts` | a glob (shares the budget) |
| `path@etag` | only what changed since that version |

Rules of thumb:
- Batch. Five targets in one `read` cost one round-trip; five reads cost five
  (each round-trip re-sends the whole conversation).
- A skeleton is a map, not an answer: expand the body you need with `path#Name` or
  the `A-B` range shown on the collapsed line.
- `search output=symbols` returns the full source of every enclosing function in
  the same call — use it when you will read the callers anyway.
- `trace` is syntactic (tree-sitter + name resolution, no type inference). Sites it
  cannot attribute to one of several same-named definitions are marked `?`.
- `search ast=true` matches code shape, not text: `pattern` is code with
  metavariables (`$X` one node, `$$$ARGS` any number), e.g. `foo($A, $$$REST)`;
  set `lang` when the paths do not imply one grammar.
- An answer that stopped early says so on a line starting `incomplete:` (the time
  limit, the file-count limit, or a cancelled request). Narrow with a path, glob
  or `base_dir` and ask again; do not read it as "nothing more exists".
- Budgets are tokens (default 8000; map 3000; trace 4000; max 10000). Raise
  `budget` rather than paging.

## Scripts and code execution

The CLI mirrors the tools and adds JSON Lines for filtering in code instead of
in context. `speedread` below is `sh scripts/speedread.sh --unrestricted` when the binary is
not on PATH:

```bash
speedread read 'src/app.ts#Server.start' src/util.ts:40-80
speedread search 'AbortWithStatus\(' --output symbols
speedread trace '#AbortWithStatus' --direction callers --depth 2
speedread symbols src --json | jq -r 'select(.kind=="function" and .end-.start>80) | "\(.path):\(.start) \(.qualified)"'
speedread search 'TODO' --json | jq -r .path | sort | uniq -c | sort -rn | head
speedread map --json | jq -s 'map(select(.lines != null)) | sort_by(-.lines) | .[:10]'
```

## Limits worth knowing

- Read-only. It never writes to the code it reads; the launcher writes only its
  own cache directory.
- No index and nothing to keep fresh: every answer is computed from the files as
  they are now. Very large trees are bounded by a 10 s discovery deadline and a
  walk limit, and the answer says when either was hit.
- macOS and Linux, arm64 and x86_64. Not Windows (WSL counts as Linux).
