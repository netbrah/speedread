# 08 — The MCP server is not fenced

Status: shipped in `0.2.0-netbrah.3`.

## Problem

`map`, and every other tool, could answer `Outside the workspace: <path>` for a path the
user had every right to read. Whether it did depended on how the server had been started,
which the model calling it cannot see:

| How the server was started | A path outside the folder |
|---|---|
| the fork's plugin or bundle (`mcp --unrestricted --no-cwd-root`) | read |
| `mcp --root DIR`, or the `0.2.0-netbrah.1` bundle with its configured folder | refused |
| `mcp` with no flags, launched inside a project | refused |
| `mcp` with no flags, launched in `/` (what desktop apps do) | refused, after instructions that said to use absolute paths |

All four were reproduced with the released `0.2.0-netbrah.2` binary before this change. The
last row was a contradiction introduced in `0.2.0-netbrah.2`: the "no folder is configured"
sentence was added whenever the server had no root, whether or not it was fenced.

The fence does not buy much for a server. The hosts that start speedread also give the
model a shell or a file-reading tool of their own, and what the model may call is governed
by the host's approval prompt. It does cost something: a refusal the model cannot fix, in
the middle of a task, because the user's question crossed into a second repository.

## Decision

**`speedread mcp` reads whatever path a call names.** A server's folders — `--root`, the
launch directory, `CLAUDE_PROJECT_DIR`, roots sent by the client — decide where relative
paths and path-less `search`/`map` resolve (spec [01](01-path-and-scope-contract.md)).
They no longer decide what may be read.

- This holds however the server is started: `speedread mcp`, `speedread` with a pipe on
  stdin and no subcommand, with or without `--root`.
- `--unrestricted` is still accepted, and for the server changes nothing. Existing
  configurations, including the fork's own plugin and bundle, keep working unedited.
- **`--restricted`** brings the fence back for a server: roots and dependency caches only,
  as before. It is the one way to get it. `--restricted` and `--unrestricted` together are
  an error, not a guess.
- **Commands are unchanged.** `speedread read|search|trace|map|symbols` stay limited to
  their roots unless `--unrestricted` is given. The request was about the server; a
  command is run by someone who chose the directory.

Scope is still never widened silently (invariant 7): a path-less `search` on a server with
a folder searches that folder, not the disk. Only a path the call names itself can reach
outside.

## Messages

Every refusal now says how to lift it. Before, only `read` and `base_dir` did; `map`,
`search`, `trace` and the JSON commands gave the path and nothing else.

```
Outside the workspace: /other/repo (to allow it add --root <dir>, or lift the limit: --unrestricted for commands, remove --restricted from `speedread mcp`)
```

The "No project folder is configured here: give absolute paths …" sentence in the server
instructions is added only when the server is unfenced. A `--restricted` server with no
folder is not told to use absolute paths it would refuse.

## What is given up

A server registered as `mcp --root DIR` used to be unable to read outside `DIR`. After
this change it can, unless `--restricted` is added. Anyone relying on that — speedread as
the only file reader in a chat client, pointed at one folder — has to add the flag. This
is called out in the release notes and in `SECURITY.md`, whose scope now reads "a
`--restricted` server, or a command without `--unrestricted`".

Unchanged: it is read-only, it reads only what the account running it can read, and macOS
privacy protections still apply.

## Tests

`tests/mcp.rs::an_mcp_server_reads_outside_its_folder_unless_restricted`, seen failing on
the first assertion (`map` refused) before the change:

- a server started four ways (`--root DIR mcp`; `mcp` in a project; no subcommand; `mcp`
  in `/`) answers `map`, `read`, `search` (by path and by `base_dir`) and `trace` on a
  sibling directory;
- with a folder, a path-less `search` still covers only that folder;
- launched in `/`, the instructions carry the no-folder sentence and a path-less `map` is
  refused with what to pass;
- `--restricted --root DIR mcp` refuses all five calls, leaks nothing from the file, and
  each refusal names `--restricted`;
- `mcp --restricted` in `/` does not carry the no-folder sentence;
- `--restricted --unrestricted` exits with an error;
- the `map` command, run in a project on a path outside it, is still refused and names
  `--unrestricted`.

`tests/fork_paths.rs::mcp_restricted_keeps_the_fence_for_base_dir` (was
`mcp_default_behaviour_is_unchanged_without_base_dir`) keeps the old refusal under
`--restricted`.

Each assertion was checked by breaking the code it guards and seeing a test fail. Six
changes, six failures: the server left fenced by default; `--restricted` ignored; commands
unfenced as well; the no-folder sentence kept under `--restricted`; the `map` refusal
without its remedy; a server started with no subcommand not recognised as one.
