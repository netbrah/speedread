#!/usr/bin/env python3
"""Fixture tests for the Grok half of the definition-token gate.

The JSON files under fixtures/grok-gate/ are sanitized shapes from a grok 1.0.92
smoke (model grok-4.7, usage name grok-4.7-build). They are not a powered run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from definition_agent import (  # noqa: E402
    GROK_EXCLUSIVE_DENY,
    extract_full_input,
    grok_argv,
    preflight_problem,
    resolve_call,
    speedread_calls,
    surface_ok,
)

FIXTURES = HERE / "fixtures" / "grok-gate"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def main() -> int:
    call = load("use_tool_call.json")
    resolved = resolve_call(call["toolName"], call["rawInput"])
    if not resolved or resolved.get("arguments_lost"):
        sys.exit(f"use_tool arguments lost: {resolved}")
    if resolved["tool"] != "read" or resolved["arguments"] != {"targets": ["src/lib.rs#add"]}:
        sys.exit(f"use_tool parsed wrong: {resolved}")
    calls = speedread_calls([call])
    if len(calls) != 1 or calls[0]["arguments"].get("targets") != ["src/lib.rs#add"]:
        sys.exit(f"speedread_calls dropped tool_input: {calls}")

    usage = load("usage_turns.json")
    base, base_buckets = extract_full_input(usage["baseline"])
    avail, avail_buckets = extract_full_input(usage["available_with_trust"])
    if base != 15004 or base_buckets.get("cached_read_tokens") != 8832:
        sys.exit(f"baseline tokens {base} {base_buckets}")
    if base == 15004 + 8832:
        sys.exit("cache was added on top of inputTokens")
    if avail != 15279 or avail - base != 275:
        sys.exit(f"available delta {avail} - {base}")
    if avail_buckets.get("full_source") != "inputTokens":
        sys.exit(f"full_source {avail_buckets}")

    untrusted = [load("tools_untrusted.json")]
    trusted = [load("tools_trusted.json")]
    exclusive = [load("tools_exclusive.json")]
    if surface_ok(untrusted, "baseline") is not True:
        sys.exit("untrusted list should be a valid baseline surface")
    if surface_ok(untrusted, "available") is not False:
        sys.exit("search_tool/use_tool must not count as speedread on available")
    if surface_ok(untrusted, "exclusive") is not False:
        sys.exit("untrusted list must not pass exclusive")
    if surface_ok(trusted, "available") is not True:
        sys.exit("speedread__ names should satisfy available")
    if surface_ok(trusted, "baseline") is not False:
        sys.exit("baseline must reject a list that contains speedread__")
    if surface_ok(trusted, "exclusive") is not False:
        sys.exit("exclusive must reject a list that still has read_file")
    if surface_ok(exclusive, "exclusive") is not True:
        sys.exit("exclusive list with speedread__ and no file tools should pass")
    if surface_ok([], "available") is not None:
        sys.exit("missing tools list should be unverified, not a pass")

    argv = grok_argv("pong", "grok-4.7", "sid", "/tmp/gate", "read-only", True, 1)
    if "--trust" not in argv:
        sys.exit(f"missing --trust: {argv}")
    deny = argv[argv.index("--disallowed-tools") + 1].split(",")
    if list(GROK_EXCLUSIVE_DENY) != deny:
        sys.exit(f"deny list {deny}")
    if "bash" in deny:
        sys.exit("bash is not Grok's shell tool")
    plain = grok_argv("pong", "grok-4.7", "sid", "/tmp/gate", "read-only", False, 1)
    if "--trust" not in plain or "--disallowed-tools" in plain:
        sys.exit(f"baseline argv {plain}")

    failed = load("init_failure.json")
    problem = preflight_problem("exclusive", failed, "grok")
    if not problem:
        sys.exit("init failure should abort")
    started = {
        "exit_code": 0,
        "stderr_tail": "",
        "stdout": "",
        "events": exclusive,
        "session_id": "00000000-0000-0000-0000-000000000003",
    }
    if preflight_problem("exclusive", started, "grok"):
        sys.exit("a started exclusive session should pass preflight")
    untrusted_session = dict(started, events=untrusted)
    if not preflight_problem("available", untrusted_session, "grok"):
        sys.exit("available without speedread__ should abort")

    print("grok gate fixtures ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
