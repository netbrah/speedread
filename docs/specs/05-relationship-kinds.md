# 05 — Relationship kinds and evidence

Status: specified. Invariant: 6. Borrowed from codebase-memory-mcp (CALLS vs callable
reference vs unresolved usage) and CodeGraph (annotated, inferred relationships).
Covers upstream ROADMAP "Calls inside closures".

## Behavior

`trace` sites carry a kind and, where applicable, a mark:

| Kind | Meaning | Rendered |
|---|---|---|
| call | name is the callee of a call expression | (as today) |
| value | a callable's name used as a value: call argument, assignment/initializer right-hand side, returned | `→ passed to router.post` / `→ assigned` |
| ref | any other mention | (as today, `refs` only) |
| import | import/use | (as today) |

Marks: `λ` when the site is inside a closure/lambda/anonymous function nested in the
enclosing named symbol (the group is still the named symbol; the mark says the call may run
later or never). `?` unresolved receiver (existing).

- `callers` includes `value` sites, listed after calls, because "who can cause this to run"
  includes registrations. They are counted separately in the summary line:
  `3 calls, 2 passed as value, 1 in closure`.
- Nothing claims runtime invocation for `value` sites.

## Design

- `OccKind::Value` in `trace::classify`: the identifier is not a callee, the target
  definition is callable, and the nearest non-trivial ancestor is an argument list, an
  assignment/initializer value, a field/property value, or a return.
- `Occ.in_closure`: while walking ancestors up to the enclosing outline symbol, any of the
  closure node kinds (`closure_expression`, `arrow_function`, `function_expression`,
  `lambda`, `lambda_expression`, `func_literal`, `anonymous_function`, `block` for Ruby
  `do`/`{}` blocks) sets it.
- For `value` sites in a call argument list, the callee text of that call (≤ 40 bytes) is the
  "passed to" label.

## Tests

Fixtures for Rust, TypeScript, Go, JavaScript:

- `items.iter().for_each(|i| target(i))` → call with `λ`.
- `router.post("/login", authenticate, target)` → value, `→ passed to router.post`.
- `let f = target;` / `const f = target` → value, `→ assigned`.
- Direct call unchanged. Existing trace tests unchanged.

## Ship gate metric

Existing trace outputs for the eval query set change only by added marks/sites; no site lost.
