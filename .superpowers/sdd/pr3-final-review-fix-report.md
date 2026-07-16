# PR 3 final review-fix report

Base: `f9440f2e90be766b389a1b111cd8c50c92d5eee8`

## Closed findings

1. All repository direct reads now use the bounded descriptor reader. Item/raw
   reads use `ITEM_FILE_LIMIT`; admin and instruction files use
   `FRONTMATTER_LIMIT`; managed views use `BODY_LIMIT`; valid atomic temporary
   files inherit their canonical target's limit. Descriptor growth and path
   substitution are rejected with store-relative typed diagnostics.
2. `inspect PATH --raw` is a typed query operation executed under the selected
   store's `FederationService.run(local=True)` lease. It never loads or locks
   children and fails closed if the lease has no reader.
3. Read-command query results derive their exit status through
   `result_exit_code`, including ORC007 exit 4 and ORC005 exit 3 for JSON,
   table, and Pipe output.
4. Empty active and history searches raise an expected ORC002 query diagnostic
   and exit 1 instead of leaking an internal-failure envelope and exit 5.
5. Maintenance view-comparison fallbacks re-raise `DiagnosticError` unchanged;
   only untyped `OSError` and `ValueError` failures degrade to stale-view state.
6. The shared `apply_views` boundary also re-raises typed failures from its
   initial comparison and view-write phases. Direct application, format writes,
   mutation finalization, and post-render comparisons preserve exact typed
   failures, while untyped renderer and writer failures retain the existing
   stale-view fallback.

No documentation, packaged skill, version, dependencies, lockfile, CI, or
release workflow changes were required: the public contracts already describe
bounded reads and typed diagnostics.

## TDD evidence

- Bounded direct reads: 10 expected failures before implementation; then
  `10 passed, 46 deselected`.
- Leased raw inspect: 3 expected failures before implementation; then
  `3 passed, 54 deselected`.
- Query exit mapping: 6 expected failures before implementation; then
  `6 passed, 34 deselected`.
- Empty search diagnostics: 4 expected failures before implementation; then
  `4 passed, 49 deselected`.
- Typed maintenance diagnostics: 3 expected failures before implementation;
  then `3 passed`.
- The first full-unit run exposed two atomic-temporary recovery regressions;
  both focused nodes then passed after inheriting the canonical target limit.
- Shared view failure propagation: 4 expected failures and one already-correct
  post-render comparison before implementation; then `5 passed, 67 deselected`.

## Verification

- Affected unit/integration slice: `260 passed` with one known Cyclopts warning.
- Full unit suite: `957 passed` after the shared view follow-up.
- Full integration suite: `102 passed, 1 skipped` with the expected offline
  isolated-install skip and one known Cyclopts warning.
- Full default suite: `1060 passed, 1 skipped`, 92.30% coverage, and the same
  one known Cyclopts warning after the shared view follow-up.
- Ruff check: clean.
- Ruff format check: 120 files formatted.
- Strict mypy: no issues in 60 source files.
