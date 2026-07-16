# PR 3 Task 4 Report

## Outcome

Implemented Task 4 on base `fe1eeb5f12630c3e9f7abd4dba3ce3d80e1f5f30`.

- `MutationExecutor.execute()` now accepts distinct current-state and
  projected-state validators while preserving the existing one-validator
  default for every prior caller.
- Front-matter repair captures bounded replacement front matter and optional
  body exactly once before scope resolution or lock acquisition.
- `RepairService` now owns only input capture, exact raw/revision guard,
  one-replacement planning, repair-specific validators, and result mapping.
- The lazy recursive mutation scope and `MutationExecutor` own participant
  locks, projection, shape checks, canonical write, durable reread, projected
  validation, views, and receipt creation.
- Duplicate repair remains delegated unchanged to `TaskService`.
- No Task 5 behavior was started.

## Validator and transaction ordering

The default-compatible validator is selected first. An explicit current
validator, when supplied, runs once after store-shape inspection and recursive
load but before lock-set validation, guard, or build. The projected validator
runs on the in-memory projected federation before canonical writes and again on
the durable reread federation before view finalization.

Repair's current validator removes only load diagnostics belonging to the exact
selected repair path, allowing the known broken source while retaining every
other selected/available-participant error. Its guard rereads the raw target
under the participant locks and verifies the exact content-derived revision.
Its projected validator calls `validate_snapshot` on the actual resolved
federation with `require_children=false`.

## Federation and failure behavior

- A valid repaired cross-store relationship resolves and validates its target.
- An unrelated missing child remains an ORC005 warning and does not block the
  targeted repair.
- An available child missing the repaired relationship target is rejected as
  ORC004 before any canonical write.
- Participant-set drift and required-lock timeout produce exact ORC007 and
  leave the source unchanged.
- Replacement inputs are observed exactly once before locks.
- The builder always emits exactly one canonical replacement.

## Receipt truth table

| Outcome | `applied` | `canonical_applied` | `views_current` | Durable state |
|---|---:|---:|---:|---|
| Dry run | false | false | false | Canonical and views untouched |
| First canonical writer interruption/failure, including a post-write exception without acknowledgement | false | false | false | Conservative/unknown canonical outcome; no view write attempted |
| Later canonical writer interruption after one or more acknowledged operations | true | true | false | Receipt preserves the exact acknowledged changed paths; later intended paths remain unapplied/unknown |
| View failure after durable canonical success | true | true | false | Canonical bytes durable; `check` reports stale views and `render --write` recovers |
| Success | true | true | true | Canonical reread equals projection and views are current |

For the injected post-replacement interruption, the returned failure receipt is
deliberately conservative: the executor did not receive successful writer
acknowledgement, so it reports false canonical/view flags and no changed path,
even though fault inspection can observe the replacement already on disk. That
on-disk state is valid and recoverable through `check`/`render`; no journal or
third transaction was added.

For a multi-file mutation, each path is acknowledged only after its writer call
returns. A later writer failure therefore preserves `changed_paths` for every
earlier acknowledged operation and reports `applied=true` and
`canonical_applied=true`. A failure on the first operation, or an exception
raised after a replacement but before its writer returns, remains false/empty.
All canonical-write failures keep `views_current=false`.

The CLI serializes the bounded mutation receipt as failure data in JSON and
table output. `MutationWriteError` retains the generic leak-free ORC002
diagnostic and exit 5. Typed `DiagnosticError` writer failures keep the same
exception and exact public diagnostic/mapped exit code while carrying the same
receipt truth as untyped writer failures. The CLI accepts failure data only
from those two failure categories and only when the attached value is an exact
`MutationReceipt`, so arbitrary exception data is not emitted.

## TDD evidence

- Separate-validator RED: 3 failed in 2.11s because the executor rejected the
  new validator arguments. GREEN compatibility/order/failure slice: 4 passed in
  2.13s.
- Pre-lock input RED: 1 failed in 1.18s because the reader observed an active
  lock. GREEN: 1 passed in 1.12s.
- Lazy repair transaction RED: 1 failed in 0.91s because `RepairService` still
  required writer/locks/views. Focused transaction GREEN: 4 passed in 2.06s;
  full repair file initially 10 passed in 1.99s.
- Cross-federation matrix: 3 passed in 2.09s.
- Participant drift/timeout matrix: 2 passed in 2.01s.
- Dry-run/view/canonical fault characterization: 4 passed in 2.53s.
- Canonical failure-receipt RED: 2 failed in 4.50s because raw OSError carried
  no receipt. GREEN repair plus MutationExecutor slice: 23 passed in 6.78s.
- Review-fix RED: 4 expected behavioral failures showed discarded earlier
  acknowledgements, wrapped typed writer diagnostics, and missing JSON/table
  receipt data. One additional test-fixture assertion was corrected because
  optional null diagnostic fields are intentionally omitted by the encoder.
  Focused GREEN: 5 passed in 0.38s; affected repair/mutation/CLI GREEN: 107
  passed in 1.38s.
- Final re-review RED: 3 expected failures in 0.74s showed that first-call typed
  writer failures lacked a conservative receipt and second-call typed failures
  lost the acknowledged path in JSON/table data. Focused GREEN: 3 passed in
  0.31s; affected repair/mutation/CLI GREEN: 109 passed in 1.38s.

## Verification

- Affected repair/mutation/lifecycle/import/registry suite after the final
  executor change: 139 passed in 27.81s.
- Broad affected repair/mutation/validation/federation/view/CLI/recovery suite:
  218 passed, 1 pre-existing Cyclopts warning in 15.90s.
- Full unit after the final executor change: 919 passed in 66.72s.
- Full integration: 97 passed, 1 pre-existing Cyclopts warning in 39.83s.
- Coverage-enabled full gate before the final conservative failure-receipt
  wrapper: 1017 passed, 1 pre-existing warning in 150.30s; 92.19% coverage
  against the 80% requirement. The final wrapper was then covered by the
  focused, affected, and full-unit reruns above.
- Ruff: all checks passed; format: 120 files already formatted.
- mypy: success in 60 source files.
- Build produced the source distribution and wheel.
- pre-commit passed all hooks.
- `git diff --check` passed.
- Review-fix full gate: 1022 passed, 1 pre-existing Cyclopts warning in 21.36s;
  92.21% coverage. Ruff and format passed (120 files), mypy succeeded for 60
  source files, build produced the sdist and wheel, and pre-commit passed all
  hooks.
- Review-fix full unit suite: 924 passed in 8.72s.
- Final re-review full unit suite: 926 passed in 8.64s. Ruff and format passed
  (120 files), mypy succeeded for 60 source files, and `git diff --check`
  passed.

## Documentation assessment

Command syntax and the documented recovery workflow are unchanged. Existing
recovery documentation and the packaged skill already state bounded repair
inputs, exact revision guards, canonical-success/view-failure truth, and
`check`/`render` recovery, so no README, docs, skill, version, lockfile, or
release metadata update is required for this internal transaction ownership
change.
