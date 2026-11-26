#!/usr/bin/env python3
"""Adoption from transcripts: which tools an agent actually called.

Trial-level and step-level definitions for the description A/B harness.
The numbers come from tool-call events, not from the model's own claim
that it used a tool.

Both runners feed this module:
  - GitHub Copilot CLI transcripts name tools ``speedread-read`` (one hyphen).
  - Grok CLI names MCP tools ``speedread__read`` (two underscores), usually
    inside a ``use_tool`` call whose ``tool_name`` is that catalog key.
  - A later speedread tool counts as soon as its name starts with
    ``speedread-`` or ``speedread__``.

A trial **used speedread** when any code-reading or other step's effective
tool is a speedread tool. ``search_tool`` (discovering the catalog) does not
count. A failed or denied call still counts: the model asked for the tool.

Step-level shares are over **code-reading steps** only. One tool call is one
step. Classes:

  speedread   a speedread tool (including via ``use_tool``)
  native      built-in file readers: Copilot ``view`` / ``grep`` / ``glob`` /
              ``rg``, Grok ``read_file`` / ``grep`` / ``list_dir`` / ``glob``
  shell       a shell step whose command is a reader (see below)
  other       not a code-reading step

Shell classification (the whole step, not each pipeline segment):

  1. Split the command on ``&&``, ``||``, ``;``, ``|``, and newlines, but not
     inside single or double quotes.
  2. If any segment is a test, build, or VCS write (``go test``, ``pytest``,
     ``cargo test``, ``git commit``, …), the step is **other**. A ``cat``
     chained with ``pytest`` is an execution step, not a read. This matches
     the previous coding harness, which vetoed the step when a runner matched.
  3. Otherwise, if any segment's command word is a reader, the step is
     **shell**. Readers: ``cat head tail sed less more grep rg ag find ls tree
     nl awk wc bat gawk nawk mawk fd exa eza tac``, plus ``git grep``,
     ``git ls-files``, ``git ls-tree``, ``git show``, and ``git diff``.
     ``sed -i`` / ``sed --in-place`` is an edit, not a reader.
  4. Otherwise the step is **other**.

Not code-reading steps: edits (``search_replace``, ``edit``, ``write``, …),
``search_tool``, web tools, subagents, tests, builds, and shell commands that
do not read source (``echo``, ``mkdir``, ``git commit``).
"""

import json
import re
import sys
from pathlib import Path

# Copilot prefixes the MCP server name with one hyphen. Grok uses two underscores.
_SPEEDREAD_PREFIXES = ("speedread-", "speedread__")

NATIVE_READ = {
    "view", "grep", "glob", "rg",
    "read_file", "list_dir", "glob_file_search",
}

# Grok 1.0.92's live shell id is run_terminal_command (Bash maps to that name).
SHELL_TOOLS = {"bash", "shell", "run_terminal_command", "run_terminal_cmd"}

EDIT_TOOLS = {
    "edit", "create", "str_replace_editor", "write", "apply_patch", "str_replace",
    "search_replace",
}

# First token of a shell segment, after env assignments and sudo.
_READERS = {
    "cat", "head", "tail", "sed", "less", "more", "grep", "rg", "ag", "find",
    "ls", "tree", "nl", "awk", "wc", "bat", "gawk", "nawk", "mawk", "fd",
    "exa", "eza", "tac",
}

_GIT_READS = {"grep", "ls-files", "ls-tree", "show", "diff"}

# A segment that matches is an execution / edit, and vetoes the whole step.
_RUNNER = re.compile(
    r"^(?:go\s+(?:test|build|vet|run|fmt)|pytest|python3?\s+-c|cargo\s+(?:test|build|check|run|clippy|fmt)"
    r"|npm\s+(?:test|run)|pnpm(?:\s|$)|yarn\s+test|make(?:\s|$)|ctest(?:\s|$)"
    r"|git\s+(?:commit|add|push|pull|checkout|switch|merge|rebase|reset|stash))\b"
)

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def effective_name(tool, args=None):
    name, _ = _unwrap_use_tool(tool, args or {})
    return name


# Only these three count as adoption. trace is leaving; context (PR #6) did not land.
# Any other speedread leaf is recorded, not counted.
_COUNTED_LEAVES = {"read", "search", "map"}


def speedread_leaf(name):
    """The tool leaf after a speedread prefix, or None if this is not a speedread tool."""
    if not name or not isinstance(name, str):
        return None
    n = name.strip()
    for prefix in ("mcp__speedread__", "mcp_speedread_", "speedread__", "speedread-"):
        if n.startswith(prefix):
            return n[len(prefix):]
    return None


def is_speedread_name(name):
    return speedread_leaf(name) in _COUNTED_LEAVES


def _unwrap_use_tool(tool, args):
    """Grok calls MCP tools through use_tool(tool_name=server__tool, tool_input=…)."""
    if tool != "use_tool" or not isinstance(args, dict):
        return tool, args or {}
    inner = args.get("tool_name") or args.get("toolName") or args.get("name")
    if not inner or not isinstance(inner, str):
        return tool, args
    payload = args.get("tool_input") or args.get("toolInput") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"_raw": payload}
    if not isinstance(payload, dict):
        payload = {}
    return inner, payload


def shell_command(args):
    if not isinstance(args, dict):
        return ""
    cmd = args.get("command") or args.get("cmd") or args.get("cmd_line") or ""
    return cmd if isinstance(cmd, str) else ""


def split_segments(command):
    """Split on shell operators outside quotes. Quotes are not fully parsed
    (no ``$()``), which is enough to keep ``echo "a && b"`` as one segment."""
    parts, buf = [], []
    quote = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if c == quote and command[i - 1] != "\\":
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if c in (";", "|", "\n"):
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _command_words(segment):
    words = segment.split()
    while words and (_ENV_ASSIGN.match(words[0]) or words[0] in {"sudo", "command", "exec", "time"}):
        words = words[1:]
    if not words:
        return []
    # "/usr/bin/cat" → "cat"
    words[0] = words[0].rsplit("/", 1)[-1]
    return words


def _segment_is_runner(segment):
    words = _command_words(segment)
    if not words:
        return False
    return bool(_RUNNER.match(" ".join(words)))


def _segment_is_reader(segment):
    words = _command_words(segment)
    if not words:
        return False
    cmd = words[0]
    if cmd == "git":
        return len(words) > 1 and words[1] in _GIT_READS
    if cmd == "sed" and any(w in ("-i", "--in-place") or w.startswith("-i") for w in words[1:]):
        return False
    return cmd in _READERS


def classify_shell(command):
    segments = split_segments(command or "")
    if any(_segment_is_runner(s) for s in segments):
        return "other"
    if any(_segment_is_reader(s) for s in segments):
        return "shell"
    return "other"


def classify_call(tool, args=None):
    """Return ``speedread``, ``native``, ``shell``, or ``other``."""
    name, args = _unwrap_use_tool(tool, args or {})
    if is_speedread_name(name):
        return "speedread"
    if name in NATIVE_READ:
        return "native"
    if name in SHELL_TOOLS or name.lower() == "bash":
        return classify_shell(shell_command(args))
    return "other"


def summarize_calls(calls):
    """``calls`` is a list of ``{"tool", "args"}`` (args optional).

    Shares are ``None`` when the trial made no code-reading step, so a trial
    that only ran tests is not a 0% speedread share.
    """
    counts = {"speedread": 0, "native": 0, "shell": 0, "other": 0}
    tools = []
    other_speedread = []
    for c in calls:
        tool = c.get("tool")
        kind = classify_call(tool, c.get("args") or {})
        counts[kind] += 1
        name, _ = _unwrap_use_tool(tool, c.get("args") or {})
        tools.append(name)
        leaf = speedread_leaf(name)
        if leaf and leaf not in _COUNTED_LEAVES:
            other_speedread.append(name)
    reading = counts["speedread"] + counts["native"] + counts["shell"]

    def share(k):
        return (counts[k] / reading) if reading else None

    return {
        "tools": tools,
        "tool_calls": len(calls),
        "used_speedread": counts["speedread"] > 0,
        "code_read_steps": reading,
        "speedread_steps": counts["speedread"],
        "native_steps": counts["native"],
        "shell_steps": counts["shell"],
        "other_steps": counts["other"],
        "speedread_share": share("speedread"),
        "native_share": share("native"),
        "shell_share": share("shell"),
        "other_speedread_calls": len(other_speedread),
        "other_speedread_tools": other_speedread,
    }


_BANNED = (
    (re.compile(r"speedread", re.I), "speedread"),
    (re.compile(r"\btrace\b", re.I), "trace"),
    (re.compile(r"MCP tools", re.I), "MCP tools"),
    (re.compile(r"use the MCP", re.I), "use the MCP"),
)


def prompt_violations(prompt):
    """Phrases that make an unprompted arm prompted. Empty if the prompt is clean."""
    return [label for rx, label in _BANNED if rx.search(prompt or "")]


def check_transcript(path, record):
    """Recompute adoption from a saved transcript and compare to the record.

    Returns a list of human-readable mismatches. Empty means the summary
    agrees with the transcript. Copilot JSONL and Grok streaming-json are
    both accepted; the caller passes events already parsed into calls when
    the file is Grok NDJSON and ``record`` carries ``calls``.
    """
    text = Path(path).read_text()
    calls = calls_from_copilot(text)
    if calls is None:
        calls = calls_from_grok_file(text)
    got = summarize_calls(calls)
    bad = []
    if bool(record.get("used_speedread")) != got["used_speedread"]:
        bad.append(f"used_speedread record={record.get('used_speedread')} transcript={got['used_speedread']}")
    for key in ("speedread_steps", "native_steps", "shell_steps", "code_read_steps"):
        if key in record and record[key] != got[key]:
            bad.append(f"{key} record={record[key]} transcript={got[key]}")
    return bad


def calls_from_copilot(text):
    """Copilot JSONL tool events, or None if this is not that shape."""
    pending, calls, saw = {}, [], False
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t, d = e.get("type"), e.get("data") or {}
        if t == "tool.execution_start":
            saw = True
            pending[d.get("toolCallId")] = {"tool": d.get("toolName"), "args": d.get("arguments") or {}}
        elif t == "tool.execution_complete":
            saw = True
            c = pending.pop(d.get("toolCallId"), {"tool": "?", "args": {}})
            calls.append(c)
    return calls if saw else None


def calls_from_grok_file(text):
    from grok_transcript import parse_streaming_json
    return parse_streaming_json(text)["calls"]


def main():
    """``python3 evals/adoption.py check RUN_DIR`` — recompute every trial from its transcript."""
    if len(sys.argv) != 3 or sys.argv[1] != "check":
        print("usage: python3 evals/adoption.py check RUN_DIR", file=sys.stderr)
        return 2
    run = Path(sys.argv[2])
    recs = [json.loads(l) for l in (run / "trials.jsonl").read_text().splitlines() if l.strip()]
    mismatches, checked = [], 0
    for r in recs:
        if r.get("status") in {"skipped", "error"} and not r.get("tools"):
            continue
        name = f"{r['task']}__{r['condition']}__{r['trial']}"
        if r.get("model"):
            alt = run / "transcripts" / f"{name}__{r['model']}.jsonl"
            path = alt if alt.exists() else run / "transcripts" / f"{name}.jsonl"
        else:
            path = run / "transcripts" / f"{name}.jsonl"
        if not path.exists():
            mismatches.append(f"{name}: transcript missing ({path.name})")
            continue
        checked += 1
        for m in check_transcript(path, r):
            mismatches.append(f"{name}: {m}")
    if mismatches:
        print(f"{len(mismatches)} mismatches in {checked} transcripts:")
        print("\n".join(mismatches))
        return 1
    print(f"adoption check ok: {checked} transcripts agree with trials.jsonl ({len(recs)} records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
