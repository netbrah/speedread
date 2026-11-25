"""The product tool set and the pre-registered agent-gate sample size.

TOOLS is the only list of speedread tools this gate scores. trace is being
removed in another PR and is not part of the set. context is not shipping.
A live server may still advertise trace; the harness can hide it. See
mcp_allow.py. `definition_cost.py power` prints the arithmetic.
"""

# Product tools, in the order the server lists the ones that are staying.
TOOLS = ("map", "search", "read")

# Advertised by current main. A surface check tolerates these in addition to
# TOOLS until they disappear. They have no tasks. The 3-tool measurement
# passes an empty extra set so a still-listed trace fails the preflight.
TOLERATED_EXTRA = ("trace",)

TASKS_PER_TOOL = 4

# 4 tasks * 70 trials = 280 per tool. That is the same per-tool n as the
# 4-tool plan, so the per-tool CI half-width does not change when trace
# leaves. At two-sided α=0.05 this is about 80% power for an
# argument-validity drop 0.98 → 0.93, and about 92% power for a selection or
# task-success drop 0.90 → 0.80. Pooled sessions fall from 1120 to 840.
TRIALS_PER_TASK = 70

PROBE_SESSIONS_PER_CONDITION = 200
PROBE_CONDITIONS = ("baseline", "available", "exclusive")

ALPHA = 0.05
POWER = 0.80

SELECTION_P0 = 0.90
SELECTION_P1 = 0.80
SUCCESS_P0 = 0.90
SUCCESS_P1 = 0.80
ARGUMENT_P0 = 0.98
ARGUMENT_P1 = 0.93

FIRST_CALL_DELTA_TOKENS = 200

PROBE_PROMPT = "Reply with the single word pong. Do not call any tools."


def speedread_wire_name(tool: str) -> str:
    return f"speedread__{tool}"
