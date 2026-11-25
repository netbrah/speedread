#!/usr/bin/env python3
"""Parse Grok CLI ``--output-format streaming-json`` NDJSON.

The documented shape is one type-tagged object per line (``tool_call``,
``tool_call_update``, ``text``, ``usage``, ``end``, ``error``, …). The same
parser also accepts a raw ACP ``session/update`` notification, so a stream
that is still in the agent's native envelope is not scored as "no tools".

This module does not contact Grok. Live token totals come from ``grok usage``;
the ``end`` event is only a fallback the runner labels as such.
"""

import json


def parse_streaming_json(text):
    calls, order = {}, []
    answer, errors = [], []
    end = None
    usage_events = 0
    bad_lines = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        kind, payload = _normalize(event)
        if kind == "text":
            if payload:
                answer.append(payload)
        elif kind == "tool_call":
            cid = payload.get("toolCallId") or f"anon-{len(order)}"
            if cid not in calls:
                order.append(cid)
                calls[cid] = {"tool": None, "args": {}, "status": None, "result": ""}
            _merge_call(calls[cid], payload, start=True)
        elif kind == "tool_call_update":
            cid = payload.get("toolCallId") or f"anon-{len(order)}"
            if cid not in calls:
                order.append(cid)
                calls[cid] = {"tool": None, "args": {}, "status": None, "result": ""}
            _merge_call(calls[cid], payload, start=False)
        elif kind == "usage":
            usage_events += 1
        elif kind == "end":
            end = payload
        elif kind == "error":
            errors.append(payload.get("message") or payload.get("data") or json.dumps(payload))
    out_calls = []
    for cid in order:
        c = calls[cid]
        if not c.get("tool"):
            c["tool"] = "?"
        out_calls.append(c)
    end = end or {}
    return {
        "answer": "".join(answer),
        "calls": out_calls,
        "model_calls": usage_events or end.get("num_turns") or end.get("numTurns"),
        "end": end,
        "errors": errors,
        "bad_lines": bad_lines,
        "session_id": end.get("sessionId") or end.get("session_id"),
        "stop_reason": end.get("stopReason") or end.get("stop_reason"),
    }


def _normalize(event):
    """Return ``(kind, payload)`` for a flattened streaming-json line or an ACP notification."""
    if not isinstance(event, dict):
        return "ignore", {}
    if event.get("method") == "session/update":
        update = (event.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate") or ""
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            return "text", content.get("text") or ""
        if kind in ("tool_call", "tool_call_update"):
            return kind, update
        return "ignore", {}
    kind = event.get("type")
    if kind == "text":
        data = event.get("data")
        if isinstance(data, dict):
            data = data.get("text") or ""
        return "text", data or ""
    if kind in ("tool_call", "tool_call_update", "usage", "end", "error"):
        return kind, event
    return "ignore", {}


def _merge_call(call, payload, start):
    name = payload.get("toolName") or payload.get("name")
    if name and (start or call.get("tool") in (None, "?")):
        call["tool"] = name
    raw = payload.get("rawInput")
    if isinstance(raw, dict) and raw:
        # Later events fill fields the start event left out; they do not wipe it.
        merged = dict(call.get("args") or {})
        merged.update(raw)
        call["args"] = merged
    if payload.get("status"):
        call["status"] = payload["status"]
    result = _result_text(payload)
    if result:
        call["result"] = result


def _result_text(payload):
    raw = payload.get("rawOutput")
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict) and raw:
        return json.dumps(raw)
    chunks = []
    for block in payload.get("content") or []:
        if isinstance(block, str):
            chunks.append(block)
        elif isinstance(block, dict):
            if isinstance(block.get("text"), str):
                chunks.append(block["text"])
            content = block.get("content")
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
            elif isinstance(content, str):
                chunks.append(content)
    return "".join(chunks)
