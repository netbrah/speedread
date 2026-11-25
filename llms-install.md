# Installing speedread (instructions for AI agents)

These steps install the **netbrah fork** of speedread: a fork of [https://github.com/brennengreen/speedread](https://github.com/brennengreen/speedread) by Brennen Green. The MIT license is preserved. speedread is an MCP server (stdio) and CLI for macOS and Linux.

The current release is `v0.2.1-netbrah.1`, a full release, so GitHub's `releases/latest` selects it.

## 1. Install the binary

On Cursor, Claude, or Grok, prefer the plugin at [`plugins/speedread-netbrah/`](plugins/speedread-netbrah/). Per-host commands are in [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md). The plugin starts the server with `--unrestricted`.

Otherwise use the first option that works:

1. Prebuilt, macOS on Apple Silicon (`sha256.txt` is on the same release; Intel macOS uses `speedread-x86_64-apple-darwin.tar.gz`):
   ```sh
   mkdir -p ~/.local/bin
   curl -fsSL https://github.com/netbrah/speedread/releases/download/v0.2.1-netbrah.1/speedread-aarch64-apple-darwin.tar.gz | tar -xz -C ~/.local/bin
   ```
   Make sure `~/.local/bin` is on `PATH`.
2. Prebuilt, Linux x86_64, static musl (arm64 uses `speedread-aarch64-unknown-linux-musl.tar.gz`):
   ```sh
   mkdir -p ~/.local/bin
   curl -fsSL https://github.com/netbrah/speedread/releases/download/v0.2.1-netbrah.1/speedread-x86_64-unknown-linux-musl.tar.gz | tar -xz -C ~/.local/bin
   ```
3. From source, macOS or Linux (Rust 1.90+): `cargo install --locked --git https://github.com/netbrah/speedread`
4. Upstream Homebrew tap only (builds Brennen Green's repository, not this fork): `brew install brennengreen/tap/speedread`. Use the full name: `brew install speedread` installs an unrelated program.

Check: `speedread --version` prints `speedread 0.2.1-netbrah.1` or a later netbrah tag.

## 2. Register the MCP server

The server command is `speedread mcp`. It needs no API keys or environment variables. The plugin keeps `--unrestricted` in its launch args. A server is unfenced without that flag as of `0.2.0-netbrah.3`; the flag is still accepted. Hand-written configs below pass `mcp` only.

| Client | How |
|---|---|
| GitHub Copilot CLI | `copilot mcp add speedread -- speedread mcp` |
| Claude Code | `claude mcp add --scope user speedread -- speedread mcp`, or the plugin in [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md) |
| Cursor | the plugin at [`plugins/speedread-netbrah/`](plugins/speedread-netbrah/) ([steps](docs/DISTRIBUTION.md#cursor)). Hand config: `{"mcpServers": {"speedread": {"command": "speedread", "args": ["mcp"]}}}` |
| Cline, Gemini CLI | add `{"mcpServers": {"speedread": {"command": "speedread", "args": ["mcp"]}}}` to the client's MCP settings |
| VS Code | `.vscode/mcp.json`: `{"servers": {"speedread": {"type": "stdio", "command": "speedread", "args": ["mcp"]}}}` |
| Codex CLI | `~/.codex/config.toml`: `[mcp_servers.speedread]` with `command = "speedread"` and `args = ["mcp"]` |
| Claude Desktop | open `speedread-aarch64-apple-darwin.mcpb` from the `v0.2.1-netbrah.1` release (Apple Silicon). This fork's bundle does not ask for a folder. |

GUI apps may not inherit the shell's `PATH`; use the absolute path from `which speedread`.

## 3. Make it the reader

speedread saves tokens only when the agent uses it instead of the built-in read and search tools. In the upstream evals, installing it without that made runs more expensive. Either remove the built-in readers (Copilot CLI: `copilot --excluded-tools view grep glob`; Claude Code: `claude --disallowedTools Grep Glob`, keeping `Read` because `Edit` requires it), or add this to the project's `AGENTS.md`, `CLAUDE.md`, `.clinerules` or `.github/copilot-instructions.md`:

```markdown
## Reading code
Use the speedread MCP tools to read, search and navigate code; use built-in tools only to edit and run commands.
- `read` everything you need (files, `path:A-B`, `path#Name`, `#Name`) in ONE call; expand skeletons with `path#Name`.
- `search` finds text (hits show their enclosing function); batch the enclosing symbols into one `read`.
- After editing, `read path@etag` to see only what changed.
```

## 4. Verify

Call the `map` tool on the workspace: it returns a budgeted tree of the repository. In this fork the server reads any path a call names; started with `--restricted`, it refuses reads outside the workspace roots.
