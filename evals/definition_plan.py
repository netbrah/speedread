"""Pre-registered sample sizes for the agent half of the definition-token gate.

These counts were chosen before any slimming and before the Grok runs. Do not
change them after seeing a result. `definition_cost.py power` prints the
arithmetic this file is the input to.
"""

# 16 tasks, 4 tools, so a regression in one tool is not diluted across the pool.
TASKS_PER_TOOL = 4
TOOLS = ("read", "search", "map", "trace")

# 4 * 70 = 280 trials per tool. At two-sided α=0.05 this is about 80% power for
# an argument-validity drop 0.98 → 0.93, and about 92% power for a selection or
# task-success drop 0.90 → 0.80. See definition_cost.py power.
TRIALS_PER_TASK = 70

# First-call probe: one fixed prompt, no tool use, three conditions interleaved.
# 200 sessions per condition is 80% power for a 200-token shift in the paired
# (available − baseline) mean when the SD of that difference is up to ~1010 tokens.
PROBE_SESSIONS_PER_CONDITION = 200
PROBE_CONDITIONS = ("baseline", "available", "exclusive")

ALPHA = 0.05
POWER = 0.80

# Planning base rates. The Grok baseline recomputes the drop that the realized
# rate and this n can detect; it does not keep these rates if they were wrong.
SELECTION_P0 = 0.90
SELECTION_P1 = 0.80
SUCCESS_P0 = 0.90
SUCCESS_P1 = 0.80
ARGUMENT_P0 = 0.98
ARGUMENT_P1 = 0.93

# Minimum paired mean shift in full first-call input tokens that the probe is
# sized to catch. Not a measurement.
FIRST_CALL_DELTA_TOKENS = 200

PROBE_PROMPT = "Reply with the single word pong. Do not call any tools."
