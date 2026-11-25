# Description variants

Tool descriptions the model sees are product behavior. The shipped text lives
only in `src/server.rs` (`INSTRUCTIONS`, `READ_DESC`, `SEARCH_DESC`,
`MAP_DESC`). This directory does not copy that text. The harness covers
`read`, `search`, and `map`. `trace` is being removed in another PR;
`context` did not land. A variant may omit both. If a file still names
either, the loader drops that key before the server reads it.

A variant is one JSON file here, `<id>.json`:

```json
{
  "instructions": "optional replacement of the server instructions",
  "tools": {
    "read": "optional replacement of that tool's description",
    "search": "...",
    "map": "..."
  }
}
```

Omitted tools keep the shipped description. An unknown tool name other than
`trace` or `context`, or a file that sets neither field, makes `speedread mcp`
exit before it serves. The server does this only when
`SPEEDREAD_DESCRIPTION_FILE` points at the file. An ordinary launch does not
set that variable. The lock for the three tool descriptions and the
instructions is `tests/fixtures/default_tool_text.json`.

The id `current` is not a file. It means "do not set the variable".

## Slot for a shorter description

The sibling track that is shrinking the tool definitions should drop its text
in as `evals/descriptions/lean.json` and run it as `--description lean`.
Do not edit the constants in `server.rs` in that same change. This directory
intentionally has no lean file yet.

Callers (the Copilot runner and the Grok runner) pass `--description <id>`.
`evals/desc_variant.py` is the only code that turns that id into an env var
or an MCP config. Variant prose has one owner: the JSON file.
