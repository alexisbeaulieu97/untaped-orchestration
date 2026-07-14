# PR 3 Task 3 Report

## Outcome

Implemented the Task 3 local-scope contract on base
`b1d20bbd621a211c97e96ebd40ab4af843a521b2`.

- `CliContext.resolve()` now performs selected-store discovery only.
- Mutation consumers receive lazy recursive and selected-local factories.
- Each mutation resolves exactly one chosen scope immediately before execution.
- Recursive factories perform one optimistic header resolution and retain invalid
  participants in the exact lock set.
- Exact-set validation now compares locks with federation participant anchors,
  including unexposed invalid participants.
- Local fmt check/write and render use selected-store-only federation acquisition
  and selected-local validation.
- Curation no longer preloads a second scope before mutation.
- Import now follows the shared lazy mutation path.
- Task 4 front-matter repair transaction behavior was not changed.

## Scope matrix enforced

Recursive mutation scope remains the default for task and structural operations:
task/decision creation, task updates and transitions, generic links, task evidence,
task curation, decision supersede/retire, and import.

Selected-local mutation scope is used for decision clarification, decision evidence,
and decision curation. Explicit local fmt and render operations resolve and lock only
the selected store. Default reads and default fmt remain recursive.

## TDD evidence

Initial RED:

- Context/factory slice: 2 failed. Context eagerly loaded the federation and the
  mutation helper treated a factory as a concrete scope.
- Participant/maintenance slice: 5 failed, 4 passed. Exact lock validation compared
  only exposed stores; local fmt check/write and render routed recursively.
- Relevant integrations: 1 failed, 68 passed. True-local fmt exposed that its
  validation still treated unresolved remote navigation as ORC004.

Focused GREEN:

- Context/factory slice: 2 passed.
- Participant/maintenance slice: 9 passed.
- Selected-local validation controls: 31 passed.
- Runtime consumer factory-choice matrix: 12 passed.
- Import factory coverage: 28 passed.
- Relevant integration set: 69 passed, 1 pre-existing Cyclopts warning.

## Complete verification

- Ruff format: 120 files already formatted.
- Ruff lint: all checks passed.
- mypy: success, 60 source files.
- Full unit suite: 907 passed in 64.47s.
- Full integration suite: 97 passed, 1 pre-existing Cyclopts warning in 40.74s.
- Build: source distribution and wheel built successfully.
- Full pytest gate: 1005 passed, 1 pre-existing Cyclopts warning in 150.50s;
  coverage 92.22% (80% required).
- pre-commit: all hooks passed.
- `git diff --check`: passed.

## Documentation assessment

No public command syntax or output contract changed, so README, packaged skill,
version, changelog, and release metadata updates are not required for Task 3. The
scope contract is covered by caller-visible and adversarial tests in this change.
