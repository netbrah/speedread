# 07 — Distribution: one plugin for every agent host

Status: shipped in `0.2.0-netbrah.2`. Install commands and the release procedure are in
[docs/DISTRIBUTION.md](../DISTRIBUTION.md); this file records what was decided, why, what
each host actually reads, and what was and was not tested.

## Problem

The fork was installable in one place (Claude Desktop, via an `.mcpb` bundle that asked for a
folder at install time). It should be installable, with no configuration step, in Claude
Code, Claude Cowork, Claude Desktop, Cursor, Grok Build and Perplexity Computer.

Two questions decide the design:

1. **Does the install have to ask for a folder?** No. The folder was a fence, not a
   requirement: spec [01](01-path-and-scope-contract.md) already makes every call
   addressable by absolute path or `base_dir`.
2. **Does every platform need its own build of the plugin?** No. A plugin is text
   (manifests, a skill, MCP configuration). Only the `speedread` binary is per-platform, and
   no host's plugin format is a supported way to ship one (see the table below).

## Decisions

### D1. One launch configuration everywhere: `mcp --unrestricted --no-cwd-root`

| Flag | Effect | Why |
|---|---|---|
| `--unrestricted` | any path the account can read is allowed | no host can be relied on to supply a folder: Cowork does not prompt for plugin configuration, Cursor and Grok have no equivalent, and a server started by a desktop app has no meaningful working directory |
| `--no-cwd-root` | the launch directory is never adopted as the workspace | a path-less `search`/`map` is refused with what to pass (`base_dir`) instead of walking `/` or the app's install directory |

Started this way, the server's instructions gain one sentence telling the model to use
absolute paths or `base_dir` (`ROOTLESS_NOTE` in `src/server.rs`, tested in
`tests/mcp.rs::a_server_with_no_folder_reads_by_absolute_path_and_says_so`). A server that
has a root is told nothing new, so existing configurations behave as before.

The sentence is decided at startup, before the client has said whether it has roots. A
client that later supplies MCP roots therefore still sees it;
relative paths then resolve against those roots, and the advice to use absolute paths
stays correct. `CLAUDE_PROJECT_DIR`, when a host sets it, is known at startup: it becomes
the base directory and the sentence is not added.

Cost, stated plainly: the folder fence is gone. What remains is the host's own tool-approval
prompt and the fact that speedread is read-only. Anyone who wants the fence back runs
`speedread mcp --root <DIR>` from a hand-written MCP entry; the binary is the same.

Client-supplied MCP roots still work: a host that sends `roots` (Claude Code does) gets
relative paths resolved against them, with no change in the plugin.

### D2. The plugin is text; a launcher fetches the binary

`plugins/speedread-netbrah/skills/speedread/scripts/speedread.sh` (POSIX `sh`) runs the
release binary for the machine, downloading it once from the fork's GitHub release and
checking it against a SHA-256 written in the script. Why not ship the binary in the plugin:

| Host | What its documentation says |
|---|---|
| Claude (claude.ai, Cowork) | a top-level `bin/` directory makes the plugin uninstallable there; the directory's reviewer holds any compiled binary for manual review |
| Grok Build | "Marketplaces distribute Grok content … They do not install a program onto a machine" |
| Cursor | plugins are cloned from Git; no per-platform artifact mechanism |
| Perplexity Computer | local (`stdio`) servers are skipped on import; a 20 MB package limit |

Four platforms at about 6 MB each would also put 24 MB of binaries in Git for every release.

The launcher's contract (each line is a check in `scripts/test-launcher.sh`):

- `SPEEDREAD_BIN`, if set, is run as is; if it does not exist that is an error, not a fallback.
- Otherwise the cached copy at `<data>/<version>/speedread`, where `<data>` is
  `SPEEDREAD_PLUGIN_DATA` (the MCP configuration passes the host's plugin data directory),
  else `CLAUDE_PLUGIN_DATA`, `GROK_PLUGIN_DATA`, `PLUGIN_DATA`, else
  `${XDG_CACHE_HOME:-~/.cache}/speedread-netbrah`. A literal, unexpanded `${…}` is ignored.
- Otherwise download `speedread-<target>.tar.gz` for the pinned version, compare SHA-256,
  unpack, and `rename` into place (atomic, so two sessions starting together are safe). A
  mismatch installs nothing. `SPEEDREAD_NO_DOWNLOAD=1` forbids the network;
  `SPEEDREAD_RELEASE_URL` points at a mirror, and the checksum still has to match.
- Otherwise a `speedread` on `PATH`, only if `--version` reports this fork at this version
  (upstream does not have `--no-cwd-root` or `base_dir`).
- Nothing is written to stdout, which is the MCP channel. Errors go to stderr with the URL
  and the manual fix.

The launcher lives inside the skill folder, not at the plugin root, because Perplexity
imports skills (folder and supporting files) and nothing else; there the skill is the whole
product and has to carry its own way to get the binary.

MCP configurations start it as `sh <path>`, not as an executable, so a checkout or zip
upload that lost the executable bit still works.

### D3. One folder, several manifests

`plugins/speedread-netbrah/` carries every host's manifest side by side. They do not
conflict because each host looks for its own file:

| File | Host | Notes |
|---|---|---|
| `.claude-plugin/plugin.json` | Claude Code, Cowork, Desktop | also Grok's third choice |
| `.mcp.json` | Claude Code, Cowork, Grok Build | `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}`; Grok expands both names and its own `GROK_*` aliases (`xai-grok-config/src/mcp_servers.rs::plugin_substitute`) |
| `.cursor-plugin/plugin.json` | Cursor | Cursor Plugin format |
| `mcp.json` | Cursor, Agent Plugins clients | `${CURSOR_PLUGIN_ROOT}` in `args`; Cursor does not expand the standard's `${PLUGIN_ROOT}` |
| `plugin.json` | Perplexity Computer, Grok Build (first choice), Agent Plugins clients | Agent Plugins 1.0.0 schema; Grok ignores fields it does not know |
| `skills/speedread/` | all | the only part Perplexity uses |

Not used, on purpose: `bin/` (blocks claude.ai and Cowork), `${user_config.*}` and
`userConfig` (Cowork does not prompt; nothing needs configuring), hooks, commands, agents.

Repo-root catalogs make the repository its own marketplace: `.claude-plugin/marketplace.json`
(Claude Code, Cowork, Grok Build) and `.cursor-plugin/marketplace.json` (Cursor). Both point
at `./plugins/speedread-netbrah`.

Name: `speedread-netbrah` everywhere, so it installs beside upstream's `speedread`
without replacing it. Version: the crate's, `0.2.0-netbrah.N`.

### D4. Platforms

| Target | Asset | Built | Run before release |
|---|---|---|---|
| `aarch64-apple-darwin` | tarball, `.mcpb` | cross-compiled on Linux (`cargo-zigbuild`) | no (no Mac available) |
| `x86_64-apple-darwin` | tarball | cross-compiled | no |
| `x86_64-unknown-linux-musl` | tarball | cross-compiled, static | yes |
| `aarch64-unknown-linux-musl` | tarball | cross-compiled, static | see the test log below |

Linux builds are static musl so one binary runs on any distribution, including the Linux
virtual machines some hosts run sessions in. Windows is not built: the code is Unix-only
upstream; WSL is Linux. The `.mcpb` stays Apple Silicon only, as upstream ships it; an Intel
Mac uses the plugin or the tarball.

### D5. Tag after pinning

The launcher downloads by tag and must already know the checksums, so the order is: build
from commit X, write the checksums into the launcher (commit Y, text only), tag Y. The
binaries are therefore built from the tag's parent. `scripts/fork-release.sh` does the
build and the pin; the release notes say so.

## What each host loads

| Host | Skill | Local MCP server | Marketplace it can add | Curated directory |
|---|---|---|---|---|
| Claude Code | yes | yes | any Git repo, `owner/repo#ref`, URL, local path | Anthropic's, by submission |
| Claude Cowork | yes | yes, when the session runs on the person's computer | GitHub repo, or a zip upload | same |
| Claude Desktop chat | yes | no (plugin servers are ignored in chat; use the `.mcpb`) | same as Cowork | same |
| Cursor | yes | yes | team marketplaces (Teams/Enterprise) import a GitHub repo; `~/.cursor/plugins/local` for one machine | Cursor Marketplace, by submission, reviewed by hand |
| Grok Build | yes | yes, once the plugin is trusted | `owner/repo`, Git URL, local path; reads `.claude-plugin/marketplace.json` | none found; distribution is by repository |
| Perplexity Computer | yes | no (`stdio` servers are skipped) | zip upload or public GitHub folder URL; personal or organization scope | catalog is not self-serve |

## Test log

Recorded when `0.2.0-netbrah.2` was cut. "Not tested" means exactly that.

Before publication, on Linux x86_64 (2 vCPU sandbox):

- **Source.** `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` (Linux, and again for `aarch64-apple-darwin`), and all 98 tests pass, including one new test that starts the server the way every package now does and checks that it reads by absolute path, refuses a path-less call, and says so in its instructions.
- **Linux binaries.** Both are static (no interpreter, no shared libraries). The x86_64 binary run natively and the arm64 binary run under QEMU gave byte-identical answers to the tested build on 15 queries over the ripgrep source (`map`, `search`, AST search, scopes, `trace` callers/implementations/callees, `read` by path, symbol, line range and heading, JSON output). Each, started as an MCP server with the shipped arguments, refused a path-less `map` and answered a `trace` given `base_dir`. Timing on the zod source was within 5% of the tested build.
- **macOS binaries.** Inspected, not run: correct CPU type, minimum macOS 11.0, only system libraries linked, arm64 ad-hoc signed. The binary inside the `.mcpb` is byte-identical to the one in the arm64 tarball; the bundle's manifest has no `user_config` and starts `mcp --unrestricted --no-cwd-root`.
- **Launcher.** 11 checks (`scripts/test-launcher.sh`) under `sh`, `dash` and `bash` against a local mirror: download and cache, second run without the network, a checksum mismatch installs nothing, a failed download names the URL, `SPEEDREAD_BIN`, `SPEEDREAD_NO_DOWNLOAD`, the data-directory fallbacks, and a `speedread` already on `PATH` used only if its version matches. `shellcheck` clean.
- **Manifests.** `claude plugin validate` (plugin, marketplace and the unpacked zip), `grok plugin validate`, Cursor's `validate-template.mjs`, and the Agent Plugins 1.0.0 JSON schemas all pass.
- **Claude Code 2.1.197** (installed from a local checkout): marketplace add, plugin install, `claude mcp list` shows the server connected.
- **Grok Build 1.0.77** (installed from a local checkout): install with `--trust`, `grok mcp doctor` reports the server started, handshake completed, 4 tools found.

After publication, against the files on this release page (downloaded without credentials; all seven match `sha256.txt`):

- **Launcher, real download.** Unpacked from `speedread-netbrah-plugin.zip` with no executable bit and an empty cache, it fetched the x86_64 Linux binary, verified it, and served MCP `initialize`, a refused path-less `map`, and `map`, `read` and `search` by absolute path and `base_dir`. The same launcher ran the command line (`--unrestricted search …`, `--root DIR search …`), which is how the skill works in Perplexity's sandbox.
- **Claude Code 2.1.197, from GitHub.** `claude plugin marketplace add netbrah/speedread#v0.2.0-netbrah.2`, `claude plugin install speedread-netbrah@netbrah-speedread`, then `claude mcp list`: connected, binary cached under `~/.claude/plugins/data/`.
- **Grok Build 1.0.77, from GitHub.** `grok plugin install netbrah/speedread@v0.2.0-netbrah.2#plugins/speedread-netbrah --trust`, then `grok mcp doctor`: server started, handshake completed, 4 tools found, binary cached under `~/.grok/plugin-data/`.

Not tested: anything on macOS; Claude Desktop and Claude Cowork (no way to run either here); Cursor at runtime (its command line needs a login to manage plugins, so only the manifest validator ran); Perplexity's import dialog.

## Sources

Read on 2026-11-16. Each is here because a decision above rests on it.

**Claude**

- https://claude.com/docs/plugins/build — plugin layout; the "check what each app loads" list (local MCP server: Claude Code and on-computer Cowork sessions; chat ignores it); top-level `bin/` blocks claude.ai and Cowork; zip upload layout.
- https://claude.com/docs/plugins/platform-support — the per-surface table D3 and "What each host loads" are built from.
- https://claude.com/docs/plugins/pre-submission-checklist and https://claude.com/docs/plugins/submit — directory requirements (README of 40+ words, license) and what the automated check holds for a human (binaries, downloads, scripts it cannot follow).
- https://claude.com/docs/cowork/guide/plugins — Customize > Plugins, adding a marketplace, uploading a zip.
- https://code.claude.com/docs/en/plugins/manifest-reference — `plugin.json` fields; which variables are substituted in an MCP `stdio` entry (`CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, `CLAUDE_PROJECT_DIR`).
- https://code.claude.com/docs/en/plugins/marketplace-reference — `marketplace.json` fields and source types.
- https://code.claude.com/docs/en/plugins/host-marketplace — `owner/repo#ref` pinning; claude.ai rejects a top-level `bin/`.
- https://code.claude.com/docs/en/plugins/publish — `claude plugin validate`, `--plugin-dir`.
- https://github.com/anthropics/knowledge-work-plugins — Anthropic's own multi-plugin repository; the layout this repo's `plugins/` + root catalog follows.
- https://github.com/modelcontextprotocol/mcpb — the Claude Desktop bundle format (`packaging/mcpb/manifest.json`).

**Cursor**

- https://cursor.com/docs/plugins — two formats (Agent Plugins, Cursor Plugins), local testing in `~/.cursor/plugins/local`, team marketplaces, publishing.
- https://cursor.com/docs/reference/plugins — manifest fields; `mcp.json` at the plugin root; which variables are expanded.
- https://github.com/cursor/plugin-template — template repository; `scripts/validate-template.mjs` is the validator run here; `plugins/starter-advanced/mcp.json` is the MCP example.
- https://github.com/cursor/plugins — the official marketplace repository, for layout precedent.
- https://github.com/fieldsphere/cursor-team-marketplace-template — template for a team marketplace.
- https://cursor.com/marketplace/publish — the submission form.

**Agent Plugins (open standard)**

- https://agent-plugins.org and https://github.com/agentplugins/agent-plugins-spec — root `plugin.json` and `mcp.json` schemas (`schemas/1.0.0/`), validated here; stdio `command` is a bare name or a `./relative` path, which is why the command is `sh`.

**Grok Build**

- https://docs.x.ai/build/features/skills-plugins-marketplaces — plugins, marketplaces, trust.
- https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/09-plugins.md — the fuller guide: install forms (`owner/repo@ref`, `#subdir`), Claude compatibility, "marketplaces do not install a program".
- https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-agent/src/plugins/manifest.rs — manifest search order (`plugin.json`, `.grok-plugin/plugin.json`, `.claude-plugin/plugin.json`); unknown fields ignored.
- https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-config/src/mcp_servers.rs — plugin MCP variable substitution.

**Perplexity Computer**

- https://www.perplexity.ai/help-center/en/articles/2026100802-create-or-upload-a-custom-plugin — package format (`plugin.json` at the zip root, name and description required), GitHub import by folder URL, limits, `stdio` servers skipped.
- https://www.perplexity.ai/help-center/en/articles/2026100701-what-are-plugins and https://www.perplexity.ai/help-center/en/articles/2026100702-find-install-and-remove-plugins — what a plugin can contain; personal and organization scope.

**MCP Registry**

- https://github.com/modelcontextprotocol/registry and https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/authentication.mdx — an `io.github.<user>/*` name needs that user's GitHub login or Actions OIDC; not publishable by anyone else.

## Not done, and who has to do it

Listings in other people's directories need the owner's account and a review by that
directory. Steps are in [docs/DISTRIBUTION.md](../DISTRIBUTION.md#curated-directories).

- Anthropic's plugin directory (developer portal, paid plan).
- Cursor Marketplace (form; manual review).
- MCP Registry (`mcp-publisher login github`, or enable Actions on the fork).
- Perplexity: personal or organization import only; the public catalog takes no submissions.
