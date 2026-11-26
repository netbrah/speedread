# Run the description baseline on an on-prem model

øp runs this on his own machine. Do not point it at a paid first-party key.
The default, with no base URL, is still Grok CLI's own catalog. Token counts
and `cost_usd` from an on-prem model are not comparable with grok-4.7.

Grok CLI 1.0.92 can target a custom OpenAI-compatible endpoint. It is not a
CLI flag. A trial `GROK_HOME/config.toml` gets one `[model."<id>"]` block per
model: `base_url`, `model`, `env_key` (the variable's **name**), and
`api_backend = "chat_completions"`. That is the documented shape in
[Custom models](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/11-custom-models.md)
and the [settings reference](https://docs.x.ai/build/settings/reference).
The 1.0.71 changelog already describes custom models with a custom `base_url`
([changelog](https://x.ai/changelog/build), 26 Oct 2026); 1.0.92 is later
(19 Nov 2026), so that path is in the CLI this harness targets. The harness
never writes the key into the file.

## Prerequisites

- Grok CLI 1.0.92, Rust, Python 3 with `venv`, Go.
- `cargo build --profile fast` so `target/fast/speedread` exists.
- The bench, including Chromium at `3a7520d2d79d6569d3a11f968d32b20665fa4db3`:

```bash
python3 evals/fetch_corpora.py --dest /path/to/bench
python3 evals/fetch_corpora.py --dest /path/to/bench --check
```

`--check` must pass. `RenderFrameHostImpl::DidCommitNavigation` is line 18236
in that tree. The checkout that was measured is 476,800 files and 5.7G, not
the 474,447-file tree in `docs/specs/eval.md`.

## Env and flags

Flag wins over env, env wins over the Grok defaults.

| Flag | Env | Default |
|---|---|---|
| `--base-url` | `SPEEDREAD_BASE_URL` | unset (Grok's own API) |
| `--models` | `SPEEDREAD_MODELS` | `grok-4.7,grok-4.7-build-fast` |
| `--api-key-env` | `SPEEDREAD_API_KEY_ENV` | unset |

`--api-key-env` is the **name** of the variable. Put the secret only in that
variable. A custom base URL without a name is an error.

```bash
export SPEEDREAD_BASE_URL=https://gateway.example/v1
export SPEEDREAD_MODELS=onprem-a,onprem-b
export SPEEDREAD_API_KEY_ENV=ONPREM_API_KEY
export ONPREM_API_KEY=...   # never echo this
```

The base URL should include `/v1` when the gateway serves `/v1/models` and
`/v1/chat/completions`. The harness calls `{base-url}/models`.

## Preflight

Does not start a trial and does not print the key.

```bash
python3 evals/run_baseline.py --preflight
```

## One task

Needs the Grok binary. A server that only implements `/models` cannot finish
this: Grok's headless session speaks streaming JSON and calls tools.

```bash
python3 evals/grok_eval.py \
  --bench /path/to/bench \
  --tasks evals/agent_tasks.json \
  --only flask-shell-context \
  --conditions dropin --trials 1 \
  --speedread "$PWD/target/fast/speedread" \
  --out /tmp/speedread-smoke
```

With the exports above, this process also reads `SPEEDREAD_BASE_URL`,
`SPEEDREAD_MODELS`, and `SPEEDREAD_API_KEY_ENV`.

## Full run

`print_plan` reports 37 decision tasks, 185 trials per arm per model,
740 decision trials, 74 exclusive, 128 coding, **942** total, and a planning
spend of about **$742**. That spend is the grok price sketch, not an on-prem
invoice. The plan does not print a wall-clock time.

Go must be on `PATH`. The command creates `<bench>/venvs/flask` from
`evals/flask-tests.txt` if it is missing.

```bash
cd /path/to/speedread
setsid nohup python3 evals/run_baseline.py --bench /path/to/bench \
  > evals/results/on-prem-baseline.log 2>&1 < /dev/null &
echo $! > evals/results/on-prem-baseline.pid
```

## Where results land

`evals/results/description-baseline-<YYYYMMDD-HHMMSS>/` with `suite3/`,
`pin/`, `chromium/`, and `coding/`. Each has `trials.jsonl` and
`transcripts/`. The log above names the directory.

```bash
python3 evals/adoption.py check evals/results/description-baseline-<stamp>/suite3
python3 evals/stats.py evals/results/description-baseline-<stamp>/suite3 --adoption --arm dropin
```

Repeat for `pin` and `chromium`. Coding is descriptive and is not in the ship rule.

## Sending the summary back

Tar that results directory (it contains no API key unless a transcript echoed
one) and send the archive plus the `stats.py --adoption` tables. Do not treat
on-prem token or cost columns as grok-4.7 numbers.
