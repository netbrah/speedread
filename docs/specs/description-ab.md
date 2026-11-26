# Description A/B harness (Phase 1)

The question this track has to be able to answer, with numbers a stranger can
recompute from the raw transcripts:

> When an agent is not told to use speedread, do different tool descriptions
> change (a) unprompted adoption and (b) task success, and at what latency,
> peak RSS, and token cost per answer, including on a Chromium-scale tree?

Phase 1 builds the harness, freezes the decision rule, and leaves a slot for
the current descriptions as the baseline arm. It does not pick a winning
description and it does not change the shipped model-facing text. Phase 2
(variant runs, a winner or an inconclusive call) waits for a human go-ahead
on the Phase 1 pull request.

The live baseline has not been run. This environment has no `grok` binary.
`evals/results/description-baseline/PENDING.md` says that, and names the
command. The numbers below are the pre-registered plan, not results.

The harness tool set is **map, search, and read**. `trace` is being removed
in a separate PR; this one does not edit `TRACE_DESC` or the other shipped
description constants. `context` (PR #6) was closed without merging, so it is
not a tool and not an adoption event. The process Grok launches is
`evals/mcp_allow.py` in front of `speedread mcp`. It drops every other tool
from `tools/list` and refuses a call to one, so the catalog is map, search,
and read. A preflight lists tools through that proxy and aborts the run if
anything else is present. `--deny MCPTool(speedread__trace)` does not hide
the tool: on grok 1.0.92 it only blocks a later call, and `available_commands`
plus `search_tool` still show `speedread__trace`. `--disallowed-tools`
removes built-in ids only. The no-speedread arm denies
`MCPTool(speedread__*)`. A baseline trial that still calls `map`, `search`,
or `read` is an invalid deny leak.

## Current path

An agent trial today is one of two Python drivers, both talking to GitHub
Copilot CLI:

| Piece | Role |
|---|---|
| `evals/agent_eval.py` | Suite 3 and 3b. `copilot -p … --output-format json`. Conditions `baseline`, `speedread` (built-in readers removed), `dropin` (speedread added, no guidance). |
| `evals/coding_eval.py` | Suite 4. Conditions `baseline`, `available` (no guidance), `preferred` (one guidance sentence), `exclusive`. |
| `evals/speedread-mcp.json` | `{"command": "speedread", "args": ["mcp"]}`. No description override. |
| `src/server.rs` | `INSTRUCTIONS`, `READ_DESC`, `SEARCH_DESC`, `MAP_DESC`, `TRACE_DESC` are the strings `tools/list` and `initialize` return. The `#[tool]` macros bake them into a `ToolRouter`. |
| `evals/stats.py` | Trial-resampled 95% bootstrap CIs for tokens and time. |

`used_speedread` on the old Copilot path was a binary "did any of the four
`speedread-*` names appear". There was no step share in Suite 3. Suite 3b's
committed run had no drop-in arm and its tasks target `trace`, so the
baseline does not run `evals/relationship_tasks.json`. Copilot remains
available (`--description` on both drivers). It is not the Phase 1 baseline.
The committed `evals/RESULTS.md` numbers are a different harness (Copilot
CLI, claude-sonnet-5) and are not reused as the baseline.

Grok CLI is the primary runner. Headless is `grok -p "<prompt>"`. Tool calls
are the NDJSON from `--output-format streaming-json` (one ACP-derived object
per line: `tool_call`, `tool_call_update`, `text`, `usage`, `end`). MCP
servers are `grok mcp` / `[mcp_servers.*]` in the trial's `GROK_HOME`.
`--trust` is on every invocation: a fresh checkout does not expose project
MCP tools without it. `--always-approve` is how an unattended run proceeds.
Per-session tokens and cost are `grok usage <session-id>` (`costUsdTicks` is
USD × 10^10; a missing cost is unreported, not zero). Built-in readers are
removed with `--disallowed-tools` (ids such as `read_file`, `grep`,
`list_dir`; the live shell id is `run_terminal_command`). MCP tools are
catalog keys `server__tool`. The model invokes them through `use_tool`.
Adoption reads that inner name. `--deny MCPTool(...)` blocks a call and
does not remove the key from the catalog; `evals/mcp_allow.py` does.
Exclusive denies `read_file`, `grep`, `list_dir`, `run_terminal_command`,
`search_replace`, and `write`. grok 1.0.92 ignores
`[toolset.search_replace] skip_read_before_edit` and refuses to start when
`search_replace` is still enabled and `read_file` is not. That deny list is
the one that starts on this CLI (the same list as the definition-token
harness). The trial config does not write `skip_read_before_edit`. If any
session fails to initialize, the driver records that trial and aborts the
run.

## Two ways to load a variant

The shipped strings have to stay the default. Variant text needs one owner,
and a caller should pass an id rather than the prose.

**Shape A — file behind an env var, applied inside the server.**
`SPEEDREAD_DESCRIPTION_FILE` points at `evals/descriptions/<id>.json`. On MCP
startup the server replaces instructions and the named tool descriptions on
the instance `ToolRouter` (`#[tool_handler(router = self.tool_router)]`,
because the macro's default rebuilds the router from the constants and would
ignore the edit). Unset, the variable is not read and `tools/list` is the
macro output. A file that names an unknown tool other than `trace` or
`context`, or that sets neither instructions nor tools, exits before the
server accepts a connection. A missing `trace` key is valid. A leftover
`trace` or `context` key is dropped by `evals/desc_variant.py` before the
env var is pointed at the file, and the server also skips those two names
when they are not in the router, so the same variant works before and after
the removal PR. Schemas are not in the file, so a variant cannot change the
`search` `output` enum or any other argument.

**Shape B — a stdio proxy in front of `speedread mcp`.** The proxy rewrites
`initialize` and `tools/list` and forwards every other message, including
server-initiated `roots/list`. The binary would not contain an override path.
Every caller would have to remember to launch the proxy, and the proxy would
be a second implementation of the framing the server already speaks.

**Shape C — each driver splices strings.** Rejected. Callers would own the prose.

Shape A is how a variant's text is applied. The JSON file is the only copy of
a variant. `evals/desc_variant.py` is the only code that turns an id into the
env var (Grok `config.toml` or a Copilot MCP config). `current` is not a
file: the env var is left unset. The process Grok launches is
`evals/mcp_allow.py`, which forwards to `speedread mcp` and removes every
tool except map, search, and read from `tools/list`. That filter stays after
`trace` is deleted; it does not rewrite description text. `SPEEDREAD_RSS_LOG`
is a single write of `VmHWM` at exit and does not change answers. The sibling
track that is shortening the definitions drops `evals/descriptions/lean.json`
and passes `--description lean`. That file is not in this change, and the
constants in `src/server.rs` are not edited.
`tests/fixtures/default_tool_text.json` locks the `map`, `search`, and `read`
descriptions byte for byte. Instructions are checked separately: the fixture
string, or that string with `` `trace` follows callers, callees, references and implementations; ``
removed (the trace-removal PR's only instructions edit). There is no default
variant file. A second test checks that an override changes only the named
strings and not the schema or the `read` answer. The same `read` via the CLI
is identical with the env var set and unset, because the CLI never consults
descriptions.

`list_tools` and `call_tool` now use the instance router. With the env var
unset that router is the one `Self::tool_router()` built at startup, which is
what the macro used to rebuild on each request.

## Metrics (fixed before any live trial)

**Trial-level adoption.** A trial used speedread if any tool call's effective
name is `speedread-read`, `speedread-search`, `speedread-map`, or the Grok
catalog forms `speedread__read`, `speedread__search`, `speedread__map`.
`use_tool` is unwrapped to its `tool_name` first. `search_tool` does not
count. Any other speedread leaf (`trace`, `context`, or a later tool) is
listed on the trial as `other_speedread_tools` and is **not** an adoption
event and **not** a speedread step. A call that fails or is denied still
counts when its leaf is one of the three: the model asked. This is read
from the transcript, not from the model's answer.

**Step-level adoption.** One tool call is one step. The denominator is
code-reading steps only.

| Class | What counts |
|---|---|
| `speedread` | `read`, `search`, or `map`, including through `use_tool` |
| `native` | Copilot `view` / `grep` / `glob` / `rg`; Grok `read_file` / `grep` / `list_dir` / `glob` / `glob_file_search` |
| `shell` | a shell step (`bash`, `shell`, `run_terminal_command`, `run_terminal_cmd`) whose command is a reader |
| `other` | not a code-reading step |

A shell step is classified as a whole, not per pipeline segment. The command
is split on `&&`, `||`, `;`, `|`, and newlines, outside quotes. If any
segment is a test, build, or VCS write (`go test` / `go build` / `pytest` /
`cargo test` / `git commit` / …), the step is `other`, even if it also runs
`cat`. Otherwise a segment whose command word is `cat`, `head`, `tail`,
`sed` (except `sed -i` / `--in-place`), `less`, `more`, `grep`, `rg`, `ag`,
`find`, `ls`, `tree`, `nl`, `awk`, `wc`, `bat`, `gawk`, `nawk`, `mawk`, `fd`,
`exa`, `eza`, `tac`, or `git grep` / `ls-files` / `ls-tree` / `show` / `diff`,
makes the step `shell`. Edits, `search_tool`, web tools, and subagents are
`other`. Shares are null when a trial has no code-reading step, so a
test-only trial is not a 0% speedread share.

`python3 evals/adoption.py check <run>` recomputes these fields from the
saved transcripts and exits non-zero on a mismatch.

**Success.** Suite 3, the pin tasks, and the Chromium questions use their
regex graders: pass@1, pass^k, and mean score (fraction of graders that hit).
Suite 4, when it is run, uses the existing grader: the repo's test suite is
green and no protected test file was modified. Suite 4 is reported and is
not in the ship rule (see sample size).

**Unprompted.** The task prompt does not contain `speedread`, `trace`,
`MCP tools`, or `use the MCP`. The condition is `dropin` (Q&A) or
`available` (coding): speedread is added next to the built-in tools and no
guidance sentence is appended. `preferred` and `exclusive` / `speedread` are
controls. The drivers refuse to start an unprompted arm whose prompt matches
those phrases. Subagents and web tools are removed in every arm
(`--disallowed-tools Agent,web_search,web_fetch`) so a subagent cannot hide
the tool calls the adoption metric counts.

**Failures.** Every scheduled trial is a line in `trials.jsonl`. `error` and
`timeout` stay in the adoption denominator (a crash did not use speedread
unless the partial transcript shows a speedread call). `skipped` (repo
missing, prompt lint, budget not the issue here) is counted in the summary
and left out of the CI. Nothing is dropped because a trial threw.

## Decision rule (fixed before the run)

A later variant is a **ship candidate** only when, on **both** models, all of
the following hold. The interval is the task-clustered 95% bootstrap from
`evals/stats.py --adoption` (tasks are resampled, trials inside a task stay
together):

1. The CI for (drop-in adoption of the variant − drop-in adoption of
   `current`) lies entirely above **+0.25**.
2. The CI for the same difference in step-level speedread share lies entirely
   above **+0.15**.
3. Success does not block: the CI for (mean score of `current` − mean score
   of the variant) does **not** lie entirely above **+0.10**. A drop we cannot
   confidently call larger than 10 points does not veto. A drop we can is a
   no-ship.

**No-ship** when the adoption CI on the primary model lies entirely below
+0.25 (the gain is confidently under the bar), or when success blocks.

**Inconclusive — stop, do not ship** when any of these is true:

- The CI half-width for the adoption difference is **≥ 0.25** (the run cannot
  resolve the bar; more of the same noise will not be read as a win).
- The CI covers +0.25, so the data do not settle whether the gain clears it.
- The two models disagree (one clears the bar and the other does not).
- A repeat of the same arm, same tasks, same n, differs from this frozen run
  by ≥ 0.25 in clustered adoption. The spread is then larger than the effect
  the rule is willing to call real.

The no-speedread arm is a negative control. If any baseline trial calls a
speedread tool, the deny failed and the run is invalid, not a 0% result.

`exclusive` (one trial per task per model) answers "could the task be solved
with the built-in readers removed". It is not in the adoption CI.

## Sample size

The unit that matters is the task. If a model either reaches for speedread on
a task or does not, extra trials of that task mostly repeat. The power
calculation assumes an intra-task correlation of 0.5.

Decision set: Suite 3 (10) + pin tasks (13) + Chromium Q&A (14) = **37
tasks**. Suite 3b is not in the set: those four tasks target `trace`.
**5 trials** per task. Per arm, per model, that is **185 trials**.
Design effect = 1 + (5 − 1) × 0.5 = **3**. Effective n = 185 / 3 ≈ **61.67**.
Stacking more trials on the same tasks cannot push effective n past about
2 × 37, because the intra-task correlation dominates.

The alternative the study is sized for is the one this track is about: a
near-zero unprompted baseline moving by a lot, but not all the way to the
historical "one guidance sentence → everyone uses it" jump. Take current
adoption 0.05 and variant adoption 0.55 (difference **0.50**).
Variance = 0.05×0.95 + 0.55×0.45 = **0.295**.
SE = √(0.295 / 61.67) ≈ **0.0692**. A 95% half-width is ≈ **0.136**, which is
under the 0.25 noise floor, so a run of this size is allowed to resolve the
rule. The ship rule needs the lower bound above 0.25, which when the truth
is 0.50 happens with probability **Φ(1.65) ≈ 95%** on one model.
Both models have to clear the bar, and the models are independent, so joint
power is about **0.95² ≈ 90%**.

At the worst-case variance for a 0.50 gap (0.375 vs 0.625, variance 0.469)
the half-width is ≈ 0.17, still under 0.25, and single-model power falls to
about **82%** (joint about **67%**). If a real run lands in that regime, the
half-width rule is what stops a shaky call. The pre-registered alternative
is the low-baseline one, because that is the historical shape (drop-in
adoption was 0/10 on the old Copilot run, which this design does not treat
as the new baseline).

Two models are part of the rule, not a pooled n. Pooling would hide a
one-model fluke. The replication has to clear the same bar. Joint power is
the square of the single-model figure for that reason.

Suite 4 has 8 tasks. Even at 4 trials, design effect 2.5, effective n ≈ 13.
A 0.25 pass-rate gap at a high base rate has a half-width around **0.31**,
which is already past the noise floor. Eight tasks cannot resolve that bar
no matter how many trials are stacked on them, once correlation is high
(effective n cannot exceed a small multiple of 8). Suite 4 is therefore
**descriptive**. The entry point runs it (8 × 4 × 2 arms × 2 models) when
`go` and a flask virtualenv are available, and the ship rule ignores it.
Coding prompts stay unprompted (`available`, no guidance sentence).

## Run count and estimated spend

Spend is not a cap. The estimate is so the operator can see the order of
magnitude before the command starts. It is not a measurement.

| Block | Trials |
|---|---|
| Decision set, 37 tasks × 5 × 2 arms (`baseline`, `dropin`) × 2 models | 740 |
| `exclusive` control, 37 × 1 × 2 models | 74 |
| Suite 4 descriptive, 8 × 4 × 2 arms × 2 models | 128 |
| **Total** | **942** |

Planning prices, extrapolated from the published headless example (about
$0.013 for one ~50k-token turn) and from the old Suite 3 shape (a few turns,
on the order of 80k input tokens), not from a Grok invoice:

- small-repo Q&A trial ≈ $0.30 (range $0.10–$0.50)
- Chromium Q&A trial ≈ $1.50 (range $0.50–$3)
- coding trial ≈ $1.00

`evals/run_baseline.py` prints the same total from the task files (currently
about **$742**), with a range of about **$300–$1800**, because the
per-trial price is an extrapolation. 460 small decision trials × $0.30,
280 Chromium decision trials × $1.50, 74 exclusive trials (23 small × 2 ×
$0.30 plus 14 Chromium × 2 × $1.50), 128 coding trials × $1.00.
`costUsdTicks` from `grok usage` replaces this estimate after the run. A
missing tick count is reported as unreported cost, not as $0.

Primary model: `grok-4.7`. Replication: `grok-4.7-build-fast`. `grok usage`
reports the primary as `grok-4.7-build`; that alias is recorded and the
`-m` flag is not rewritten. `run_baseline.py` runs `grok models` and exits
if either id is absent. It does not substitute. Pass `--models` with ids
the CLI actually lists if those two names have moved.

Token source of record is `grok usage` when the session ledger is present.
`inputTokens` already includes cache; `cache_read` is a subset, not an
addend; total is `totalTokens` or input + output, never input + cache +
output. If the ledger is missing, the stream `end` event is the fallback:
its `input_tokens` is uncached, and the driver folds `cache_read` and
`cache_creation` into the inclusive input so both sources mean the same
thing. A trial records `usage_source`. A present ledger is not mixed with
stream buckets.

`claude-sonnet-5` via Copilot is a different runner. New Grok numbers are
not comparable to `evals/results/` and are not presented as an update of
those headlines.

## Tasks and pins

`evals/corpora.json` is the pin. `evals/fetch_corpora.py --dest <bench>`
does a depth-1 fetch of each commit and checks a sentinel line the grader
depends on.

| Repo | Commit | Why this one |
|---|---|---|
| flask | `d73fa1cdcbd8b1465c151db8924ba58b1dd14e35` | `app.py:628` is the shell-context dict; dispatch/finalize/preprocess lines match Suite 3 and 3b |
| gin | `0f09c3a9b4626d9fe9979cf4d0521d6ae32ab646` | `go 1.26.0`; `AbortWithStatus` lines; 18 `Render` methods; Suite 4 injection sites present |
| ripgrep | `435f59fc4b43af3ab32f34d53fa34978f393fe52` | `defs.rs:2362` is `struct FixedStrings` (file is 8,161 lines); `walk.rs:1428` is `pub fn run` |
| zod | `19c40d2072288b4cafec851e9d056a72a88e5973` | already the CLI pin in `scripts/fork-eval.py`; `types.ts:583-584` are `ip` and `cidr` |
| vscode | `2242ebbb54efeeb0129e08e919e7e8d43033cd83` | tag 1.139.0, published the day of the old Copilot run; `textModel.ts:487` is the null check |
| Alamofire | `dea4bab91767a971d6e112bc955c6a82295589b7` | `Session.swift:136` is `startRequestsImmediately: Bool = true` (file is 1,441 lines) |
| swift-argument-parser | `d225b6fe8aed45478d1e36e770bf269c5d48d3a9` | `ParsableCommand.swift:16` is the `configuration` requirement |
| chromium | `3a7520d2d79d6569d3a11f968d32b20665fa4db3` | full object name of the commit `evals/fork/big-chromium.txt` and `docs/specs/eval.md` cited as `3a7520d2d79d`. A prefix is not fetchable from googlesource |

Chromium, exactly:

```bash
# evals/fetch_corpora.py does this.
git init chromium && git remote add origin https://chromium.googlesource.com/chromium/src.git
# sparse-checkout (non-cone): include the tree, then drop the extensions in corpora.json
# (png, jpg, jpeg, gif, webp, ico, icns, bmp, psd, mp3, mp4, wav, ogg, webm, mov,
#  avi, flac, ttf, otf, woff, woff2, eot, zip, tar, gz, tgz, bz2, xz, 7z, rar,
#  apk, aab, dmg, iso, pdf)
git -c protocol.version=2 fetch --filter=blob:none --depth 1 origin 3a7520d2d79d6569d3a11f968d32b20665fa4db3
git checkout --detach FETCH_HEAD
```

The checkout of this SHA with those extensions omitted measured **476,800
files and 5.7G**. `docs/specs/eval.md` records **474,447 files and 4.5 GB**
for the same commit under an earlier omission pass. The script writes
`chromium/CHECKOUT.txt` with the commit, the file count, and `du`. CLI
timings are not comparable to `evals/fork/big-chromium.txt`: the file counts
differ, so that comparison is a different tree.

Chromium questions (`evals/chromium_tasks.json`, 14) keep the set balanced:
a tiny file (`chrome/VERSION` is 160.0.8155.0, fetched from the commit),
small headers (`base/command_line.h` lines 16 and 38, `url/gurl.h` lines
92 / 159 / 232 / 236 / 240 / 252, `base/version.h` lines 45 / 54 / 63), and
two large-file line numbers. `RenderFrameHostImpl::DidCommitNavigation`
begins at line **18236** (18234 is the comment above it).
`RenderFrameImpl::DidCommitNavigation` begins at line 4029. `--check`
confirms those source lines contain the symbol. The small-repo control
tasks stay in the decision set: Suite 3 (10) plus `evals/pin_tasks.json`
(13), which adds
line questions on flask, gin, ripgrep (`struct LineNumber` at `defs.rs:3976`),
Alamofire, swift-argument-parser (`_commandName` at `ParsableCommand.swift:23`),
and zod. None of the prompts name speedread, trace, or MCP.

## Perf protocol

For any number that claims a speed or memory difference:

- Binary: `cargo build --profile fast` (release optimizations, no LTO), or a
  `--release` binary if the operator builds one. The profile is named next to
  the number. The description override is compiled in; the baseline does not
  set `SPEEDREAD_DESCRIPTION_FILE`.
- CLI large-tree queries: `python3 scripts/fork-big-eval.py chromium <binary>`,
  **5 runs**, median and min–max, error count. The script already keeps a
  best-of-three per query; the entry point asks for that invocation to be
  repeated across five process starts when a claim is made. One invocation is
  not a claim.
- Agent trials: input and output tokens and `cost_usd` from `grok usage`;
  wall-clock around the `grok` process; model API time from the stream's
  `duration_api_ms` when Grok emits it, otherwise **unreported** (wall is not
  substituted). Wall is labeled noisy when `/proc/loadavg` exceeds `nproc`.
- Peak RSS of the speedread process: `SPEEDREAD_RSS_LOG` writes `VmHWM` at
  MCP exit (`getrusage` on machines without `/proc`). The baseline arm has no
  server, so its RSS is null.
- Machine line: `nproc`, RAM, OS, load average, written to `machine.json`.
- A claim with no named limiter, or whose range covers both sides of the
  comparison, is **inconclusive — no limiter** or **inconclusive — spread
  exceeds effect**. A choice between two binaries is never a one-run ballpark.

The entry point records the CLI command in `<run>/cli-chromium.txt`. It does
not launch that comparison itself, because five whole-tree passes are a
separate measurement from the agent trials and the file count has to be
checked first.

## Hypothesis (not a result)

Clearer "when to pick this instead of grep or cat" text will raise unprompted
adoption without lowering success. That is a guess. The rule above is what
would make it a result, or an inconclusive one.

## What this phase does not do

- It does not edit `INSTRUCTIONS`, `READ_DESC`, `SEARCH_DESC`, `MAP_DESC`,
  `TRACE_DESC`, or the skill `description`. `trace` leaves in another PR.
- It does not change the `search` `output` enum or its shipped description.
- It does not add an LSP/SCIP layer or a `context` tool. PR #6 is closed
  without merging. A later speedread tool is recorded as
  `other_speedread_tools` and does not count as adoption until this spec is
  revised.
- It does not run Phase 2 variants, the live baseline, or declare a winner.
- It does not rewrite `evals/RESULTS.md` or the README charts. There is no
  new live number to put there.
- It does not change the GitHub Actions workflows. `ci.yml` and `linux.yml`
  already trigger on `pull_request`, and this fork has never recorded a
  workflow run. Enabling Actions is an owner setting
  (the repository Actions page, or Settings → Actions → Allow all actions),
  not a YAML change.

## Reproduce

```bash
python3 evals/run_baseline.py --bench /path/to/bench
```

An on-prem OpenAI-compatible gateway uses the same command with
`SPEEDREAD_BASE_URL`, `SPEEDREAD_MODELS`, and `SPEEDREAD_API_KEY_ENV`.
The steps, the preflight, and why those token numbers are not grok-4.7
numbers are in [`evals/RUN-ON-PREM.md`](../../evals/RUN-ON-PREM.md).
Grok CLI reads the endpoint from the trial `config.toml` (`[model.*]`
`base_url` and `env_key`), which is how 1.0.92 selects a custom model.

The trace-not-visible and exclusive-starts check, on a machine with grok
1.0.92. It is two short sessions plus the filtered `tools/list` preflight.
It is not the baseline:

```bash
python3 evals/grok_eval.py --live-check --speedread "$PWD/target/fast/speedread"
```

Needs Rust 1.90+, a C compiler, Go, Grok CLI signed in or `XAI_API_KEY`, and
disk for the pins (Chromium is the large one). The command creates
`<bench>/venvs/flask` from `evals/flask-tests.txt` (hashes exported from the
pinned flask `uv.lock`) when that venv is missing, then runs Suite 4 as well
as the decision set: 942 trials. `--venvs <dir>` uses another directory.
`--dry-run`, or a machine without `grok`, runs
`evals/test_description_harness.py` against the hand-written stream in
`evals/fixtures/grok_streaming/` and does not write a baseline.

Copilot, unchanged except for `--description` and the shared classifier:

```bash
python3 evals/agent_eval.py --bench <bench> --conditions baseline,speedread --dropin-trials 1
python3 evals/coding_eval.py --bench <bench> --venvs <venvs> --trials 2
```
