# PR #3 Task 1 report: diagnostic CLI contracts

## Scope and outcome

Task 1 is complete on base `93a07f66494e296db567b962fa7b8e50d2d7fc23`.
Typed diagnostic tuples are now the only expected-runtime marker used by the
CLI. ORC007 errors exit 4, ORC005 errors exit 3, and other typed expected
failures exit 1, with ORC007 taking precedence. Exceptions without a valid
diagnostic tuple exit 5, use a redacted structured diagnostic, and show the
original traceback only with `--debug`.

The change also gives failed `brief` JSON/table envelopes the 32768-byte
fallback ceiling, rejects duplicate decision predecessor IDs before context
resolution in guarded and force-current modes, and preserves ORC001..ORC009
diagnostics across the caller-reachable codec, path/identity, creation,
relation, federation, lifecycle, revision/lock, view, and policy families.

## RED evidence and root causes

- Initial required suite:
  `uv --cache-dir .uv-cache run pytest tests/unit/cli/test_diagnostic_contracts.py -q --no-cov`
  -> `8 failed, 13 passed`.
- The failures traced to four sources:
  1. failed `brief` envelopes were sent through successful-result
     `max_total_bytes` validation;
  2. `_exit_code` and `_error_diagnostics` guessed from class names and blanket
     `ValueError`, leaking arbitrary exception messages;
  3. federation, view, bootstrap, maintenance, and query resolution exceptions
     lacked typed diagnostics;
  4. duplicate predecessor detection ran only in the guarded branch.
- Added caller-family assertions failed `5 failed, 2 passed` before typing the
  bootstrap, registry, identity, and maintenance families.
- Import/repair exact-diagnostic constructor assertions failed `2 failed`
  before those recovery exceptions accepted diagnostic tuples.
- Creation/query identity assertions failed `2 failed, 7 passed` before
  `CreateConflict` and query resolution were classified ORC003.
- The broad regression run exposed old tests that still treated arbitrary
  `ValueError` as expected and recovery wrappers that erased `CodecError`.
  Tests were updated to the approved contract, while wrappers now retain the
  exact diagnostic tuple.

## Changed interfaces and behavior

- `DiagnosticError` owns an exact nonempty `tuple[Diagnostic, ...]`;
  `expected_diagnostic` creates one stable expected diagnostic.
- `CodecError.diagnostics` exposes its existing exact diagnostic without
  changing its path, field, or location.
- Expected application/infrastructure exception families now carry stable ORC
  diagnostics. Query/path/creation identity failures are ORC003, relations are
  ORC004, federation is ORC005, lifecycle/curation is ORC006,
  revision/lock/lock-set is ORC007, views are ORC008, and validation-provided
  capability diagnostics remain ORC009.
- `ImportConflict` and `RepairConflict` accept optional exact diagnostics.
  Typed codec/validation failures are rewrapped without recoding; revision and
  reconstructed-state cases receive contextual diagnostics now.
- No class-name or blanket-`ValueError` exit guessing remains.
- Failed empty `brief` results use 32768 bytes; successful briefs still require
  a configured value in 4096..32768.
- Duplicate predecessor IDs are usage exit 2 before `CliContext.resolve` in
  both guarded and `--force-current` requests.
- Output envelope key order, table/Pipe/raw recovery shapes, command syntax,
  ORC meanings, and version are unchanged. Existing docs and the packaged
  skill required no update because they already state the stabilized contract.

## Verification

- Focused final:
  `uv --cache-dir .uv-cache run pytest tests/unit/cli/test_diagnostic_contracts.py -q --no-cov`
  -> `32 passed in 2.96s`.
- Full unit suite:
  `uv --cache-dir .uv-cache run pytest tests/unit -q --no-cov`
  -> `841 passed in 46.93s`.
- Relevant integration suite:
  `uv --cache-dir .uv-cache run pytest tests/integration/test_cli_contract.py tests/integration/test_raw_recovery.py tests/integration/test_federation.py -q --no-cov`
  -> `32 passed, 1 warning in 12.46s`. The warning is Cyclopts' existing
  pytest no-token invocation warning in the version test.
- `uv --cache-dir .uv-cache run ruff check .` -> `All checks passed!`.
- `uv --cache-dir .uv-cache run ruff format --check .` ->
  `114 files already formatted`.
- `uv --cache-dir .uv-cache run mypy` ->
  `Success: no issues found in 58 source files`.
- `git diff --check` -> clean.

## Concerns and deferred Task 2/4 work

Import and repair now preserve any typed diagnostic they receive and have
explicit contextual handling for codec/validation, identity, and revision
paths. Some recovery-specific string-only branches still use the typed ORC002
default. Assigning their final fine-grained syntax/path/view codes belongs to
the planned Task 2 import and Task 4 repair work; there is no remaining blanket
wrapper that destroys an existing typed diagnostic.

Configuration-only constructor guards and service-not-configured assertions
remain ordinary exceptions intentionally: CLI usage is rejected before service
execution, while impossible dependency wiring is an internal exit-5 failure.

## Diagnostic review follow-up

A post-Task-1 review found three remaining caller-reachable gaps. Pydantic
validation still escaped task/decision creation, store initialization, and
registry child path construction; `validated_copy` recoded every model error
as ORC006; and federation resolution caught broad `OSError`/`ValueError`
families while embedding exception text in public diagnostics.

The focused RED suite demonstrated all three issues:
`16 failed, 84 passed in 8.80s`. The production follow-up now converts schema
and field validation to ORC002, registry child paths to ORC003, body bounds and
UTF-8 failures to ORC001, relation validation to ORC004, and lifecycle
validation to ORC006. Body files are validated before context/service access.
Federation catches only explicit missing/invalid discovery markers and raw
`FileNotFoundError`, emits static ORC005/ORC007 messages, preserves unrelated
typed diagnostics, and lets arbitrary failures reach the redacted exit-5 path.

Follow-up verification:

- Focused GREEN: `99 passed in 8.26s`.
- Full unit suite: `847 passed in 43.54s`.
- Relevant integration suite: `32 passed, 1 warning in 4.71s`; the warning is
  the existing Cyclopts pytest no-token invocation warning.
- Ruff check: `All checks passed!`.
- Ruff format: `114 files already formatted`.
- mypy: `Success: no issues found in 58 source files`.

## Precise mutation-validation follow-up

A second review found that fresh decision supersede still constructed its
caller-controlled successor without typed validation handling, and that
`validated_copy` selected an ORC family from requested update keys instead of
the actual Pydantic failure. That made invalid successor title/tags exit 5 and
allowed an unrelated valid lifecycle-key update to recode a title failure as
ORC006.

The focused RED suite was `4 failed, 23 passed in 0.98s`. Fresh supersede now
uses the same item-validation classifier as update mutations. The classifier
uses the first actual validation location and model-level category: ordinary
field/schema failures, including duplicate waiting parties, are ORC002;
relation/link failures are ORC004; and true lifecycle failures remain ORC006.
Fresh supersede schema failures exit 1 and leave the store snapshot unchanged.

Second follow-up verification:

- Focused GREEN: `27 passed in 0.89s`.
- Broader affected unit slice: `68 passed in 0.94s`.
- Full unit suite: `850 passed in 6.16s`.
- Relevant integration suite, including item mutations:
  `47 passed, 1 warning in 1.23s`; the warning is the existing Cyclopts pytest
  no-token invocation warning.
- Ruff check: `All checks passed!`.
- Ruff format: `114 files already formatted`.
- mypy: `Success: no issues found in 58 source files`.
