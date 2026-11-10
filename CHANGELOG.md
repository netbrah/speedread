# Changelog

Notable changes by release. Before 1.0, minor versions may change tool arguments and output formats.

## Fork (`netbrah/speedread`, on upstream `37abea2`)

Not part of an upstream release. Details and measurements: [docs/FORK.md](docs/FORK.md), [docs/specs/](docs/specs/README.md).

- Paths: `--base-dir`, `--no-cwd-root` (`SPEEDREAD_NO_CWD_ROOT=1`) and MCP `base_dir` on every tool. Relative paths and path-less `search`/`map` resolve against a stated base, never an assumed working directory; a server with no root refuses path-less requests instead of walking from `/`. Definition and usage lookups run in the root that contains the target.
- Limits: `--max-walk`, `--deadline-ms` (`SPEEDREAD_MAX_WALK`, `SPEEDREAD_DEADLINE_MS`), and MCP `notifications/cancelled` stops walks. An answer cut short ends with `incomplete: …`; silent caps are reported. Reading a named file never walks the tree.
- Memory: `--cache-mb` (`SPEEDREAD_CACHE_MB`, default 512) bounds cached contents and outlines by bytes; the outline cache was bounded by count.
- `search output=scopes`: hits plus the header lines of the enclosing loops, conditions and branches.
- `search ast=true` (`--ast`, `--lang`): structural patterns with metavariables (`$X.unwrap()`, `router.$METHOD($$$ARGS)`), through `ast-grep-core`. Opt-in; parses every candidate file.
- `trace`: call sites inside closures are marked `λ`; places a function is passed, assigned or returned as a value are listed with `→`.
- Linux: clippy passes with `-D warnings` (macOS-only code gated).

## Unreleased

- Luau: outlines, `path#Symbol`, function-grouped `search` hits and `trace` for `.luau` files, and for `.lua` files that use Luau syntax (recognized by a `--!strict` directive, type aliases or annotations, or because only Luau parses them). Covers type aliases, attributes such as `@native`, and functions in table constructors.
- Lua and Luau: `function M.f()` is named `f` in scope `M`, and `function C:m()` is a method `m` of `C`. `#m`, `#C.m` and `#C:m` all find it, and `trace` resolves `obj:m()` calls. Calls through the standard libraries and Roblox globals (`table.insert`, `task.spawn`, `Instance.new`) are no longer matched to workspace functions of the same name.
- `read path:LINE` outside any symbol (top-level code, or a language without a grammar) returns the enclosing block, found from indentation and closing lines such as `end` and `}`, instead of a fixed 81-line window.

## 0.1.0 (2026-09-26)

First public release. Release notes: [docs/releases/v0.1.0.md](docs/releases/v0.1.0.md).

- MCP server (stdio) and CLI with four primitives: `map`, `search`, `trace` and `read`.
- `read`: batched targets under one token budget: files, ranges, `path:LINE` (the enclosing symbol), `path#Symbol`, `#Symbol`, Markdown sections, JSON/YAML/TOML keys and globs. Oversized files degrade to skeletons and outlines; `path@etag` returns only what changed, with symbol-labelled diffs.
- `search`: hits grouped under their enclosing function or class; `output=symbols` and `output=files`.
- `trace`: callers (depth 1–3), callees, references and implementations, syntactic and receiver-aware.
- `map`: budgeted, importance-weighted repository tree, with top-level symbols on request.
- Content-aware token estimator, and tokenizer profiles (`SPEEDREAD_TOKENIZER=claude|openai|legacy`).
- macOS: `getattrlistbulk` walker, performance-core thread pools, iCloud dataless files fail fast.
- Evals: tool scenarios, budget contract, tokenizer calibration, and real-agent code questions, relationship questions and bug fixes, with raw data.
- Distribution: a prebuilt macOS arm64 binary, a Claude Desktop bundle (`.mcpb`), and publishing to the MCP Registry as `io.github.brennengreen/speedread`.
