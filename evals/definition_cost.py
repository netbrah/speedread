#!/usr/bin/env python3
"""Measure speedread's live tool-definition cost, and the gates around slimming it.

  python3 evals/definition_cost.py power
  python3 evals/definition_cost.py self-test
  python3 evals/definition_cost.py tokens --binary target/fast/speedread
  python3 evals/definition_cost.py arguments --binary target/fast/speedread
  python3 evals/definition_cost.py chromium --binary target/fast/speedread \\
      --repo target/eval-repos/chromium
  python3 evals/definition_cost.py agent --harness grok --binary target/fast/speedread \\
      --model <id from `grok models`> --yes

`tokens`, `arguments`, and `chromium` talk to a built binary. `agent` is the
Grok (or Copilot) run; this measurement VM does not execute it. Token counts
use the JSON the server writes, not a schema rebuilt from source.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from definition_mcp import Mcp, tool_text  # noqa: E402
from definition_plan import (  # noqa: E402
    ALPHA,
    ARGUMENT_P0,
    ARGUMENT_P1,
    FIRST_CALL_DELTA_TOKENS,
    POWER,
    PROBE_CONDITIONS,
    PROBE_SESSIONS_PER_CONDITION,
    SELECTION_P0,
    SELECTION_P1,
    SUCCESS_P0,
    SUCCESS_P1,
    TASKS_PER_TOOL,
    TOOLS,
    TRIALS_PER_TASK,
)

TASKS_PATH = HERE / "definition_tasks.json"
FIXTURE = HERE / "fixtures" / "definition-gate"
DEFAULT_OUT = ROOT / "evals" / "fork" / "definition-tokens"
FLAGS = ["--deadline-ms", "10000", "--cache-mb", "256"]

# Calls the schema marks required, sent without the required field. Counted
# separately from the validity rate so a lenient accept cannot hide.
NEGATIVES = [
    ("search", {}),
    ("read", {}),
    ("trace", {}),
]


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam), enough for the power table."""
    if not 0.0 < p < 1.0:
        raise ValueError(p)
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def n_two_proportions(p0: float, p1: float, alpha: float = ALPHA, power: float = POWER) -> float:
    za, zb = norm_ppf(1 - alpha / 2), norm_ppf(power)
    var = p0 * (1 - p0) + p1 * (1 - p1)
    return (za + zb) ** 2 * var / (p0 - p1) ** 2


def power_two_proportions(p0: float, p1: float, n: int, alpha: float = ALPHA) -> float:
    za = norm_ppf(1 - alpha / 2)
    se = math.sqrt(p0 * (1 - p0) / n + p1 * (1 - p1) / n)
    z = abs(p0 - p1) / se
    return 0.5 * (1 + math.erf((z - za) / math.sqrt(2))) + 0.5 * (1 + math.erf((-z - za) / math.sqrt(2)))


def n_paired_mean(sigma: float, delta: float, alpha: float = ALPHA, power: float = POWER) -> float:
    za, zb = norm_ppf(1 - alpha / 2), norm_ppf(power)
    return (za + zb) ** 2 * sigma ** 2 / delta ** 2


def sigma_at_n(n: int, delta: float, alpha: float = ALPHA, power: float = POWER) -> float:
    za, zb = norm_ppf(1 - alpha / 2), norm_ppf(power)
    return delta * math.sqrt(n) / (za + zb)


def power_markdown() -> str:
    per_tool = TASKS_PER_TOOL * TRIALS_PER_TASK
    pooled = per_tool * len(TOOLS)
    rows = [
        "# Pre-registered sample size",
        "",
        "Two-sided test, α = 0.05, target power 80%. Binary n is trials per arm",
        "(Phase 1 exclusive vs a later exclusive re-measure). First-call n is paired",
        "sessions per condition. Planning rates are assumptions, not measurements;",
        "the Grok baseline recomputes the detectable drop from the rate it actually sees.",
        "",
        f"Per tool: {TASKS_PER_TOOL} tasks × {TRIALS_PER_TASK} trials = {per_tool} trials.",
        f"Pooled exclusive sessions: {pooled}.",
        f"First-call probe: {PROBE_SESSIONS_PER_CONDITION} sessions × {len(PROBE_CONDITIONS)} conditions "
        f"= {PROBE_SESSIONS_PER_CONDITION * len(PROBE_CONDITIONS)} short sessions, interleaved.",
        "",
        "| Gate | Planning rates | n needed for 80% | Power at the planned n |",
        "|---|---|---:|---:|",
    ]
    binary = [
        ("tool selection, per tool", SELECTION_P0, SELECTION_P1, per_tool),
        ("tool selection, pooled", SELECTION_P0, SELECTION_P1, pooled),
        ("task success, per tool", SUCCESS_P0, SUCCESS_P1, per_tool),
        ("argument validity, per tool", ARGUMENT_P0, ARGUMENT_P1, per_tool),
        ("argument validity, pooled", ARGUMENT_P0, ARGUMENT_P1, pooled),
    ]
    for label, p0, p1, n in binary:
        need = n_two_proportions(p0, p1)
        rows.append(f"| {label} | {p0:.2f} → {p1:.2f} | {need:.0f} | {power_two_proportions(p0, p1, n):.3f} |")
    rows += [
        "",
        f"First-call MDE: {FIRST_CALL_DELTA_TOKENS} tokens on the paired mean of full input",
        "(uncached + cache read + cache creation) for available − baseline.",
        f"At n = {PROBE_SESSIONS_PER_CONDITION}, 80% power holds while the SD of that paired",
        f"difference is at most {sigma_at_n(PROBE_SESSIONS_PER_CONDITION, FIRST_CALL_DELTA_TOKENS):.0f} tokens.",
        "",
        "| Assumed SD of (available − baseline) | n for 80% at a 200-token shift |",
        "|---:|---:|",
    ]
    for sigma in (100, 250, 500, 1000, 2000):
        rows.append(f"| {sigma} | {n_paired_mean(sigma, FIRST_CALL_DELTA_TOKENS):.0f} |")
    rows += [
        "",
        "Per-tool tests are each at α = 0.05, not Bonferroni. A gate that blocks when any",
        "one tool regresses would rather false-block than miss a one-tool break. The pooled",
        "rate is reported too, because a 10 point drop on one tool of four is only 2.5 points",
        "pooled and the pool test will not see it.",
        "",
        "Spread rule: a comparison is inconclusive when the interquartile range of the",
        "paired differences (or, for a rate, the bootstrap interval width) is larger than",
        "the absolute effect. The min–max range is reported beside the median; it is not",
        "the stop rule, because the range grows with n.",
        "",
    ]
    return "\n".join(rows)


# --- JSON spans on the exact server bytes ---------------------------------


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _end_string(s: str, i: int) -> int:
    if s[i] != '"':
        raise ValueError(f"expected string at {i}")
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


def value_span(s: str, i: int) -> tuple[int, int]:
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


def object_fields(s: str, start: int, end: int) -> list[tuple[str, int, int]]:
    """(key, value_start, value_end) for one object. Offsets into s."""
    if s[start] != "{":
        raise ValueError("not an object")
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
        vs, ve = value_span(s, i + 1)
        out.append((key, vs, ve))
        i = _skip_ws(s, ve)
        if i < end and s[i] == ",":
            i += 1
    return out


def array_elements(s: str, start: int, end: int) -> list[tuple[int, int]]:
    if s[start] != "[":
        raise ValueError("not an array")
    i = start + 1
    out = []
    while True:
        i = _skip_ws(s, i)
        if i >= end or s[i] == "]":
            break
        a, b = value_span(s, i)
        out.append((a, b))
        i = _skip_ws(s, b)
        if i < end and s[i] == ",":
            i += 1
    return out


def find_field(s: str, key: str) -> tuple[int, int]:
    fields = object_fields(s, 0, len(s)) if s[:1] == "{" else None
    # The response line is one object. Search only its top-level fields.
    if not s or s[0] != "{":
        raise ValueError("response is not a JSON object")
    for name, vs, ve in object_fields(s, 0, len(s.rstrip("\n"))):
        if name == key:
            return vs, ve
    raise KeyError(key)


def encodings():
    import tiktoken
    return {
        "o200k_base": tiktoken.get_encoding("o200k_base"),
        "cl100k_base": tiktoken.get_encoding("cl100k_base"),
    }, tiktoken.__version__


def count_tokens(enc, text: str) -> int:
    return len(enc.encode(text, disallowed_special=()))


def attribute(enc, text: str, labels: list[str]) -> dict:
    """Assign each token of `text` to the label of its bytes. Tokens that cross
    a label boundary go to `boundary`. Counts sum to the token total."""
    raw = text.encode()
    if len(labels) != len(raw):
        raise ValueError(f"label length {len(labels)} != {len(raw)} bytes")
    ids = enc.encode(text, disallowed_special=())
    counts: dict[str, int] = {}
    boundary = 0
    pos = 0
    acc = []
    for tid in ids:
        piece = enc.decode_single_token_bytes(tid)
        acc.append(piece)
        end = pos + len(piece)
        labs = set(labels[pos:end])
        if len(labs) == 1:
            lab = next(iter(labs))
            counts[lab] = counts.get(lab, 0) + 1
        else:
            boundary += 1
        pos = end
    if b"".join(acc) != raw or pos != len(raw):
        raise ValueError("tokenizer bytes did not round-trip; refusing to attribute")
    if sum(counts.values()) + boundary != len(ids):
        raise ValueError("attribution does not sum to the token total")
    return {"tokens": len(ids), "by_label": counts, "boundary": boundary}


def paint(labels: list[str], start: int, end: int, label: str):
    for i in range(start, end):
        labels[i] = label


def tool_rows(result_text: str, enc) -> tuple[dict, list[dict]]:
    """Break a tools/list `result` object into per-tool attributed tokens.

    The tool list is whatever the server returned. A fifth tool, including one
    named `context`, is another row. Nothing here assumes the four names.
    """
    raw = result_text.encode()
    labels = ["result.other"] * len(raw)
    parsed = json.loads(result_text)
    tools_field = None
    for name, vs, ve in object_fields(result_text, 0, len(result_text)):
        if name == "tools":
            tools_field = (vs, ve)
    if tools_field is None:
        raise ValueError("result has no tools array")
    rows = []
    seen = []
    for el_s, el_e in array_elements(result_text, *tools_field):
        el_raw_s, el_raw_e = len(result_text[:el_s].encode()), len(result_text[:el_e].encode())
        paint(labels, el_raw_s, el_raw_e, "pending")
        fields = object_fields(result_text, el_s, el_e)
        name = None
        isolated = {}
        schema_fields = []
        for key, vs, ve in fields:
            raw_s, raw_e = len(result_text[:vs].encode()), len(result_text[:ve].encode())
            if key == "name":
                name = json.loads(result_text[vs:ve])
            bucket = key if key in ("description", "inputSchema", "annotations") else "other"
            # `other` stays the tool default; named buckets overwrite it.
            if bucket != "other":
                paint(labels, raw_s, raw_e, f"{bucket}")
            isolated[bucket] = isolated.get(bucket, 0) + count_tokens(enc, result_text[vs:ve])
            if key == "inputSchema":
                schema = json.loads(result_text[vs:ve])
                props = schema.get("properties") or {}
                for pname, spec in props.items():
                    if isinstance(spec, dict) and isinstance(spec.get("description"), str):
                        schema_fields.append({
                            "property": pname,
                            "description": spec["description"],
                            "isolated_tokens": count_tokens(enc, spec["description"]),
                        })
        if not name:
            raise ValueError("tool object has no name")
        # Rename the pending/other bytes of this element to the tool, and the
        # overlaid buckets likewise, so tools do not share a label.
        for i in range(el_raw_s, el_raw_e):
            lab = labels[i]
            if lab == "pending":
                labels[i] = f"{name}.other"
            elif lab in ("description", "inputSchema", "annotations"):
                labels[i] = f"{name}.{lab}"
        seen.append(name)
        rows.append({
            "name": name,
            "isolated_tokens": isolated,
            "schema_field_descriptions": schema_fields,
        })
    attributed = attribute(enc, result_text, labels)
    by_name = {}
    for name in seen:
        by_name[name] = {
            "description": attributed["by_label"].get(f"{name}.description", 0),
            "inputSchema": attributed["by_label"].get(f"{name}.inputSchema", 0),
            "annotations": attributed["by_label"].get(f"{name}.annotations", 0),
            "other": attributed["by_label"].get(f"{name}.other", 0),
        }
    for row in rows:
        row["attributed_tokens"] = by_name[row["name"]]
        row["attributed_total"] = sum(by_name[row["name"]].values())
    return attributed, rows


def repeated_field_text(rows: list[dict]) -> list[dict]:
    """Identical property-description strings that appear on more than one tool."""
    where: dict[str, list[str]] = {}
    for row in rows:
        for field in row["schema_field_descriptions"]:
            where.setdefault(field["description"], []).append(f"{row['name']}.{field['property']}")
    return [
        {"text": text, "occurrences": places, "isolated_tokens_each": None, "copies": len(places)}
        for text, places in where.items() if len(places) > 1
    ]


def server_args(binary: str, root: str | None, rootless: bool) -> list[str]:
    args = []
    if rootless:
        args += ["--no-cwd-root"]
    elif root:
        args += ["--root", root]
    args += FLAGS + ["mcp"]
    return args


def one_listing(binary: str, root: str) -> dict:
    mcp = Mcp(binary, server_args(binary, root, False), cwd="/")
    try:
        init_line, init_obj = mcp.handshake()
        list_line, list_obj = mcp.rpc("tools/list", {})
    finally:
        mcp.close()
    return {"initialize_line": init_line, "initialize": init_obj, "tools_line": list_line, "tools": list_obj}


def slice_result(line: str) -> str:
    body = line[:-1] if line.endswith("\n") else line
    vs, ve = find_field(body, "result")
    return body[vs:ve]


def instruction_text(init_obj: dict) -> str | None:
    result = init_obj.get("result") or {}
    text = result.get("instructions")
    return text if isinstance(text, str) else None


def cmd_tokens(args) -> int:
    encs, tik_version = encodings()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    root = str(FIXTURE.resolve())
    runs = []
    for i in range(args.runs):
        got = one_listing(args.binary, root)
        (out / f"tools-list-run-{i}.jsonl").write_text(got["tools_line"])
        (out / f"initialize-run-{i}.jsonl").write_text(got["initialize_line"])
        result = slice_result(got["tools_line"])
        runs.append({
            "result": result,
            "result_sha256": hashlib.sha256(result.encode()).hexdigest(),
            "wire_line": got["tools_line"],
            "initialize_line": got["initialize_line"],
            "initialize": got["initialize"],
            "tools": got["tools"],
        })
    hashes = {r["result_sha256"] for r in runs}
    stable = len(hashes) == 1
    primary = runs[0]
    result = primary["result"]
    parsed_tools = (primary["tools"].get("result") or {}).get("tools") or []
    names = [t.get("name") for t in parsed_tools]
    per_encoding = {}
    field_rows_for_repeat = None
    for enc_name, enc in encs.items():
        attributed, rows = tool_rows(result, enc)
        if field_rows_for_repeat is None:
            field_rows_for_repeat = rows
        instr = instruction_text(primary["initialize"])
        instr_tokens = count_tokens(enc, instr) if instr is not None else None
        # Exact JSON string of the instructions field, if present, tokenized alone.
        init_body = primary["initialize_line"][:-1] if primary["initialize_line"].endswith("\n") else primary["initialize_line"]
        try:
            ivs, ive = find_field(slice_result(primary["initialize_line"]), "instructions")
            instr_json = slice_result(primary["initialize_line"])[ivs:ive]
            instr_json_tokens = count_tokens(enc, instr_json)
        except KeyError:
            instr_json_tokens = None
        per_encoding[enc_name] = {
            "result_tokens": attributed["tokens"],
            "result_boundary_tokens": attributed["boundary"],
            "result_other_tokens": attributed["by_label"].get("result.other", 0),
            "wire_line_tokens": count_tokens(enc, primary["wire_line"]),
            "tools": rows,
            "attribution_sums_to_result": (
                sum(sum(r["attributed_tokens"].values()) for r in rows)
                + attributed["by_label"].get("result.other", 0)
                + attributed["boundary"]
                == attributed["tokens"]
            ),
            "instructions_text_tokens": instr_tokens,
            "instructions_json_string_tokens": instr_json_tokens,
        }
        del init_body
    repeats = repeated_field_text(field_rows_for_repeat or [])
    for item in repeats:
        item["isolated_o200k_tokens_each"] = count_tokens(encs["o200k_base"], item["text"])
        item.pop("isolated_tokens_each", None)
    # Rootless instructions, one launch, not part of the tools/list headline.
    rootless = Mcp(args.binary, server_args(args.binary, None, True), cwd="/")
    try:
        _, rootless_init = rootless.handshake()
    finally:
        rootless.close()
    rooted_instr = instruction_text(primary["initialize"]) or ""
    rootless_instr = instruction_text(rootless_init) or ""
    summary = {
        "verdict": "stable" if stable else "unstable — do not treat the count as a baseline",
        "unit": "tokens",
        "tokenizer_package": f"tiktoken {tik_version}",
        "encode": "Encoding.encode(text, disallowed_special=())",
        "runs": args.runs,
        "result_sha256": sorted(hashes),
        "result_bytes": len(result.encode()),
        "headline": {
            "what": "exact JSON of the tools/list result object, sliced from the server's response line",
            "tokenizer": "o200k_base",
            "tokens": per_encoding["o200k_base"]["result_tokens"],
            "min": per_encoding["o200k_base"]["result_tokens"] if stable else None,
            "max": per_encoding["o200k_base"]["result_tokens"] if stable else None,
            "median": per_encoding["o200k_base"]["result_tokens"] if stable else None,
            "range": 0 if stable else "payload bytes differed across launches",
        },
        "cl100k_base_result_tokens": per_encoding["cl100k_base"]["result_tokens"],
        "in_the_tools_list_result": [
            "tool name, description, inputSchema, annotations, and any other field the server put on each tool",
            "the tools array and the result object's own punctuation",
        ],
        "not_in_the_tools_list_result": [
            "the jsonrpc envelope and id (reported as wire_line_tokens, which does include them)",
            "initialize instructions (reported separately; they are on initialize, not tools/list)",
            "client wrapping that turns definitions into a model prompt",
            "skills/speedread/SKILL.md",
        ],
        "tool_names_observed": names,
        "fifth_tool": "counted if present; this script does not special-case map/read/search/trace",
        "encodings": {
            name: {
                "result_tokens": block["result_tokens"],
                "wire_line_tokens": block["wire_line_tokens"],
                "result_other_tokens": block["result_other_tokens"],
                "boundary_tokens": block["result_boundary_tokens"],
                "attribution_sums_to_result": block["attribution_sums_to_result"],
                "instructions_text_tokens": block["instructions_text_tokens"],
                "instructions_json_string_tokens": block["instructions_json_string_tokens"],
                "tools": [
                    {
                        "name": row["name"],
                        "attributed_tokens": row["attributed_tokens"],
                        "attributed_total": row["attributed_total"],
                        "isolated_field_tokens": row["isolated_tokens"],
                        "schema_field_descriptions": [
                            {"property": f["property"], "isolated_tokens": f["isolated_tokens"], "chars": len(f["description"])}
                            for f in row["schema_field_descriptions"]
                        ],
                    }
                    for row in block["tools"]
                ],
            }
            for name, block in per_encoding.items()
        },
        "repeated_property_descriptions": repeats,
        "schema_dialect_url": {
            "url": "https://json-schema.org/draft/2020-12/schema",
            "occurrences_in_result": result.count("https://json-schema.org/draft/2020-12/schema"),
            "isolated_o200k_tokens": count_tokens(encs["o200k_base"], "https://json-schema.org/draft/2020-12/schema"),
            "note": "Inside each inputSchema. Isolated count is not additive with the inputSchema column.",
        },
        "instructions": {
            "where": "initialize result.instructions, not inside tools/list",
            "rooted_text": rooted_instr,
            "rootless_extra": rootless_instr[len(rooted_instr):] if rootless_instr.startswith(rooted_instr) else None,
            "rootless_o200k_tokens": count_tokens(encs["o200k_base"], rootless_instr),
            "rooted_o200k_tokens": count_tokens(encs["o200k_base"], rooted_instr),
        },
        "server_version": ((primary["initialize"].get("result") or {}).get("serverInfo") or {}).get("version"),
        "flags": FLAGS,
        "binary": str(Path(args.binary).resolve()),
        "host": host_info(args.binary),
        "limiter": "tokenizer is deterministic; the only noise is a payload that changes between launches",
    }
    if not stable:
        summary["headline"]["tokens_per_run"] = [
            count_tokens(encs["o200k_base"], r["result"]) for r in runs
        ]
    (out / "tools-list-tokens.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "tools-list-tokens.md").write_text(tokens_markdown(summary))
    print(tokens_markdown(summary))
    return 0 if stable else 1


def tokens_markdown(summary: dict) -> str:
    h = summary["headline"]
    lines = [
        f"# tools/list tokens — {summary['verdict']}",
        "",
        f"Headline: **{h['tokens']} {h['tokenizer']} tokens**, median of {summary['runs']} launches, "
        f"range {h['range']}.",
        "",
        f"What that is: {h['what']}. tiktoken {summary['tokenizer_package'].split()[-1]}, "
        f"`{summary['encode']}`. cl100k_base on the same bytes: "
        f"**{summary['cl100k_base_result_tokens']}**.",
        "",
        "In the result: " + "; ".join(summary["in_the_tools_list_result"]) + ".",
        "",
        "Not in the result: " + "; ".join(summary["not_in_the_tools_list_result"]) + ".",
        "",
        f"Tools observed ({len(summary['tool_names_observed'])}): "
        + ", ".join(summary["tool_names_observed"]) + ".",
        "",
        "Attributed tokens sit on the bytes of the result object, so a token that crosses a field boundary "
        "is `boundary` and the columns plus `result.other` plus `boundary` equal the headline. "
        "`isolated` is the field encoded alone and does not add up to the headline.",
        "",
        "| Tool | description | inputSchema | annotations | other | attributed sum | isolated description | isolated inputSchema |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    block = summary["encodings"]["o200k_base"]
    for row in block["tools"]:
        a = row["attributed_tokens"]
        iso = row["isolated_field_tokens"]
        lines.append(
            f"| {row['name']} | {a.get('description', 0)} | {a.get('inputSchema', 0)} | "
            f"{a.get('annotations', 0)} | {a.get('other', 0)} | {row['attributed_total']} | "
            f"{iso.get('description', 0)} | {iso.get('inputSchema', 0)} |"
        )
    lines += [
        "",
        f"result.other {block['result_other_tokens']}, boundary {block['boundary_tokens']}, "
        f"wire line (jsonrpc envelope included) {block['wire_line_tokens']}.",
        f"Attribution sums to the result: {block['attribution_sums_to_result']}.",
        "",
        f"Instructions, rooted server, o200k text: {summary['instructions']['rooted_o200k_tokens']}. "
        f"Rootless server (one launch): {summary['instructions']['rootless_o200k_tokens']}. "
        "Neither number is inside tools/list.",
        "",
        f"JSON Schema dialect URL occurs {summary['schema_dialect_url']['occurrences_in_result']} times "
        f"inside inputSchema; the URL alone is {summary['schema_dialect_url']['isolated_o200k_tokens']} o200k tokens. "
        "That count is inside the inputSchema column, not on top of it.",
        "",
        "Property descriptions that are byte-identical on more than one tool:",
        "",
    ]
    for item in summary["repeated_property_descriptions"]:
        lines.append(
            f"- {item['copies']} copies, {item['isolated_o200k_tokens_each']} o200k tokens each, "
            f"at {', '.join(item['occurrences'])}"
        )
    if not summary["repeated_property_descriptions"]:
        lines.append("- none")
    lines += [
        "",
        f"Server version {summary['server_version']}. Flags `{' '.join(summary['flags'])}`. "
        f"Binary `{summary['binary']}`.",
        "",
        "Limiter: " + summary["limiter"] + ".",
        "",
    ]
    return "\n".join(lines)


def load_tasks() -> list[dict]:
    return json.loads(TASKS_PATH.read_text())["tasks"]


def cmd_arguments(args) -> int:
    tasks = load_tasks()
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["tool"]] = counts.get(task["tool"], 0) + 1
    if counts != {name: TASKS_PER_TOOL for name in TOOLS}:
        sys.exit(f"task set is not {TASKS_PER_TOOL} per tool: {counts}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    mcp = Mcp(args.binary, server_args(args.binary, str(FIXTURE.resolve()), False), cwd="/")
    rows = []
    try:
        mcp.handshake()
        for task in tasks:
            line, obj = mcp.rpc("tools/call", {"name": task["tool"], "arguments": task["canonical_arguments"]})
            text, err = tool_text(obj)
            hits = [bool(re.search(pat, text or "")) for pat in task["response_graders"]]
            ok = err is None and all(hits)
            rows.append({
                "id": task["id"],
                "tool": task["tool"],
                "accepted": err is None,
                "grader_hits": hits,
                "pass": ok,
                "error": err,
                "text_sha256": hashlib.sha256((text or "").encode()).hexdigest(),
                "text_bytes": len((text or "").encode()),
            })
            (out / "argument-responses" / f"{task['id']}.txt").parent.mkdir(parents=True, exist_ok=True)
            (out / "argument-responses" / f"{task['id']}.txt").write_text(text if text is not None else json.dumps(err))
            (out / "argument-responses" / f"{task['id']}.jsonl").write_text(line)
        negatives = []
        for name, arguments in NEGATIVES:
            line, obj = mcp.rpc("tools/call", {"name": name, "arguments": arguments})
            text, err = tool_text(obj)
            negatives.append({
                "tool": name,
                "arguments": arguments,
                "rejected": err is not None,
                "error": err,
                "text_head": (text or "")[:200],
            })
            (out / "argument-responses" / f"negative-{name}.jsonl").write_text(line)
    finally:
        mcp.close()
    passed = sum(1 for r in rows if r["pass"])
    rejected = sum(1 for n in negatives if n["rejected"])
    summary = {
        "verdict": "pass" if passed == len(rows) else "fail",
        "argument_validity_rate": passed / len(rows),
        "passed": passed,
        "tasks": len(rows),
        "unit": "trials",
        "runs": 1,
        "what": "canonical arguments for each fixed task, sent to the live server; pass means the server accepted the call and every response grader matched",
        "tool_selection_rate": None,
        "tool_selection_note": "not measured here; the agent subcommand scores it. This command does not invent a selection rate.",
        "task_success_rate": None,
        "task_success_note": "agent answer graders run on the Grok machine. Canonical response graders are the argument-validity check above.",
        "negatives_rejected": f"{rejected}/{len(negatives)}",
        "negatives": negatives,
        "rows": rows,
        "by_tool": {
            name: {
                "passed": sum(1 for r in rows if r["tool"] == name and r["pass"]),
                "tasks": sum(1 for r in rows if r["tool"] == name),
            }
            for name in TOOLS
        },
        "binary": str(Path(args.binary).resolve()),
        "flags": FLAGS,
        "host": host_info(args.binary),
    }
    (out / "argument-validity.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"argument validity {passed}/{len(rows)} ({summary['verdict']}); "
        f"negatives rejected {rejected}/{len(negatives)}"
    )
    for row in rows:
        if not row["pass"]:
            print(f"  FAIL {row['id']} accepted={row['accepted']} hits={row['grader_hits']}")
    return 0 if passed == len(rows) else 1


def chromium_queries() -> list[list[str]]:
    path = ROOT / "scripts" / "fork-big-eval.py"
    spec = importlib.util.spec_from_file_location("fork_big_eval", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.REPOS["chromium"]["queries"])


def run_query(binary: str, repo: str, query: list[str]) -> dict:
    cmd = [str(Path(binary).resolve()), "--root", repo, *FLAGS, *query]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, cwd="/", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    out = proc.stdout.read()
    _, status, ru = os.wait4(proc.pid, 0)
    wall_ms = (time.perf_counter() - t0) * 1000
    exit_code = os.waitstatus_to_exitcode(status)
    note = re.search(rb"^(incomplete|not fully searched): [^\n]{0,80}", out, re.M)
    return {
        "wall_ms": wall_ms,
        "cpu_s": ru.ru_utime + ru.ru_stime,
        "peak_rss_mib": ru.ru_maxrss / 1024,  # Linux ru_maxrss is KiB
        "exit_code": exit_code,
        "stdout": out,
        "note": note.group(0).decode(errors="replace") if note else None,
    }


def cmd_chromium(args) -> int:
    encs, tik_version = encodings()
    repo = str(Path(args.repo).resolve())
    if not Path(repo).is_dir():
        sys.exit(f"missing corpus {repo}; run scripts/fetch-chromium-corpus.py")
    queries = chromium_queries()
    out = Path(args.out) / "chromium"
    answers = out / "answers"
    answers.mkdir(parents=True, exist_ok=True)
    load_before = Path("/proc/loadavg").read_text().strip()
    host_before = host_info(args.binary)
    print(f"load before timed runs: {load_before}", flush=True)
    print(f"mem before: {host_before['meminfo']} balloon_mib={host_before['balloon_mib']}", flush=True)
    rows = []
    for qi, query in enumerate(queries):
        for run in range(args.runs):
            got = run_query(args.binary, repo, query)
            text = got["stdout"].decode(errors="replace")
            tokens = {name: count_tokens(enc, text) for name, enc in encs.items()}
            name = f"{qi:02d}-run{run}.txt"
            (answers / name).write_bytes(got["stdout"])
            row = {
                "query_index": qi,
                "query": query,
                "run": run,
                "wall_ms": round(got["wall_ms"], 1),
                "cpu_s": round(got["cpu_s"], 3),
                "peak_rss_mib": round(got["peak_rss_mib"], 1),
                "exit_code": got["exit_code"],
                "stdout_bytes": len(got["stdout"]),
                "stdout_sha256": hashlib.sha256(got["stdout"]).hexdigest(),
                "tokens": tokens,
                "note": got["note"],
                "loadavg": Path("/proc/loadavg").read_text().strip(),
            }
            rows.append(row)
            print(
                f"{' '.join(query)[:56]:56} run {run} {row['wall_ms']:8.0f} ms "
                f"rss {row['peak_rss_mib']:6.0f} MiB o200k {tokens['o200k_base']:7d}"
                + (f" | {got['note']}" if got["note"] else ""),
                flush=True,
            )
    # Profile run. Not one of the timed rows. strace slows the process.
    profile = profile_query(args.binary, repo, out)
    summary = chromium_summary(rows, queries, args, tik_version, load_before, profile)
    summary["host_before"] = host_before
    (out / "runs.jsonl").write_text("".join(json.dumps({k: v for k, v in r.items()}) + "\n" for r in rows))
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "summary.md").write_text(chromium_markdown(summary))
    print(chromium_markdown(summary))
    bad = [r for r in rows if r["exit_code"] != 0]
    return 1 if bad else 0


def profile_query(binary: str, repo: str, out: Path) -> dict:
    """One whole-tree search under strace. Excluded from the timed table."""
    dest = out / "profile"
    dest.mkdir(parents=True, exist_ok=True)
    query = ["search", "zzz_profile_not_in_the_timed_set_zzz"]
    cmd = [str(Path(binary).resolve()), "--root", repo, *FLAGS, *query]
    env = os.environ.copy()
    env["SPEEDREAD_STATS"] = "1"
    strace = ["strace", "-c", "-o", str(dest / "strace.txt")]
    if shutil_which("strace") is None:
        return {"ran": False, "reason": "strace not installed"}
    t0 = time.perf_counter()
    proc = subprocess.run(
        strace + cmd, cwd="/", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
    )
    (dest / "stderr.txt").write_bytes(proc.stderr)
    return {
        "ran": True,
        "in_timed_table": False,
        "why": "strace slows the work; this run is the limiter check, not a result",
        "query": query,
        "wall_ms_under_strace": round((time.perf_counter() - t0) * 1000, 1),
        "exit_code": proc.returncode,
        "strace": str(dest / "strace.txt"),
        "stderr_tail": proc.stderr.decode(errors="replace")[-500:],
    }


def shutil_which(name: str) -> str | None:
    from shutil import which
    return which(name)


def _median(xs: list[float]):
    return statistics.median(xs) if xs else None


def chromium_summary(rows, queries, args, tik_version, load_before, profile) -> dict:
    per_query = []
    for qi, query in enumerate(queries):
        mine = [r for r in rows if r["query_index"] == qi]
        ok = [r for r in mine if r["exit_code"] == 0]
        use = ok or mine
        walls = [r["wall_ms"] for r in use]
        rss = [r["peak_rss_mib"] for r in use]
        o200k = [r["tokens"]["o200k_base"] for r in use]
        cl100k = [r["tokens"]["cl100k_base"] for r in use]
        per_query.append({
            "query": query,
            "runs": len(mine),
            "errors": sum(1 for r in mine if r["exit_code"] != 0),
            "notes": sorted({r["note"] for r in mine if r["note"]}),
            "wall_ms": {"first": mine[0]["wall_ms"], "median": _median(walls), "min": min(walls), "max": max(walls)},
            "cpu_s": {"median": _median([r["cpu_s"] for r in use]), "min": min(r["cpu_s"] for r in use), "max": max(r["cpu_s"] for r in use)},
            "peak_rss_mib": {"median": _median(rss), "min": min(rss), "max": max(rss)},
            "answer_tokens_o200k": {"median": _median(o200k), "min": min(o200k), "max": max(o200k)},
            "answer_tokens_cl100k": {"median": _median(cl100k), "min": min(cl100k), "max": max(cl100k)},
            "answer_identical_across_runs": len({r["stdout_sha256"] for r in ok}) <= 1,
        })
    return {
        "verdict": "baseline",
        "corpus": str(Path(args.repo).resolve()),
        "queries_from": "scripts/fork-big-eval.py REPOS['chromium']['queries']",
        "runs_per_query": args.runs,
        "order": "all runs of a query before the next query, same order as fork-big-eval.py; no 6s early stop",
        "flags": FLAGS,
        "cwd": "/",
        "build_note": "pass the binary you built; this script does not rebuild",
        "clock": "time.perf_counter wall; ru_utime+ru_stime CPU; Linux ru_maxrss/1024 = MiB",
        "tokenizer": f"tiktoken {tik_version}",
        "encode": "Encoding.encode(stdout decoded as utf-8 with replacement, disallowed_special=())",
        "loadavg_before": load_before,
        "loadavg_after": Path("/proc/loadavg").read_text().strip(),
        "host": host_info(args.binary),
        "profile_not_in_table": profile,
        "queries": per_query,
        "errors": sum(q["errors"] for q in per_query),
        "limiter": "page cache, deadline, and load. A definition-token edit cannot move these times; a later latency gap larger than the min–max here is an observation error.",
    }


def chromium_markdown(summary: dict) -> str:
    lines = [
        "# Chromium query baseline",
        "",
        f"Runs per query: {summary['runs_per_query']}. Flags: `{' '.join(summary['flags'])}`. "
        f"Tokenizer: {summary['tokenizer']}.",
        "",
        f"Load before `{summary['loadavg_before']}`, after `{summary['loadavg_after']}`.",
        "",
        "| Query | first ms | median ms | min–max ms | median RSS MiB | RSS min–max | median o200k | o200k min–max | errors |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for q in summary["queries"]:
        label = " ".join(q["query"])
        if len(label) > 64:
            label = label[:61] + "..."
        w, r, t = q["wall_ms"], q["peak_rss_mib"], q["answer_tokens_o200k"]
        lines.append(
            f"| `{label}` | {w['first']:.0f} | {w['median']:.0f} | {w['min']:.0f}–{w['max']:.0f} | "
            f"{r['median']:.0f} | {r['min']:.0f}–{r['max']:.0f} | {t['median']:.0f} | {t['min']:.0f}–{t['max']:.0f} | {q['errors']} |"
        )
    lines += [
        "",
        f"Errors: {summary['errors']}. Limiter: {summary['limiter']}",
        "",
        "Profile run (not in the table):",
        "",
        "```",
        json.dumps(summary["profile_not_in_table"], indent=2)[:2000],
        "```",
        "",
    ]
    return "\n".join(lines)


def host_info(binary: str | None = None) -> dict:
    def read(path: str) -> str:
        try:
            return Path(path).read_text()
        except OSError:
            return ""

    balloon = {}
    for line in read("/proc/vmstat").splitlines():
        if line.startswith("balloon_"):
            k, v = line.split()
            balloon[k] = int(v)
    pages = balloon.get("balloon_inflate", 0) - balloon.get("balloon_deflate", 0)
    mem = {}
    for line in read("/proc/meminfo").splitlines():
        if line.startswith(("MemTotal", "MemAvailable", "Cached", "Buffers", "MemFree")):
            mem[line.split(":")[0]] = line.split(":", 1)[1].strip()
    model = ""
    for line in read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    sha = None
    if binary and Path(binary).exists():
        sha = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
    rustc = subprocess.run(["rustc", "+stable", "--version"], capture_output=True, text=True)
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return {
        "git_sha": git.stdout.strip(),
        "nproc": os.cpu_count(),
        "uptime_s": read("/proc/uptime").split()[0] if read("/proc/uptime") else None,
        "loadavg": read("/proc/loadavg").strip(),
        "uname": " ".join(os.uname()),
        "cpu_model": model,
        "meminfo": mem,
        "balloon_mib": round(pages * 4 / 1024, 1),
        "rustc_stable": rustc.stdout.strip(),
        "binary_sha256": sha,
    }


def synthetic_five_tool_result() -> str:
    tools = []
    for name in ("map", "read", "search", "trace", "context"):
        tools.append({
            "name": name,
            "description": f"{name} does a thing. Quote: \"x\".",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": f"the {name} query"},
                    "base_dir": {"type": "string", "description": "Absolute directory, same sentence."},
                },
                "required": ["q"],
            },
            "annotations": {"readOnlyHint": True, "title": name},
        })
    return json.dumps({"tools": tools}, separators=(",", ":"))


def cmd_self_test(_args) -> int:
    encs, _ = encodings()
    text = synthetic_five_tool_result()
    for enc_name, enc in encs.items():
        attributed, rows = tool_rows(text, enc)
        names = [r["name"] for r in rows]
        if names != ["map", "read", "search", "trace", "context"]:
            sys.exit(f"{enc_name}: names {names}")
        if not attributed or rows[-1]["attributed_total"] <= 0:
            sys.exit(f"{enc_name}: context tool got no tokens")
        block_sum = sum(r["attributed_total"] for r in rows) + attributed["by_label"].get("result.other", 0) + attributed["boundary"]
        if block_sum != attributed["tokens"]:
            sys.exit(f"{enc_name}: sum {block_sum} != {attributed['tokens']}")
        # A tool with no annotations still attributes.
    bare = json.dumps({"tools": [{"name": "only", "description": "d", "inputSchema": {"type": "object"}}]}, separators=(",", ":"))
    _, rows = tool_rows(bare, encs["o200k_base"])
    if rows[0]["attributed_tokens"]["annotations"] != 0:
        sys.exit("missing annotations should attribute 0")
    tasks = load_tasks()
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["tool"]] = counts.get(task["tool"], 0) + 1
    if counts != {name: TASKS_PER_TOOL for name in TOOLS}:
        sys.exit(f"tasks {counts}")
    # Power table is finite and the planned n meets the stated binary targets.
    per_tool = TASKS_PER_TOOL * TRIALS_PER_TASK
    if power_two_proportions(ARGUMENT_P0, ARGUMENT_P1, per_tool) < 0.80:
        sys.exit("planned per-tool n is under 80% for the argument-validity MDE")
    if power_two_proportions(SELECTION_P0, SELECTION_P1, per_tool) < 0.80:
        sys.exit("planned per-tool n is under 80% for the selection MDE")
    print(f"self-test ok: 5-tool attribution sums on o200k and cl100k; {len(tasks)} tasks")
    return 0


def cmd_power(_args) -> int:
    print(power_markdown())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("power", help="print the pre-registered sample-size table").set_defaults(fn=cmd_power)
    sub.add_parser("self-test", help="fifth-tool attribution and the task-set shape").set_defaults(fn=cmd_self_test)

    p = sub.add_parser("tokens", help="tokenize the live tools/list and initialize payloads")
    p.add_argument("--binary", required=True)
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--runs", type=int, default=5)
    p.set_defaults(fn=cmd_tokens)

    p = sub.add_parser("arguments", help="canonical argument-validity on the fixture")
    p.add_argument("--binary", required=True)
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.set_defaults(fn=cmd_arguments)

    p = sub.add_parser("chromium", help="fork-big-eval Chromium queries plus answer tokens")
    p.add_argument("--binary", required=True)
    p.add_argument("--repo", default=str(ROOT / "target" / "eval-repos" / "chromium"))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--runs", type=int, default=5)
    p.set_defaults(fn=cmd_chromium)

    p = sub.add_parser("agent", help="Grok or Copilot selection, validity, success, first-call delta")
    p.add_argument("--harness", choices=("grok", "copilot"), required=True)
    p.add_argument("--binary", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", default=str(DEFAULT_OUT / "agent"))
    p.add_argument("--yes", action="store_true", help="actually run the pre-registered session count")
    p.add_argument("--print-commands", action="store_true")
    p.add_argument("--trials", type=int, default=TRIALS_PER_TASK)
    p.add_argument("--probe-sessions", type=int, default=PROBE_SESSIONS_PER_CONDITION)
    p.add_argument("--sandbox", default="read-only")
    p.add_argument("--max-credits", type=int, default=60)
    p.add_argument("--timeout", type=int, default=180)
    p.set_defaults(fn=None)

    args = ap.parse_args(argv)
    if args.cmd == "agent":
        from definition_agent import cmd_agent
        return cmd_agent(args)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
