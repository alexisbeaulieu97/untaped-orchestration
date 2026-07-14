# PR #3 Task 2 report: bounded orchestration file reads

## Scope and outcome

Task 2 is complete on base `41c7d53116d00845393903c2ceab9d627e708cf5`.
All caller-supplied manifest, import-record, replacement-front-matter, and body
files now cross one application `ExternalFileReader` port and are consumed as
one immutable bounded byte snapshot. Canonical store loading and mutation
projection also use the bounded reader, with 64 KiB front-matter/admin limits,
a 1 MiB body limit, and one aggregate item-file limit that includes both exact
delimiter lines.

The filesystem adapter opens each path component without following symlinks,
requires the final descriptor to be regular, reads at most `limit + 1`, and
checks component identity after the read. Mutation projection retains the full
`StoreEntry` map but content-reads only canonical administrative and item files;
views, artifacts, temporary files, editor files, and unexpected entries remain
metadata-only.

## RED evidence and root causes

- New bounded-reader tests initially failed `9 failed` because there was no
  shared limit module, injected reader port, descriptor-safe adapter, or
  substitution/growth coverage.
- Repository tests initially failed `6 failed, 40 passed`: canonical reads were
  unbounded, and projection read every regular store entry before filtering.
- CLI routing tests initially failed `4 failed, 5 passed` because ordinary
  create/update/supersede body files still used the old direct helper.
- An invalid UTF-8 manifest initially returned ORC002 instead of the required
  ORC001. The byte reader now stays encoding-neutral, and the manifest caller
  performs the semantic UTF-8 validation without leaking file content.
- Repair initially rejected a corrupt current item before it could consume
  explicit replacements because UTF-8 validation had been placed in the byte
  reader. Moving encoding validation back to codecs/callers restored raw
  recovery while preserving the single-snapshot boundary.
- The first full unit run was `861 passed, 8 failed in 58.90s`. All eight were
  one regression: tolerant administrative inspection received the new ORC003
  for a missing/unsafe scaffold path instead of allowing `check` to emit its
  established primary diagnostic. That path now ignores only read-time ORC003;
  oversized ORC001 canonical diagnostics still propagate.
- The first relevant integration run was `64 passed, 1 failed in 26.74s`.
  Forwarding an exact typed symlink diagnostic lost the contextual word
  `manifest` from the public message. Import now prefixes forwarded diagnostic
  messages while retaining their original code, path, field, and hint.

## Changed interfaces and behavior

- `domain.limits` centrally owns `FRONTMATTER_LIMIT`, `BODY_LIMIT`,
  `DELIMITER_OVERHEAD`, and `ITEM_FILE_LIMIT`; the codec uses the same values.
- Application `ExternalFileReader.read_external(path, *, limit, field)` returns
  immutable bytes and is injected into import and repair services.
- `FilesystemExternalFileReader` implements descriptor-relative component-wise
  no-follow reads, regular-descriptor checks, `limit + 1` detection, and
  post-read path identity verification. FIFO/device inputs fail without
  blocking.
- Platforms without descriptor-relative `O_NOFOLLOW` use a documented
  cooperative fallback: pre-open component identities, descriptor identity,
  and post-read component identities must agree. Detected substitutions are
  rejected, but writers must cooperate by not replacing components during the
  read. The fallback is force-testable and has deterministic race hooks; no
  platform skip was needed on macOS.
- Import retains manifest-relative containment checks and additionally routes
  the manifest, each record front matter, and each record body through the
  injected bounded reader exactly once.
- Front-matter repair routes replacement metadata and an optional replacement
  body through the injected reader exactly once. Repair transaction behavior is
  unchanged.
- Ordinary task/decision create, update, and supersede body files all use the
  same bounded reader before service access, preserving Task 1 typed UTF-8 and
  body-limit diagnostics.
- Canonical store, registry, instructions, and item loads use precise bounded
  reads. Oversized canonical paths report ORC001 against their canonical
  relative path.
- Mutation projection reads content only for canonical admin/item paths while
  retaining all entry metadata needed for safety validation.
- Recovery documentation and the packaged skill state the external-file
  regular/nonsymlink and size contracts, including the cooperative fallback.

## Verification

- Focused bounded-reader suite:
  `uv --cache-dir .uv-cache run pytest tests/unit/infrastructure/test_external_files.py -q --no-cov`
  -> `9 passed in 0.16s`.
- Broad affected unit slice covering filesystem, codec, repository, import,
  repair, mutation finalization, and CLI translation/diagnostics:
  `uv --cache-dir .uv-cache run pytest tests/unit/infrastructure/test_external_files.py tests/unit/infrastructure/test_filesystem.py tests/unit/infrastructure/test_codec.py tests/unit/infrastructure/test_repository.py tests/unit/application/test_import.py tests/unit/application/test_repair.py tests/unit/application/test_mutation_finalization.py tests/unit/cli/test_review_fixes.py tests/unit/cli/test_request_translation.py tests/unit/cli/test_diagnostic_contracts.py -q --no-cov`
  -> `249 passed in 9.96s`.
- Administrative-check regression suite:
  `uv --cache-dir .uv-cache run pytest tests/unit/application/test_check_fmt_render.py -q --no-cov`
  -> `41 passed in 4.69s`.
- Full unit suite:
  `uv --cache-dir .uv-cache run pytest tests/unit -q --no-cov`
  -> `869 passed in 66.69s`.
- Affected import unit plus relevant CLI, federation, import, mutation, local
  store, raw-recovery, and recursive-maintenance integration suites:
  `uv --cache-dir .uv-cache run pytest tests/unit/application/test_import.py tests/integration/test_import_fault_states.py tests/integration/test_cli_contract.py tests/integration/test_federation.py tests/integration/test_item_mutations.py tests/integration/test_local_store.py tests/integration/test_raw_recovery.py tests/integration/test_recursive_maintenance.py -q --no-cov`
  -> `90 passed, 1 warning in 15.12s`. The warning is Cyclopts' existing pytest
  no-token invocation warning in the version test.
- `uv --cache-dir .uv-cache run ruff check .` -> `All checks passed!`.
- `uv --cache-dir .uv-cache run ruff format --check .` ->
  `117 files already formatted`.
- `uv --cache-dir .uv-cache run mypy` ->
  `Success: no issues found in 60 source files`.
- `git diff --check` -> clean.

## Deferred work

Task 3's true-local path policy and Task 4's repair transaction redesign are
unchanged and intentionally deferred. This task adds no compatibility layer,
release action, merge, push, PR mutation, or generated cross-repository state.

## Follow-up review fix: post-read bounds and exact item projection

### Scope and outcome

The Important follow-up findings against Task 2 were verified and fixed on the
exact base `0666406a75207bbaa325c8a64aaf552856f9f4bf`. The follow-up remains
strictly inside Task 2 and is committed separately as
`fix: close bounded-read review gaps`.

- Both the descriptor-relative no-follow path and cooperative fallback now
  perform a fresh `fstat` after the bounded read. The opened descriptor must
  still be regular and retain the same device/inode/type identity; a post-read
  size above the caller's limit is ORC001, while type or identity drift is
  ORC003. A deterministic `after-read` seam proves growth after EOF rather
  than relying on timing.
- Repository mutation projection now treats only exact `.md` files directly
  inside `tasks/`, `decisions/`, or `archive/tasks/` as item content. Non-Markdown
  regular files in every item root remain in the complete `StoreEntry` map for
  shape validation but are never content-read or parsed.
- Import record identity extraction now separates `UnicodeDecodeError` from
  TOML/schema failures. Invalid UTF-8 front matter emits a leak-free ORC001
  diagnostic carrying the external record path and `frontmatter` field;
  downstream body codec diagnostics and other typed failures remain intact.

No command syntax, store format, public compatibility layer, true-local policy,
or repair transaction behavior changed. Tasks 3 and 4 remain untouched.

### TDD evidence

The regression tests were added before production changes. The focused RED was:

```text
$ uv --cache-dir .uv-cache run pytest tests/unit/infrastructure/test_external_files.py tests/unit/infrastructure/test_repository.py::test_projection_reads_only_bounded_canonical_content_but_keeps_all_entries tests/unit/application/test_import.py::test_import_record_frontmatter_invalid_utf8_is_orc001_without_content_leak tests/unit/application/test_import.py::test_import_record_body_invalid_utf8_preserves_codec_diagnostic_without_content_leak -q --no-cov
8 failed, 10 passed in 2.49s
```

The eight failures were exactly two primary/fallback growth-after-EOF cases,
four primary/fallback post-read descriptor identity/type cases, one non-`.md`
projection case spanning all three item roots, and the front-matter ORC002 to
ORC001 classification gap. The invalid UTF-8 body case passed in RED because
the existing codec-owned ORC001 diagnostic was already correct and needed to
be preserved.

The same focused slice passed after the minimal implementation:

```text
18 passed in 1.57s
```

### Verification

- Affected unit slice from the original Task 2 report, with the new
  regressions: `257 passed in 11.08s`.
- Full unit suite: `877 passed in 70.37s`.
- Full integration suite: `97 passed, 1 warning in 53.07s`; the warning is the
  pre-existing Cyclopts no-token warning in the version contract test.
- Coverage-enabled full suite: `975 passed, 1 warning in 135.48s`, with 92.20%
  total coverage against the 80% floor.
- `uv --cache-dir .uv-cache run ruff check .` — `All checks passed!`.
- `uv --cache-dir .uv-cache run ruff format --check .` — `117 files already
  formatted` after formatting the new import regressions.
- `uv --cache-dir .uv-cache run mypy` — `Success: no issues found in 60 source
  files`.
- `git diff --check` — clean.

### Self-review

- Confirmed the post-read descriptor validation runs before component identity
  verification in both adapter paths and never returns bytes after detected
  descriptor growth, substitution, or type drift.
- Confirmed projection spies cover metadata-only noise inside `tasks/`,
  `decisions/`, and `archive/tasks/`, while existing top-level/view/artifact
  coverage remains intact.
- Confirmed invalid UTF-8 front matter includes only stable diagnostic context,
  never source bytes, and invalid UTF-8 body still forwards the codec's typed
  destination diagnostic unchanged.
