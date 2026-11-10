# Fork specs: bounded-context upgrades

Working specs for the `netbrah/speedread` fork. Goal: the most capability we can add to
speedread while keeping its operating model — point it at files and it works — and without
becoming a persistent code-graph service.

Branch: `feat/bounded-context-upgrades`. Upstream: `brennengreen/speedread` @ `37abea2`.

## Hard invariants (every spec inherits these)

1. **No required initialization.** No index build, no background watcher, no daemon state
   that must exist before a query can be answered.
2. **Caches are disposable accelerators.** Deleting any cache changes latency, never answers.
   Anything persisted is keyed by content plus parser/extractor version.
3. **No ambient-CWD dependence.** Path resolution, scope and cache identity derive from the
   request's targets or its explicit `base_dir`, not from where the process started
   ([01](01-path-and-scope-contract.md)).
4. **Work is bounded before it starts, not trimmed after.** Discovery, reading, parsing and
   retention each have a limit; hitting one is reported ([02](02-bounded-discovery.md)).
5. **Incomplete is never reported as absent.** A capped or cancelled search says
   "incomplete", not "no matches" / "no references".
6. **Relationships state their evidence.** Syntactic, lexical-binding, import-path or
   heuristic — and what was not established (runtime invocation, type identity).
7. **Scope is never silently widened.** Discovering a repository root does not authorize
   scanning it; explicit sub-directory scope is honored.
8. **Read-only.** No rewriting, no merge tooling.

## Ship gate

A feature merges into the fork's default behavior only when, against the previous commit:

- `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test --all-targets` pass on Linux;
- its own regression tests were seen failing before the change and passing after;
- `scripts/fork-eval.py` shows no regression in latency, peak RSS or output size on the
  reference repos beyond the thresholds in [eval.md](eval.md), and the feature's own metric improves;
- otherwise it stays opt-in (flag/argument) or is dropped, and the result is logged in [eval.md](eval.md).

## Index

| # | Spec | Status | Borrowed from |
|---|---|---|---|
| 00 | [Research and decision record](00-research.md) | living | — |
| 01 | [Path and scope contract (CWD independence)](01-path-and-scope-contract.md) | shipped | codegraph/CBM operational lessons |
| 02 | [Bounded discovery and residency](02-bounded-discovery.md) | shipped | weave (file-local core), gix (traversal controls), sem (failure mode) |
| 03 | [Ancestor context for search hits](03-ancestor-context.md) | shipped, opt-in | grep-ast |
| 04 | [Structural search](04-structural-search.md) | shipped, opt-in | ast-grep |
| 05 | [Relationship kinds and evidence](05-relationship-kinds.md) | shipped | codebase-memory-mcp, CodeGraph |
| 06 | [Backlog: entity correspondence, import-aware lookup, Git input layer](06-backlog.md) | backlog | sem, weave, gix |
| — | [Implementation plan](plan.md) | living | — |
| — | [Evaluation log](eval.md) | living | — |

## Upstream strategy

Carry on the fork branch; upstream in slices that match upstream's contribution rules
(one topic per PR, tests, model-facing text in its own PR with eval numbers —
see `CONTRIBUTING.md`). Candidates and order are tracked in [00-research.md](00-research.md#pr-or-fork).
