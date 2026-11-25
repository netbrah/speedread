# Live baseline: not run yet

This directory will hold the Grok CLI baseline once it is run on a machine
where `grok` 1.0.x is installed and signed in (or `XAI_API_KEY` is set).
No trial in this PR was produced by a model. Do not read the fixture under
`evals/fixtures/grok_streaming/` as a result.

The run is specified in [docs/specs/description-ab.md](../../../docs/specs/description-ab.md).
The tool set is map, search, and read (37 tasks; joint power about 90% under
the pre-registered alternative). It has not been started here because this
environment has no `grok` binary.

```bash
python3 evals/run_baseline.py --bench /path/to/bench
```

That command builds `target/fast/speedread`, fetches every repo in
`evals/corpora.json` (including Chromium at `3a7520d2d79d`), then runs the
pre-registered baseline. Until that command has written `trials.jsonl` files
next to this note, there is no adoption number, no success number, and no
latency or RSS number from this harness.
