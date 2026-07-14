# PR 3 Task 5 Report

## Outcome

Implemented truthful package and documentation acceptance on base
`05e7a19289fe865ef47a33a6a67ac3fa0a6c5f80` without changing version `0.1.0`,
dependency pins, the lockfile, or the release workflow.

- Removed the development-site-packages `.pth` workaround and `--no-deps`
  install from acceptance.
- Default offline acceptance builds a fresh wheel and sdist outside `dist/`
  and reports exactly one explicit isolated-install skip.
- PR CI sets `UNTAPED_ISOLATED_WHEEL_TEST=1`; the enabled test resolves the
  exact wheel's dependencies in a fresh external virtual environment with no
  checkout cwd, `PYTHONPATH`, editable install, or development-site leakage.
- Public/normative contracts now describe the implemented but unreleased v1
  behavior while preserving publication, release, self-adoption, and fleet
  approval gates.

## Archive acceptance

The offline artifact fixture starts with an empty temporary directory outside
the checkout and invokes `uv build --offline --out-dir ... --no-sources`.
Wheel selection is restricted to that directory, so stale `dist/` files cannot
satisfy acceptance.

The wheel audit verifies the exact packaged source set, all five ordered
`Requires-Dist` values, Python/version/name metadata, the complete WHEEL field
set, exact console entry-point bytes, every RECORD path/hash/size, the packaged
skill, `py.typed`, and repository-state exclusions. The sdist audit verifies
the exact top-level/package file set, PKG-INFO dependency metadata, skill,
typing marker, and the same exclusions.

The review follow-up makes the generated metadata contract complete without
pinning backend-generated noise. METADATA and PKG-INFO must contain exactly the
stable Metadata-Version, name, version, summary, author, author-email, license
expression/file, ordered dependencies, Python requirement, and Markdown
content-type fields, with no additional headers; their body must equal README
text. Those expectations are explicit and cross-checked against pyproject.
Every wheel package member and license, and every sdist source plus
LICENSE/README/pyproject support member, is byte-compared with the checkout.

An independent fresh build under `/private/tmp/untaped-task5-final-audit`
produced one wheel and one sdist. ZIP integrity passed, the sdist contained 72
members, the five dependency requirements and WHEEL/entry point matched, and
no `.untaped`, Git, `dist`, virtualenv, cache, `.superpowers`, `.pth`, or
egg-link state appeared in either archive.

## Isolated install

The default local path is offline and skips only the one dependency-resolving
test. With `UNTAPED_ISOLATED_WHEEL_TEST=1`, the test creates a fresh Python
3.14 environment outside the checkout, installs the exact fresh wheel without
`--no-deps`, verifies imported module/sys.path isolation, and runs help,
version, init, check, fmt, and render.

The first enabled local attempt reproduced the sandbox DNS restriction while
fetching `untaped` from PyPI. Re-running the same test with approved network
access passed: 1 passed, 3 deselected in 4.46s. No harness change or dependency
workaround was used.

## Documentation alignment

- Normative status is `implemented; unreleased`; the two stale implementation
  prohibitions were removed while external gates remain explicit.
- CLI/docs/skill cover recursive-by-default and true-local reads, diagnostic
  ownership/redaction, typed and untyped writer failure receipts, acknowledged
  changed paths, guarded recursive repair finalization, and view recovery.
- File/input docs state exact 64 KiB front-matter/manifest and 1 MiB body
  limits plus component-wise no-follow and cooperative-writer fallback.
- README/AGENTS/CHANGELOG/design/plan document the offline archive audit,
  exactly one local skip, and the CI-only dependency-resolving install.
- SECURITY warns that diagnostics are redacted but receipt paths remain
  repository-sensitive.

## TDD evidence

Initial package/docs/CI contracts failed 3 tests and passed 22 in 0.21s:
CI lacked the isolated-install variable, the package harness still wrote the
`.pth` and used `--no-deps`, and the design remained `proposed, docs-only`.

The first archive run exposed uv's generated artifact-directory `.gitignore`;
the audit was corrected to recognize that non-artifact marker. Subsequent
failures exposed live archive facts rather than product defects: source-tree
`__pycache__` must not be part of the expected package set, and the entry-point
file ends with one blank line. The final focused package/docs/CI suite passed
30 tests with exactly 1 skip in 0.24s.

The narrow artifact review RED produced 3 expected failures, 4 passes, and 1
skip in 0.52s: corrupted Summary and README body were accepted, and no
checkout-byte mapping existed. After the exact stable metadata/body and member
byte checks, the focused artifact suite passed 7 tests with exactly 1 skip in
0.13s.

## Verification

- Full unit: 929 passed in 8.07s.
- Full integration: 98 passed, exactly 1 isolated-install skip, and 1
  pre-existing Cyclopts warning in 4.02s.
- Final full default coverage gate: 1028 passed, exactly 1 isolated-install
  skip, and 1 pre-existing warning in 16.61s; 92.22% coverage.
- Env-enabled isolated install: 1 passed, 3 deselected in 4.46s with approved
  network access.
- Ruff check passed; Ruff format was applied to three new/changed test files;
  mypy succeeded for 60 source files.
- Release workflow SHA-256 remains
  `d421d9b3e5cdd03e1cb12036b72fdbd723ee3e054523d896c3db27aa9f87421a`;
  its Git diff is empty.
- Generated-artifact and archive-exclusion audits passed. `git diff --check`
  passed.
- Pre-commit passed all hooks.

Review-follow-up verification:

- Unit: 929 passed in 7.84s.
- Integration: 100 passed, exactly 1 isolated-install skip, and 1 pre-existing
  warning in 3.35s.
- Full default: 1030 passed, exactly 1 isolated-install skip, and 1
  pre-existing warning in 16.24s; 92.22% coverage.
- The dependency-resolving smoke path was unchanged, so the previously
  recorded genuine network-enabled pass was not repeated.
- Follow-up Ruff, format, mypy, pre-commit, release-byte, and `git diff --check`
  gates all passed.

No push, PR readiness change, merge, release, publication, self-adoption, or
fleet work was performed.
