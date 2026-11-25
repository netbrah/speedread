# Pre-registered sample size

Two-sided test, α = 0.05, target power 80%. Binary n is trials per arm
(Phase 1 exclusive vs a later exclusive re-measure). First-call n is paired
sessions per condition. Planning rates are assumptions, not measurements;
the Grok baseline recomputes the detectable drop from the rate it actually sees.

Per tool: 4 tasks × 70 trials = 280 trials.
Pooled exclusive sessions: 1120.
First-call probe: 200 sessions × 3 conditions = 600 short sessions, interleaved.

| Gate | Planning rates | n needed for 80% | Power at the planned n |
|---|---|---:|---:|
| tool selection, per tool | 0.90 → 0.80 | 196 | 0.917 |
| tool selection, pooled | 0.90 → 0.80 | 196 | 1.000 |
| task success, per tool | 0.90 → 0.80 | 196 | 0.917 |
| argument validity, per tool | 0.98 → 0.93 | 266 | 0.820 |
| argument validity, pooled | 0.98 → 0.93 | 266 | 1.000 |

First-call MDE: 200 tokens on the paired mean of full input
(uncached + cache read + cache creation) for available − baseline.
At n = 200, 80% power holds while the SD of that paired
difference is at most 1010 tokens.

| Assumed SD of (available − baseline) | n for 80% at a 200-token shift |
|---:|---:|
| 100 | 2 |
| 250 | 12 |
| 500 | 49 |
| 1000 | 196 |
| 2000 | 785 |

Per-tool tests are each at α = 0.05, not Bonferroni. A gate that blocks when any
one tool regresses would rather false-block than miss a one-tool break. The pooled
rate is reported too, because a 10 point drop on one tool of four is only 2.5 points
pooled and the pool test will not see it.

Spread rule: a comparison is inconclusive when the interquartile range of the
paired differences (or, for a rate, the bootstrap interval width) is larger than
the absolute effect. The min–max range is reported beside the median; it is not
the stop rule, because the range grows with n.

