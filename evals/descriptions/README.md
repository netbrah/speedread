# Description variants

Tool descriptions the model sees are product behavior. The shipped text lives
only in `src/server.rs` (`INSTRUCTIONS`, `READ_DESC`, `SEARCH_DESC`,
`MAP_DESC`, `TRACE_DESC`). This directory does not copy that text.

A variant is one JSON file here, `<id>.json`:

```json
{
  "instructions": "optional replacement of the server instructions",
  "tools": {
    "read": "optional replacement of that tool's description",
    "search": "...",
    "map": "...",
    "trace": "..."
  }
}
```

Omitted tools keep the shipped description. An unknown tool name, or a file
that sets neither field, makes `speedread mcp` exit before it serves. The
server does this only when `SPEEDREAD_DESCRIPTION_FILE` points at the file.
An ordinary launch does not set that variable, and `tools/list` stays
byte-identical to `tests/fixtures/default_tool_text.json`.

The id `current` is not a file. It means "do not set the variable".

## Slot for a shorter description

The sibling track that is shrinking the tool definitions should drop its text
in as `evals/descriptions/lean.json` and run it as `--description lean`.
Do not edit the constants in `server.rs` in that same change. This directory
intentionally has no lean file yet.

Callers (the Copilot runner and the Grok runner) pass `--description <id>`.
`evals/desc_variant.py` is the only code that turns that id into an env var
or an MCP config. Variant prose has one owner: the JSON file.
