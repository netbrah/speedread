# Installing and publishing the fork

[netbrah/speedread](https://github.com/netbrah/speedread) is a fork of [https://github.com/brennengreen/speedread](https://github.com/brennengreen/speedread) by Brennen Green. The MIT license is preserved ([LICENSE](../LICENSE)). The current release, `v0.2.1-netbrah.1`, is a full release of this fork, not an upstream release.

One plugin, `speedread-netbrah`, for Claude Code, Claude Cowork, Cursor, Grok Build and
Perplexity Computer, plus an `.mcpb` bundle for Claude Desktop chat. It installs beside
upstream's `speedread` and asks for no folder. Design, sources and the test log:
[specs/07-distribution.md](specs/07-distribution.md). The plugin itself:
[plugins/speedread-netbrah/](../plugins/speedread-netbrah/README.md).

The plugin is on the default branch, so the commands below name only the repository and
follow `main`. To stay on one release instead, pin its tag: `netbrah/speedread#v0.2.1-netbrah.1`
for Claude, `netbrah/speedread@v0.2.1-netbrah.1#plugins/speedread-netbrah` for Grok. Either
way the binary is the one the launcher pins, checked by SHA-256.

## Install

### Claude Code

```
claude plugin marketplace add netbrah/speedread
claude plugin install speedread-netbrah@netbrah-speedread
```

The same two commands work inside a session as `/plugin marketplace add …` and
`/plugin install …`. `claude mcp list` should then show
`plugin:speedread-netbrah:speedread … Connected`.

### Claude Cowork

Customize > Plugins > upload `speedread-netbrah-plugin.zip` from the
[release](https://github.com/netbrah/speedread/releases/tag/v0.2.1-netbrah.1), or
Add marketplace > `netbrah/speedread`. The tools appear in Cowork sessions
that run on your computer.

### Claude Desktop (chat)

Chat does not start a plugin's local server, so use the bundle: download
`speedread-aarch64-apple-darwin.mcpb` from the release and open it (Apple Silicon). It no
longer asks for a folder. If macOS refuses to open it:
`xattr -d com.apple.quarantine speedread-aarch64-apple-darwin.mcpb`.

### Cursor

For one machine, put the plugin folder where Cursor looks for local plugins and reload:

```
git clone --depth 1 --branch v0.2.1-netbrah.1 https://github.com/netbrah/speedread /tmp/speedread-netbrah
mkdir -p ~/.cursor/plugins/local
cp -R /tmp/speedread-netbrah/plugins/speedread-netbrah ~/.cursor/plugins/local/
```

Then restart Cursor or run "Developer: Reload Window". (A symlink to a folder outside
`~/.cursor/plugins/local` is skipped, hence the copy.) The server shows up under MCP as
`plugin-speedread-netbrah-speedread`. A Teams or Enterprise workspace can instead import
`netbrah/speedread` as a team marketplace; the catalog is `.cursor-plugin/marketplace.json`.

### Grok Build

```
grok plugin install netbrah/speedread#plugins/speedread-netbrah --trust
```

or, to browse it as a marketplace: `grok plugin marketplace add netbrah/speedread`, then
`grok plugin install speedread-netbrah --trust`. `--trust` is what
lets Grok start the plugin's MCP server. `grok mcp doctor` should report the `speedread`
server healthy with 4 tools.

### Perplexity Computer

Plugins > Add custom > Import plugin from GitHub, and paste the plugin folder's URL:

```
https://github.com/netbrah/speedread/tree/v0.2.1-netbrah.1/plugins/speedread-netbrah
```

or Upload plugin with `speedread-netbrah-plugin.zip` from the release. Perplexity does not
start local MCP servers; it installs the skill, which runs the same binary as a command
line inside Computer's sandbox (`sh scripts/speedread.sh search …`).

### Anything else that speaks MCP

Download the tarball for your platform from the release and register it by hand:

```json
{ "mcpServers": { "speedread": { "command": "/path/to/speedread", "args": ["mcp", "--no-cwd-root"] } } }
```

Use `"args": ["mcp", "--restricted", "--root", "/path/to/project"]` instead to confine it
to one folder. `--root` alone no longer confines a server: it only says where relative
paths resolve ([specs/08](specs/08-unfenced-server.md)).

## What "no folder" means

The server is started with `--unrestricted --no-cwd-root`; since `0.2.0-netbrah.3` a
server is unfenced without the first flag, which the packages keep so that they also work
with an older binary. It reads any path your account
can read when the agent asks for one, and it never searches a directory it was not pointed
at: the agent names files by absolute path, or passes `base_dir`. A call with neither is
refused with what to pass. A host that sends its workspace folders over MCP (Claude Code
does) also gets relative paths resolved against them. What is given up is the fence that
kept the server inside one chosen folder; the host's tool-approval prompt is the control
that remains. To have the fence, register the binary yourself with `--restricted --root`.

## Platforms

| Platform | Plugin | `.mcpb` |
|---|---|---|
| macOS, Apple Silicon | yes | yes |
| macOS, Intel | yes | no |
| Linux x86_64, arm64 (incl. WSL) | yes | no |
| Windows | no | no |

The plugin is the same files everywhere; its launcher downloads the binary for the machine
on first use (about 6 MB, from this repository's release, checked against a SHA-256 in the
launcher). `SPEEDREAD_BIN`, `SPEEDREAD_NO_DOWNLOAD` and `SPEEDREAD_RELEASE_URL` change that;
see the [plugin README](../plugins/speedread-netbrah/README.md).

## Curated directories

Self-hosting (above) needs nobody's approval. A listing in someone else's directory needs
the repository owner's account and that directory's review. None has been submitted.

| Directory | How | Expect |
|---|---|---|
| Anthropic plugin directory | sign in at https://claude.ai/directory/manage (paid plan), submit the repository and the folder `plugins/speedread-netbrah` | the automated check holds plugins that download or run a binary for a human reviewer; the plugin README states what is downloaded and from where |
| Cursor Marketplace | submit the repository link at https://cursor.com/marketplace/publish | manual review |
| MCP Registry | the release workflow writes `server.json` as `io.github.netbrah/speedread` and runs `mcp-publisher` (`.github/workflows/mcp-registry.yml`). Upstream's name `io.github.brennengreen/speedread` is unchanged. By hand: `mcp-publisher login github` as `netbrah`, then `mcp-publisher publish` | automated; name ownership is proved by the GitHub login |
| Perplexity | none: the public catalog takes no submissions. Share within an organization with Transfer to organization on the plugin's page | — |
| Grok Build | none found: distribution is by repository | — |
| cursor.directory, Glama and similar community lists | their own forms | — |

## Cutting a release

1. Bump `version` in `Cargo.toml` (`0.2.0-netbrah.N`), `cargo build`, and update the
   version in the three plugin manifests and two marketplace files, and the tag in the
   links they and the plugin README carry (`rg --hidden 0.2.0-netbrah`).
2. Write `docs/releases/v<version>.md` and a `CHANGELOG.md` entry. Commit.
3. `scripts/fork-release.sh v<version>` — builds four targets and the bundle into
   `target/fork-release/`, and writes the version and checksums into the launcher.
4. `scripts/test-launcher.sh`, then commit the launcher. Tag that commit.
5. `scripts/plugin-zip.sh` — zips the plugin folder's contents (`plugin.json` at the zip
   root) as `speedread-netbrah-plugin.zip` and adds its checksum to `sha256.txt`.
6. Push the tag. `.github/workflows/ci.yml` builds the four targets, `sha256.txt`, the
   plugin zip and the `.mcpb`, and publishes a full release (not a pre-release) marked
   latest. `gh release create` by hand is the fallback when Actions cannot run; do not
   pass `--prerelease`.
7. From a clean cache, run the launcher against the real release, and install from the
   tag in each host you can run (the commands above).

Needs `cargo-zigbuild`, `zig`, the four Rust targets, `python3`, ImageMagick and `npx`.
