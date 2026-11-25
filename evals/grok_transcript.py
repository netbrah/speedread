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


def normalize_usage(session=None, end=None):
    """One token record per answer. ``input_tokens`` includes cache.

    ``grok usage`` is the source when its session object has token fields.
    There ``inputTokens`` already includes cached reads (a 1.0.92 smoke:
    15004 = 6172 uncached + 8832 ``cachedReadTokens``). Adding the cache
    field again double-counts.

    The streaming ``end`` event is the fallback, and its ``input_tokens``
    excludes cache (same smoke: 11617 + 35456 cache_read + 255 output =
    47328 ``total_tokens``). Those buckets are folded into the same
    inclusive ``input_tokens`` so the two sources are not mixed.
    ``cache_read_tokens`` is always a subset, never an addend.
    """
    session = session or {}
    end = end or {}
    has_ledger = any(session.get(k) is not None for k in ("inputTokens", "outputTokens", "totalTokens"))
    if has_ledger:
        inclusive = session.get("inputTokens")
        output = session.get("outputTokens")
        total = session.get("totalTokens")
        if total is None and inclusive is not None and output is not None:
            total = inclusive + output
        return {
            "input_tokens": inclusive,
            "input_includes_cache": True,
            "output_tokens": output,
            "cache_read_tokens": session.get("cachedReadTokens"),
            "cache_creation_tokens": session.get("cacheCreationTokens"),
            "reasoning_tokens": session.get("reasoningTokens"),
            "total_tokens": total,
            "model_calls": session.get("modelCalls"),
            "cost_usd_ticks": session.get("costUsdTicks"),
            "cost_usd": (session["costUsdTicks"] / 1e10) if isinstance(session.get("costUsdTicks"), int) else None,
            "cost_is_partial": bool(session.get("costIsPartial")),
            "usage_is_incomplete": bool(session.get("usageIsIncomplete")),
            "usage_model": session.get("primaryModelId"),
            "usage_source": "grok usage",
        }
    usage = end.get("usage") or {}
    if not usage:
        return {"input_includes_cache": True, "usage_source": "missing"}
    uncached = usage.get("input_tokens") or 0
    cache = usage.get("cache_read_input_tokens") or 0
    created = usage.get("cache_creation_input_tokens") or 0
    output = usage.get("output_tokens")
    inclusive = uncached + cache + created
    total = usage.get("total_tokens")
    if total is None and output is not None:
        total = inclusive + (output or 0)
    ticks = end.get("total_cost_usd_ticks")
    cost = end.get("total_cost_usd")
    return {
        "input_tokens": inclusive,
        "input_includes_cache": True,
        "output_tokens": output,
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "total_tokens": total,
        "model_calls": end.get("num_turns"),
        "cost_usd_ticks": ticks,
        "cost_usd": cost if isinstance(cost, (int, float)) else (ticks / 1e10 if isinstance(ticks, int) else None),
        "usage_model": None,
        "usage_source": "streaming-json end event",
    }


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
