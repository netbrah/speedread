# speedread (netbrah fork) — plugin

> **Fork of [https://github.com/brennengreen/speedread](https://github.com/brennengreen/speedread) by Brennen Green.** The MIT license is preserved. Maintained by netbrah.

One plugin folder for Claude Code, Claude Cowork, Cursor, Grok Build and
Perplexity Computer. It gives the agent four read-only tools for code —
`read`, `search`, `trace`, `map` — that return the part of a codebase a
question needs within a token budget: the function around each search hit, a
large file's skeleton, a symbol's callers and implementations, or only what
changed since the last read. What the fork changes is in [docs/FORK.md](https://github.com/netbrah/speedread/blob/v0.2.0-netbrah.3/docs/FORK.md).

## What is in the folder

| File | Read by | Purpose |
|---|---|---|
| `.claude-plugin/plugin.json` | Claude Code, Claude Cowork, Grok Build (fallback) | manifest |
| `.cursor-plugin/plugin.json` | Cursor | manifest |
| `plugin.json` | Perplexity Computer, Grok Build, Agent Plugins clients | manifest |
| `.mcp.json` | Claude Code, Claude Cowork, Grok Build | starts the local server |
| `mcp.json` | Cursor | starts the local server |
| `skills/speedread/SKILL.md` | every host | when and how to use the tools or the CLI |
| `skills/speedread/scripts/speedread.sh` | every host | launcher: fetches and runs the binary |

## Use

Install it (commands per host are in
[docs/DISTRIBUTION.md](https://github.com/netbrah/speedread/blob/v0.2.0-netbrah.3/docs/DISTRIBUTION.md)), then ask about code. There
is nothing to configure and no folder to choose: the agent names files by
absolute path, or passes `base_dir` to scope a call to one project. Example
prompts: "map /path/to/repo", "who calls `parse_header` in /path/to/repo?",
"read `src/server.rs#Server.start` and what it calls".

Hosts that start local MCP servers (Claude Code, Cowork sessions on your
computer, Cursor, Grok Build) get the four tools. Perplexity Computer does not
start local servers, so there the skill drives the same binary as a command
line inside its sandbox.

## What it does on your machine, and what it sends

- **Reads** any file your account can read, when the agent asks for it. The
  server has no folder fence (`speedread mcp` reads any path a call names
  unless started with `--restricted`), and it never searches a directory it was not pointed at. Your host's
  tool-approval prompt is the control over which calls run. It never writes to
  the files it reads.
- **Downloads once.** The plugin contains no executable code besides the
  launcher script. On first use the launcher downloads the release binary for
  your platform from
  `https://github.com/netbrah/speedread/releases/download/v<version>/`, checks
  it against a SHA-256 written in the script, and caches it in the host's
  plugin data directory (else `~/.cache/speedread-netbrah`). A mismatch
  installs nothing. That request to GitHub is the only network traffic.
- **Sends nothing else.** The binary makes no network connections and has no
  telemetry. What it reads goes to the agent that asked, in the tool result.

To skip the download, put a `speedread` binary of the same version on the
machine and set `SPEEDREAD_BIN` to its path, or set `SPEEDREAD_NO_DOWNLOAD=1`
to forbid the network. Behind a firewall, mirror the release files and set
`SPEEDREAD_RELEASE_URL` to the mirror's base URL; the checksum still has to
match.

## Platforms

macOS (Apple Silicon and Intel) and Linux (x86_64 and arm64, static). Not
Windows; WSL counts as Linux.

## License

MIT, preserved from upstream. Copyright (c) 2026 Brennen Green, and fork changes Copyright (c) 2026 netbrah. See [LICENSE](../../LICENSE).
