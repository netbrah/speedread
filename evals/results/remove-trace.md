# Removing `trace`: search plus one batched read

`trace` is gone. The tools are `map`, `search` and `read`. These numbers are the check that the questions `trace` answered are still in the tool output, plus the size of the binary and of the tool definitions.

Tokens are speedread's content-aware estimate (`src/tokens.rs`), the same weights the server uses for budgets. They are tool-output tokens, not a model's billed input. The agent suites were not re-run.

Raw scores: [`remove-trace.json`](remove-trace.json). Command:

```bash
python3 evals/trace_replaced.py target/release/speedread /path/to/speedread-with-trace /path/to/bench
```

`bench` holds `gin/`, `flask/` and `ripgrep/`. The with-trace binary is a release build of `a45eb15` (main, which still has `trace`). The without-trace binary is a release build of this change. Both use the release profile (fat LTO, one codegen unit).

## Binary and definitions

| | with `trace` | without | saved on every model call |
|---|---:|---:|---:|
| Release binary | 42,583,408 bytes | 42,234,928 bytes | 348,480 bytes (0.8%) |
| `tools/list` JSON | 1,949.2 tokens | 1,550.5 tokens | 398.7 |
| Server instructions | 156.4 tokens | 144.6 tokens | 11.8 |
| Definitions, together | 2,105.6 | 1,695.1 | 410.5 |

The `trace` tool's own schema is 398.0 tokens, which is the tools-list saving. `READ_DESC`, `SEARCH_DESC` and `MAP_DESC` are unchanged. The instructions change is the deletion of `` `trace` follows callers, callees, references and implementations; ``.

That 410 tokens is paid on every model call, including calls that never ask a relationship question. In the bug-fix suite, `trace` was called in 0 of 32 trials.

## Fixture

`evals/fixtures/trace_oracle`, nine questions (the `value` case is inside the caller and reference cases). Pass means every labeled source snippet is present. It does not score whether an arrow points at the right definition: the call text `other::helper(2)` is on the line either way. Reference precision of syntactic trace on a ripgrep sample (0.035) stays in the closed draft that measured it.

| | pass | calls / question | mean tokens |
|---|---:|---:|---:|
| `trace` | 9/9 | 1 | 149 |
| `search` | 7/9 | 1 | 172 |
| `search` + one `read` of the enclosing symbols | 9/9 | 2 | 409 |
| one `read` of `src/lib.rs` and `src/other.rs` | 9/9 | 1 | 855 |

The two searches that miss are the callee questions: a search for the function's name shows the definition line, and the call is in the body. One `read` of that symbol (the second call) contains the call text. On this crate the whole graph also fits in a single read of the two files, 855 tokens.

## Relationship questions

Same graders as [`relationship_tasks.json`](../relationship_tasks.json), applied to the tool output. The suite never recorded repository commits. These are the default-branch tips at the end of 2026-09-24, the day the suite ran, and flask's definition lines here are the ones the graders name (868, 969, 1024, 1369).

| repo | commit |
|---|---|
| gin | `3b08cd7235bd5ad2f055aa9e38135f111f6c5926` |
| flask | `d73fa1cdcbd8b1465c151db8924ba58b1dd14e35` |
| ripgrep | `3fce3b5bb0236da2df6d99672afb8a719642eca7` |

| question | `trace` | `search` (or the body `read`) | then one batched `read` |
|---|---|---|---|
| gin render implementations | 1 call, 359 tokens, 10/10 | 1 search, 1,441 tokens, 10/10 | +1 read of 18 symbols, 4,786 tokens total, 10/10 |
| ripgrep `Sink` implementations | 1 call, 435 tokens, 6/6 | 1 search, 698 tokens, 6/6 | +1 read of 9 symbols, 7,020 tokens total, 6/6 |
| flask `full_dispatch_request` callees | 1 call, 322 tokens, 7/7 | 1 read of the method, 404 tokens, 3/7 | +1 read of the 4 callees, 2,189 tokens total, 7/7 |
| gin `AbortWithStatus` two hops | 1 call, 1,376 tokens, 3/3 | 2 searches, 1,057 tokens, 3/3 | +1 read of 10 symbols, 3,703 tokens total, 3/3 |

Procedures, all at the default budget:

- **Render.** `search` in `render/` for `_ Render` or `func (r Type) Render(`, tests excluded. The nine required type names are in that output. The grader `\b18\b` also matches, in this output and in the `trace` output, because a line number and a range (`[18-26]`, `[18-21]`) contain 18. Neither tool states the count. `trace`'s summary line says 19, and that list includes `TestRender`.
- **Sink.** `search` for `impl(<[^>]*>)? Sink for`. The six type names are in the hit lines.
- **Callees.** `read src/flask/app.py#full_dispatch_request` names `preprocess_request`, `dispatch_request`, `handle_user_exception` and `finalize_request`. Their definition lines are not in that body. One `read` of those four symbols carries 969, 1369, 868 and 1024.
- **Two hops.** `search -w` for `AbortWithStatus` in non-test Go, then one `search -w` for the enclosing function names (`AbortWithError`, `BasicAuthForRealm`, `BasicAuthForProxy`, `defaultHandleRecovery`). `BindUri`, `MustBindWith` and `Negotiate` are the enclosing symbols of the second search. The third call reads those symbols.

On the two implementation questions the search is enough for the name graders, and it costs more tokens than `trace` because it returns the matching lines under their symbols rather than a one-line list. The batched read is the check of those symbols, and it is the expensive call. On the callee question the batched read is what supplies the definition lines. On the two-hop question the two searches are smaller than the depth-2 trace (1,057 vs 1,376 tokens); reading the ten symbols back makes it larger.

## What the recorded agent run still says

Suite 3b (claude-sonnet-5, 8 trials per arm, 2026-09-24) is unchanged and stays the record of that experiment: pass@1 100% in both arms, mean input tokens 44,636 with speedread against 104,797 baseline, mean tool calls 1.25 against 4.38. On the two-hop task the agent called `trace` at depth 2 and then checked one function: 2 calls, about 61k input tokens, against 7–14 calls and 186k–277k with the built-in tools. Those session totals include the prompt and the conversation. This page measures tool output only.
