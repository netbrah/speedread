---
name: speedread
description: Read, search and navigate code with a token budget. Use for any code reading - whole files, line ranges, symbols (functions, classes, sections, keys), text search grouped by enclosing function, repo overviews, and re-reading a file after an edit (diff only). Prefer it over cat/head/sed/grep/find and over one-file-at-a-time reads.
---

# speedread

Fork of [https://github.com/brennengreen/speedread](https://github.com/brennengreen/speedread) by Brennen Green. The MIT license is preserved. Maintained at [netbrah/speedread](https://github.com/netbrah/speedread). This file keeps upstream's workflow. The plugin copy, with this fork's arguments, is [`plugins/speedread-netbrah/skills/speedread/SKILL.md`](../../plugins/speedread-netbrah/skills/speedread/SKILL.md).

speedread returns the *smallest useful unit of code* for the question, sized to a
token budget: a symbol instead of a file, a skeleton instead of a truncated page, a
diff instead of a re-read. Three tools (MCP) or the same three commands (CLI).

## The loop

1. **Orient** — `map` (or `map symbols=true`) instead of `ls`/`find`/`tree`.
2. **Locate text** — `search "pattern"`: hits come grouped under their enclosing
   function/class with its line range, e.g. `[40-72] fn parse(...)`.
3. **Read exact evidence** — ONE `read` call with every target you need:
   `["src/app.ts#Server.start", "src/util.ts:40-80", "#parse_header"]`.
4. **After editing** — `read "path@etag"` (etag from the `==> path @etag` header):
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
  the same call — use it when you will read those functions anyway.
- Budgets are tokens (default 8000; map 3000; max 10000). Raise
  `budget` rather than paging.

## Scripts and code execution

The CLI mirrors the tools and adds JSON Lines for filtering in code instead of
in context:

```bash
speedread read 'src/app.ts#Server.start' src/util.ts:40-80
speedread search 'AbortWithStatus\(' --output symbols
speedread symbols src --json | jq -r 'select(.kind=="function" and .end-.start>80) | "\(.path):\(.start) \(.qualified)"'
speedread search 'TODO' --json | jq -r .path | sort | uniq -c | sort -rn | head
speedread map --json | jq -s 'map(select(.lines != null)) | sort_by(-.lines) | .[:10]'
```

## Make it the file reader

Agents rarely pick up a new tool on their own: merely installed, speedread was used in 0 of 26 trials, and those runs cost more than not installing it.
Tell the harness: in Copilot CLI start with
`--excluded-tools view grep glob` (edit/bash stay), or add to your project
instructions: "Use the speedread tools to read, search and navigate code."
In Claude Code, Edit/Write still require one native Read of the file first.
