# Definition-token cost

Phase 1 baseline. No model-facing string was edited. Phase 2 does not start until this note is reviewed.

**Headline:** the live `tools/list` result is **1578 o200k_base tokens** (cl100k_base **1551**), median of 5 launches, range **0**. Server `0.2.1-netbrah.1` at `7478ebc4e93ea1faa11f18148669bb4a175b06d2`. Payload sha256 `457a2cb09ba4a2a7b58bffdcbbaeaae5cd948a42c9221ed63579aac5e7bd3960`.

That number is the exact JSON of the `result` object, sliced from the line the server wrote, tokenized with tiktoken 0.14.0 `Encoding.encode(text, disallowed_special=())`. It is not a schema rebuilt from `src/server.rs`. Raw lines: `evals/fork/definition-tokens/tools-list-run-*.jsonl`. Table: `evals/fork/definition-tokens/tools-list-tokens.md`.

## Metrics (fixed before any slimming)

Three metrics. The first two were run on this machine. The third's agent half is sized here and left pending.

1. **Definition-token cost.** Tokens of the live `tools/list` result, o200k_base and cl100k_base. Broken down by tool and by description vs `inputSchema` vs annotations vs the rest of that tool. Server `instructions` are counted from `initialize` and kept out of the headline, because they are not in `tools/list`. The wire line (jsonrpc envelope included) is reported beside the result. A fifth tool, whatever its name, is another row. `python3 evals/definition_cost.py self-test` checks that on a synthetic list that includes `context`.

2. **Correctness gate.**
   - *Canonical argument validity* (this machine): for each of 16 fixed tasks, the canonical arguments are sent to the live server. A pass is an accepted call whose response matches that task's graders. Three calls that omit a required field are counted separately and must be rejected. Tool-selection is not scored here and is not filled in with a fake rate.
   - *Agent tool-selection, argument validity, task success, and first-call input delta* (Grok machine): the same 16 tasks, exclusive of the harness file tools, plus a first-call probe. Task success is the answer graders. Pending. Not invented.

3. **Chromium-scale perf.** The query list in `scripts/fork-big-eval.py` (`REPOS["chromium"]["queries"]`), on the fetched Chromium tree. Per query: wall (first, median, min–max), peak RSS, answer tokens under the same two tokenizers. This is a non-regression gate for a later slim. A definition-token edit cannot honestly move these times. A later gap larger than this run's min–max is an observation error, not a win.

## What is and is not in the 1578

In the result: each tool's name, description, `inputSchema` (including per-field descriptions and the JSON Schema dialect URL the SDK added), annotations, and the result object's own punctuation.

Not in the result:

- the jsonrpc envelope and id (the whole response line is 1592 o200k tokens, 14 more than the result)
- `initialize` instructions (145 o200k tokens of the rooted text; 185 with the rootless note; one rootless launch)
- whatever a client adds when it turns definitions into a model prompt
- `skills/speedread/SKILL.md`

Attributed tokens sit on the bytes of the result, so they sum to 1578. A token that crosses a field boundary is one of 23 boundary tokens. `result.other` is 4. Isolated field counts are the field encoded alone and do not add up.

| Tool | description | inputSchema | annotations | other | sum |
|---|---:|---:|---:|---:|---:|
| map | 57 | 165 | 29 | 10 | 261 |
| read | 244 | 165 | 29 | 9 | 447 |
| search | 94 | 383 | 29 | 9 | 515 |
| trace | 139 | 151 | 29 | 9 | 328 |

Largest single bucket: `search.inputSchema`, 383. Largest description: `read`, 244. The largest property descriptions, encoded alone (they already sit inside `inputSchema`): `search.ast` 49, `search.output` 34, `read.mode` 31. The `base_dir` sentence is byte-identical on all four tools, 17 o200k tokens a copy. `read.budget` and `search.budget` share a 9-token sentence. The dialect URL `https://json-schema.org/draft/2020-12/schema` occurs 4 times inside `inputSchema`, 13 tokens encoded alone.

Flags on the measured server: `--deadline-ms 10000 --cache-mb 256`. Those are the code defaults (`src/engine.rs`). Binary `target/fast/speedread`, sha256 `656a455eda60145c52c460742a77e023cfdfc2e29d1c92562a9e677765a0e27b`, built with `cargo +stable build --profile fast` (opt-level 3, no LTO, codegen-units 16; the profile the harness documents, not a debug build). rustc 1.100.0.

Limiter: tiktoken is deterministic. The five payloads were byte-identical, so the range is 0. There is no run-to-run noise to hide a later change in this number.

## Canonical argument validity

**16/16 pass**, one run, on `evals/fixtures/definition-gate`. **3/3** missing-required-field calls were rejected (`search` without `pattern`, `read` without `targets`, `trace` without `target`). Responses: `evals/fork/definition-tokens/argument-responses/`.

Tool-selection rate: not measured. Task-success rate of an agent: not measured. Both are the pending Grok run.

## Agent gate (pending)

Spend is not the constraint. The gate has to catch a real regression on one tool, not only a pooled rate: a 10 point drop on one tool of four is 2.5 points pooled, and the pool test will not see it.

Pre-registered in `evals/definition_plan.py`, printed by `python3 evals/definition_cost.py power`, saved at `evals/fork/definition-tokens/power.md`.

- 16 tasks, 4 per tool (`read`, `search`, `map`, `trace`).
- **70 trials per task** on the exclusive condition: **280 trials per tool**, **1120** task sessions.
- Two-sided α = 0.05. At the planning rates, 280 trials is **92% power** for a drop 0.90 → 0.80 (selection and task success) and **82% power** for a drop 0.98 → 0.93 (argument validity). The n those rates need for 80% is 196 and 266. Per-tool tests stay at α = 0.05 rather than Bonferroni: a gate that blocks when any tool regresses would rather false-block than miss a one-tool break.
- **200 probe sessions per condition** (`baseline`, `available`, `exclusive`), interleaved, one fixed prompt that forbids tool use, `--max-turns 1`. Primary first-call number is available − baseline on full turn-1 input (uncached + cache read + cache creation, from `grok usage <session> 1`). At n = 200 that is 80% power for a **200 token** shift while the SD of the paired difference is at most **1010** tokens. If the observed SD is higher, the report states the shift this n can actually detect and does not keep 200 by inertia.
- Exclusive disallows the harness file tools (`read_file`, `grep`, `list_dir`, `bash` on Grok; `view`, `grep`, `glob`, `rg`, `bash` on Copilot) so a shell read cannot pass as selection. Sub-agents and web tools are off in every condition. This is stricter than Suite 4's exclusive, which kept bash, and it is not the unprompted-adoption arm.
- A trial whose tools list still offers those file tools is excluded, not scored as pass or fail. If that happens often, selection is inconclusive.
- Stop rule: a first-call comparison is inconclusive when the interquartile range of the paired differences is larger than the absolute median difference. Min–max is reported and is not the stop rule, because the range grows with n.
- If the observed selection or success rate is far from 0.90, or argument validity far from 0.98, recompute the detectable drop from the observed rate and this n. Do not keep the planning MDE.

The planning rates are assumptions. Nothing here is a Grok measurement.

One command, on a fresh checkout with a built binary and Grok CLI 1.0.92:

```bash
python3 evals/definition_cost.py agent --harness grok \
    --binary "$PWD/target/fast/speedread" --model <id from grok models> --yes
```

Copilot, same gate: `--harness copilot`. Without `--yes` the command prints the plan and the argv and does not start sessions. Workdirs are created under `/tmp` as their own git roots so `grok mcp add --scope project` cannot write into this repository.

Grok facts this harness depends on, from the CLI docs rather than from a run here: `streaming-json` emits `tool_call` events with `toolName` and `rawInput`; `grok usage <session-id> 1` prints turn 1; headless `input_tokens` is the uncached bucket ([headless mode](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md), [sessions](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/17-sessions.md)). MCP tools are namespaced `server__tool`, and `tool_definitions.json` omits those entries because the model reaches them through `search_tool` and `use_tool` ([MCP servers](https://docs.x.ai/build/features/mcp-servers)). The harness resolves both shapes and saves the raw `grok usage` JSON. A field it cannot find stays null.

Published Copilot first-call medians (Suite 4, claude-sonnet-5, not re-measured): baseline 17,950, available 20,151, exclusive 18,858. That is a harness delta, not this `tools/list` count. See `evals/RESULTS.md`.

## Chromium

Corpus: `https://github.com/chromium/chromium` commit `3a7520d2d79d6569d3a11f968d32b20665fa4db3`, codeload tarball sha256 `928a0510a87d927c5d8547c12df282ee15890814765d26ec9bd2395c6516eb7d` (1,500,794,986 bytes), extracted, then image, media, font, and archive extensions deleted, then `git init`. Before deletion: 517,513 files, 4,478,702,917 bytes. After: **472,579 files, 3,570,206,786 bytes**. Required query paths are present (`content/browser/renderer_host/render_frame_host_impl.cc` and the `content` trees). Provenance: `evals/fork/definition-tokens/chromium-provenance.json`. Reproduce with `python3 scripts/fetch-chromium-corpus.py`.

The historical note (474,447 files, 4.5 GB, 2 vCPU) is a different machine and a slightly different omission. It is not the gate. This tree is the gate.

Machine for the timed runs: 4 cores, Intel Xeon (KVM), 16 GiB `MemTotal`, balloon about 6 GiB at the start of the run (it had been 10 GiB before the extract), Linux. `nproc` = 4. Load average before the timed runs was 0.13. Build profile as above. Flags `--deadline-ms 10000 --cache-mb 256`, cwd `/`, `--root` the corpus. Five runs of each query, all runs of one query before the next, no early stop. Page cache was not dropped. `Cached` was about 3.7 GiB before the timed runs, just after writing a 3.57 GiB tree, so this is a warm-from-extract run, not a cold-cache run. Answers and the per-query table: `evals/fork/definition-tokens/chromium/`.

Full table: `evals/fork/definition-tokens/chromium/summary.md`. Errors: 0 of 85 runs. Load before the timed runs was 0.13; `nproc` is 4. The load of 3.88 at the end is this run, not a foreign workload. `Cached` before the runs was 3,788,940 kB, balloon 6,144 MiB. After the runs `Cached` was 6,086,832 kB and the balloon was 4,096 MiB.

Verdict on latency and RSS: **no comparison yet** (one side). The noise floor is the min–max.

- Most queries sit near 174 MiB peak RSS on every run (min = max). `trace Run --depth 2` is the exception: median 512 MiB, range 504–713. A later RSS number outside a query's min–max is not a definition-token effect.
- Whole-tree literal search (`zzz_not_there_zzz`): median wall 1,057 ms (range 1,041–1,064), median CPU 4.20 s. Four cores in about one second of wall means the machine is busy for that second. The first run is not an outlier on this query.
- Two queries are warmup-bound. `search base::BindOnce` first 237 ms, then 96–115. `search -w ScopedObservation` first 2,671 ms, then 1,129–1,141. The min–max is that warmup. A later median has to leave the whole range before it is a latency change. The spread is larger than a small effect, so a small latency "win" on those queries is not measurable here.
- Answer tokens are identical across the five runs on 15 of 17 queries. `search base::BindOnce` is 5,178–5,813 because the 5,000-match cap stops on a different prefix of the walk each run (5,000 to 5,011 matches). `trace Run --depth 2` is 2,783–2,928 and ends incomplete because the name is in more files than a lookup examines, not because the 10 s deadline fired. `map` bytes differ by `1.1 GB` vs `1.2 GB` in one summary line and stay 2,814 tokens. The whole-tree `base::BindOnce` structural search also differs in bytes and stays 5,432 tokens.

Why this is not twice as fast, from runs that are not in the table:

- `strace -c` without `-f` only saw the main thread blocked in `futex`. The workers were not attached. That file is kept (`profile/strace.txt`) so it is not mistaken for the profile. `SPEEDREAD_STATS` on that same run: `walks=1 entries=471431 stopped=0 cancelled=0`, wall 1,128 ms under a tracer that was not following the workers. The walk finished.
- `strace -f -c` (`profile/strace-follow.txt`) did follow the workers and tripped the 10 s deadline (`stopped=1`, 257,492 entries). It is not a timing result. Of the syscall time in that partial walk: `futex` 33%, `read` 30% (805,252 calls), `openat` 12%, `statx` 11%, `close` 10%, `getdents64` 3%. The visible work is the walk. There is no second syscall phase of the same size. The timed command is the CLI, which never builds `tools/list`. A change that only edits definition text cannot move this wall time. If Phase 2 shows a large latency gap, the observation is wrong.

## Hypothesis (non-binding)

Checked against the live payload, not against a reconstruction:

- Most of the result is schema and the long `read` description. `search.inputSchema` (383) plus `read`'s description (244) are 627 of 1578. The whole `search` tool is 515; the whole `read` tool is 447.
- `base_dir`'s sentence is repeated four times. Instructions are real (145) and are not inside `tools/list`.
- 1578 and 145 encoded separately are 1723. The published Copilot available-minus-baseline first-call gap is 2,201 (20,151 − 17,950). 1723 is smaller than 2,201, which is consistent with client wrapping adding tokens the server does not send as one blob. It does not measure that wrapping. Grok's omission of MCP `server__tool` entries from `tool_definitions.json` means the live first-call delta on Grok can move by a different amount than 1578. That half stays pending.

## What a slim would touch

- `tools/list`: `#[tool]` names, descriptions, and annotations in `src/server.rs`, plus the `JsonSchema` impls. The live schema also carries `$schema`, which comes from the SDK rather than from the description strings.
- `initialize`: `ServerHandler::get_info` → `with_instructions`. `INSTRUCTIONS`, and `ROOTLESS_NOTE` only when the server has no root.
- Clients. Copilot's historical tax is a first-call input delta. Grok 1.0.92 reaches MCP tools through `search_tool` / `use_tool`, so a shorter schema changes the bytes those tools return when the model asks, which is not the same as shrinking every first call by the tiktoken delta.
- `tests/mcp.rs` checks the four names and `readOnlyHint`. It does not snapshot descriptions. A shorter string does not fail it. A renamed tool, a dropped annotation, or a new required field would.
- The gate in `evals/definition_cost.py` and the task file. Suite 1–4 are unchanged.

Engineering Lead's track adds constants or parameters and one `context` tool registration, and does not edit `READ_DESC`, `SEARCH_DESC`, `MAP_DESC`, or `INSTRUCTIONS`. This measurement does not special-case the four names. Phase 2 does not edit those new constants, parameters, or the `context` registration. If a slim would touch the same lines, stop. `TRACE_DESC`, `TraceArgs`, the direction enum, and `trace` output stay as they are unless a later note says otherwise. `SearchArgs.output` stays `matches | symbols | files | scopes`. No `auto` mode. No change to `src/search.rs`, path policy, or `--restricted` / `--unrestricted`.

## Two shapes

**Shape A. Shorter strings in place.** Same tools, same property names, same arity, same enums. The literals in `READ_DESC`, `SEARCH_DESC`, `MAP_DESC`, `TRACE_DESC`, `INSTRUCTIONS`, and the `description` fields inside the `JsonSchema` impls get edited where they stand. One file holds them. One fact can still have two owners: target syntax in `READ_DESC` and again on `targets`; output modes in `SEARCH_DESC` and again on `output`; `base_dir` copied four times. Callers still receive every parameter. This is the smaller change to the types. It does not give each fact one owner.

**Shape B. One owner per fact, same names and arity.** The tool description owns when to call the tool. The field description owns how to fill that field. A fact that is said in both places keeps the copy on the field the model has to fill. The `base_dir` sentence has one source string, used by each schema (the payload will still repeat it if the SDK inlines it; the source does not). `INSTRUCTIONS` owns only cross-tool policy that is not already on a tool. The dialect URL stays owned by the SDK. No new tool, no renamed property, no new mode. `SKILL.md` is not the owner of a cue `tools/list` must carry: a client that only forwards `tools/list` never loads the skill, so moving a required cue there would make those callers know less than they need.

**Pick B.** Each fact has one owner. Each of the 16 tasks still has one tool. Callers see each parameter's contract once, which is the least they need to form the call. Shape A can leave two sentences for the same fact, so a later edit updates one and not the other.

This note does not propose replacement wording.

## The claim Phase 2 would have to support

On main at `7478ebc4e93ea1faa11f18148669bb4a175b06d2`, `tools/list` result went from **1578** to M o200k_base tokens (cl100k_base from **1551** to M2), 5 launches, baseline range 0, same tokenizer and same `disallowed_special=()`. Canonical argument-validity stayed **16/16**, and the three negative calls stayed rejected. On the Grok exclusive gate (280 trials per tool), tool-selection and task success did not fall by the detectable drop computed from the observed baseline rate (10 points at the planning rate 0.90), and argument-validity did not fall by its detectable drop (5 points at 0.98). First-call median (available − baseline, 200 pairs) did not rise by more than the IQR of those pairs. Every Chromium query's median wall, peak RSS, and answer-token count stayed inside this run's min–max.

What would change that decision:

- The five `tools/list` payloads are not byte-identical. Stop. The count is not a baseline.
- The exclusive tools list still contains file tools on enough trials that the scored set is no longer the planned n. Selection is inconclusive.
- The probe SD is above about 1010 tokens. A 200-token shift is underpowered. Report the shift n can detect. Do not call a smaller shift a win or a regression.
- The IQR of the paired first-call differences is larger than the absolute median difference. Inconclusive.
- A Chromium query's later median leaves this run's min–max, or its error count changes. Do not explain that as a definition-token effect. Re-measure before citing it.

Noise floor for the number already in hand: **0 tokens** on `tools/list`. Argument validity is one deterministic run. Chromium noise is the per-query min–max in the artifact. Agent noise is whatever the Grok run records; until then the agent half is pending.
