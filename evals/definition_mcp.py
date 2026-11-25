"""Minimal MCP stdio client. Speaks the live server; does not rebuild its schema."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path


class McpError(RuntimeError):
    pass


class Mcp:
    def __init__(self, binary: str, args: list[str], cwd: str = "/"):
        # The child cwd is often `/`. A relative binary would then miss.
        binary = str(Path(binary).resolve())
        self.proc = subprocess.Popen(
            [binary, *args],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.next_id = 0
        self.err = bytearray()
        self._drain = threading.Thread(target=self._read_err, daemon=True)
        self._drain.start()

    def _read_err(self):
        assert self.proc.stderr is not None
        for chunk in iter(lambda: self.proc.stderr.read(4096), b""):
            self.err += chunk

    def close(self):
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)

    def err_tail(self) -> str:
        return self.err.decode(errors="replace")[-800:]

    def _send(self, payload: dict):
        assert self.proc.stdin is not None
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        self.proc.stdin.write(line.encode())
        self.proc.stdin.flush()

    def notify(self, method: str, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def rpc(self, method: str, params) -> tuple[str, dict]:
        """Return the exact response line (including its trailing newline) and the parsed object."""
        self.next_id += 1
        want = self.next_id
        self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
        assert self.proc.stdout is not None
        while True:
            raw = self.proc.stdout.readline()
            if not raw:
                raise McpError(f"{method} : server closed stdout; stderr={self.err_tail()!r}")
            line = raw.decode()
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise McpError(f"non-json from server: {line[:200]!r}") from exc
            if obj.get("method") == "roots/list":
                self._send({"jsonrpc": "2.0", "id": obj.get("id"), "result": {"roots": []}})
                continue
            if obj.get("id") == want and "method" not in obj:
                return line, obj

    def handshake(self, client_name: str = "definition-cost") -> tuple[str, dict]:
        line, obj = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "0"},
            },
        )
        self.notify("notifications/initialized")
        return line, obj


def tool_text(obj: dict) -> tuple[str | None, dict | None]:
    """(text, error). error is set when the server rejected the call."""
    if "error" in obj:
        return None, obj["error"]
    result = obj.get("result") or {}
    if result.get("isError"):
        return None, result
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts), None
