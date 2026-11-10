# 05 — Relationship kinds and evidence

Status: shipped (default; output is unchanged when no mark applies). Invariant: 6. Borrowed from codebase-memory-mcp (CALLS vs callable
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

## As shipped

Implementation differs from the design above in three ways, all found by comparing 120
sampled `trace` targets on ripgrep and zod against the upstream binary:

- No `OccKind::Value`. A site keeps its kind (`Call`/`Ref`) and carries
  `value: Option<ValueUse>` (`PassedTo(callee)`, `Assigned`, `Returned`) plus `in_closure`.
- Summary line: `N call sites + V passed as value in M functions, C inside closures`, and the
  legends `λ = inside a closure (may run later or never)` /
  `→ = passed, stored or returned, not called here` appear only when a mark is present.
- A first version marked every non-call mention in a value position. On zod that produced
  504 value sites, most of them wrong (`result.error`, `node.number`, `Set<number>`,
  `7 as never`, getters). The shipped rules (`names_callable`, `value_use`, `bound_locally`):

| Mention | Counts as a value use |
|---|---|
| bare `name`, target is a free function | yes, unless a parameter or an earlier local binds the name |
| bare `name`, target is a method | only where `this` is implicit and the enclosing type is the owner |
| `m::name`, `Type::name`, `this::name` | yes (a path is never a field) |
| `x.name` | only when the receiver is known to be the owner (`this`/`self` inside it, the owner's name, a Go package) and only in Python, JS/TS, Go, C#, Swift, Lua |
| `x.name` in Rust, Java, C, C++, Kotlin, Objective-C | no (fields and methods are separate namespaces, or the syntax is not a method value) |
| anything in Ruby, Scala, Bash, PHP | no (a bare name is a call or a constant there) |
| a type position (`Set<name>`, `x as name`, `T = name`) | no |
| the target is a getter/setter or `@property` | no |

Known misses, accepted for precision: namespace-qualified functions passed as values in
JS/TS and Python (`arr.map(z.string)`, `Thread(target=worker.run)`), object shorthand
(`{ handler }`), re-exports. They remain visible under `direction=refs`.

The `passed to` label is the callee as written, at most 40 bytes; a method chain keeps its
last link (`xs.into_iter().map(f)` → `passed to ….map`).

Result on the sample (unlimited budget): 0 sites lost, 77 of 120 answers byte-identical,
5 value sites added (all five checked by hand: `.some(containsRef)` twice,
`.some(fastPathAcceptsAbsence)`, `.map(IgnoreMatch::overrides)`,
`const booleanType = ZodBoolean.create`), 395 closure marks. See [eval.md](eval.md).
