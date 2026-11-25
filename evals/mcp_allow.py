#!/usr/bin/env python3
"""stdio MCP proxy that drops tools outside an allowlist.

speedread's own tool definitions are not edited. Grok is pointed at this
process, which forwards to the real binary and removes other tools from
`tools/list` (and refuses a call to one). `--disallowed-tools` cannot do this:
Grok's flag removes built-in tools only, and MCP tools stay reachable through
`search_tool` / `use_tool`.

  python3 evals/mcp_allow.py --allow map,search,read -- speedread --root DIR mcp
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _end_string(s: str, i: int) -> int:
    i += 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated string")


def _end_container(s: str, i: int) -> int:
    depth = 0
    while i < len(s):
        ch = s[i]
        if ch == '"':
            i = _end_string(s, i)
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unterminated container")


def _value_span(s: str, i: int) -> tuple[int, int]:
    i = _skip_ws(s, i)
    start = i
    ch = s[i]
    if ch == '"':
        return start, _end_string(s, i)
    if ch in "{[":
        return start, _end_container(s, i)
    j = i
    while j < len(s) and s[j] not in ",}] \t\r\n":
        j += 1
    return start, j


def _object_fields(s: str, start: int, end: int) -> list[tuple[str, int, int]]:
    i = start + 1
    out = []
    while True:
        i = _skip_ws(s, i)
        if i >= end or s[i] == "}":
            break
        key_end = _end_string(s, i)
        key = json.loads(s[i:key_end])
        i = _skip_ws(s, key_end)
        if s[i] != ":":
            raise ValueError("expected colon")
        vs, ve = _value_span(s, i + 1)
        out.append((key, vs, ve))
        i = _skip_ws(s, ve)
        if i < end and s[i] == ",":
            i += 1
    return out


def _array_elements(s: str, start: int, end: int) -> list[tuple[int, int]]:
    i = start + 1
    out = []
    while True:
        i = _skip_ws(s, i)
        if i >= end or s[i] == "]":
            break
        a, b = _value_span(s, i)
        out.append((a, b))
        i = _skip_ws(s, b)
        if i < end and s[i] == ",":
            i += 1
    return out


def _find_tools_array(body: str) -> tuple[int, int] | None:
    """Byte-preserving search for the top-level result.tools array, else the first tools array."""
    if not body or body[0] != "{":
        return None
    end = len(body.rstrip("\n"))
    for key, vs, ve in _object_fields(body, 0, end):
        if key == "result" and body[vs] == "{":
            for rkey, rvs, rve in _object_fields(body, vs, ve):
                if rkey == "tools" and body[rvs] == "[":
                    return rvs, rve
        if key == "tools" and body[vs] == "[":
            return vs, ve
    return None


def _tool_name(body: str, start: int, end: int) -> str | None:
    if body[start] != "{":
        return None
    for key, vs, ve in _object_fields(body, start, end):
        if key == "name":
            return json.loads(body[vs:ve])
    return None


def filter_tools_message(line: str, allow: set[str]) -> str:
    """Drop tool objects whose name is not in `allow`. Other bytes stay as the server wrote them."""
    newline = "\n" if line.endswith("\n") else ""
    body = line[:-1] if newline else line
    span = _find_tools_array(body)
    if span is None:
        return line
    start, end = span
    kept = []
    for a, b in _array_elements(body, start, end):
        name = _tool_name(body, a, b)
        if name in allow:
            kept.append(body[a:b])
    array = "[" + ",".join(kept) + "]"
    return body[:start] + array + body[end:] + newline


def blocked_call(line: str, allow: set[str]) -> str | None:
    """A JSON-RPC error for tools/call of a name outside the allowlist, else None."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if obj.get("method") != "tools/call":
        return None
    name = ((obj.get("params") or {}).get("name"))
    if not isinstance(name, str) or name in allow:
        return None
    err = {
        "jsonrpc": "2.0",
        "id": obj.get("id"),
        "error": {"code": -32602, "message": f"tool {name} is outside the measurement allowlist"},
    }
    return json.dumps(err, separators=(",", ":")) + "\n"


def serve(allow: set[str], cmd: list[str]) -> int:
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin and proc.stdout

    def outbound():
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            text = line.decode()
            sys.stdout.write(filter_tools_message(text, allow))
            sys.stdout.flush()

    thread = threading.Thread(target=outbound, daemon=True)
    thread.start()
    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            blocked = blocked_call(line, allow)
            if blocked is not None:
                sys.stdout.write(blocked)
                sys.stdout.flush()
                continue
            proc.stdin.write(line.encode())
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
    thread.join(timeout=2)
    return proc.returncode or 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--allow", required=True, help="comma-separated tool names to keep")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="the real server command, after --")
    args = ap.parse_args(argv)
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("pass the server command after --")
    allow = {name.strip() for name in args.allow.split(",") if name.strip()}
    return serve(allow, cmd)


if __name__ == "__main__":
    sys.exit(main())
