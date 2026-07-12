# AGENTS.md — untaped-orchestration

This public repository owns the `untaped-orchestration` standalone CLI. It is a
Git-native typed task/decision store with bounded agent bootstrap output.

## Repository boundaries

- This repository owns the domain, application services, filesystem adapters,
  CLI, package documentation, release workflow, and packaged agent skill.
- Generic tool composition and SDK behavior remain in `untaped`; public SDK
  imports come only from `untaped.api`.
- The tool performs local validation and offline evidence normalization. It
  does not call Git, GitHub, PyPI, or another provider.
- Do not add `.untaped/orchestration`, adoption workflows, migration manifests,
  cohort state, or fleet-repository content on the implementation branch.
- Pushes, PRs, merges, workflow dispatches, releases, tags, publication,
  environment changes, and self-adoption each require explicit approval.
- Commits are unsigned (`--no-gpg-sign`).

## Module ownership

- `domain/`: immutable IDs, Pydantic schemas, lifecycle/graph invariants,
  ordering, canonical field ownership, timestamps, and evidence references.
- `application/`: use-case requests/results, queries, federation, validation,
  guarded mutations, recovery protocols, curation, import, and ports.
- `infrastructure/`: strict codecs, bounded filesystem reads, atomic file
  replacement, locking, repository access, and deterministic Markdown views.
- `cli/`: Cyclopts command composition, request translation, format gating,
  output envelopes, diagnostics, and exit-code mapping.
- `skills/untaped-orchestration/SKILL.md`: shipped agent safety contract. Update
  it whenever commands, guards, recovery, privacy, or workflow behavior changes.
- `docs/`: user/operator contracts. The design under `docs/superpowers/specs/`
  remains the normative v1 reference.

Dependency direction is domain → application ports → infrastructure → CLI.
Inward layers never import outward layers. Avoid provider adapters, a database,
a cache, a write-ahead journal, or a Markdown AST.

## Development and verification

Use Python 3.14, `uv`, test-driven development, and caller-visible contract
tests. Run focused tests while iterating, then the complete gate:

```sh
uv --cache-dir .uv-cache run pytest PATH -q --no-cov
uv --cache-dir .uv-cache run ruff check .
uv --cache-dir .uv-cache run ruff format --check .
uv --cache-dir .uv-cache run mypy
uv --cache-dir .uv-cache build --no-sources
uv --cache-dir .uv-cache run pytest
uv --cache-dir .uv-cache run pre-commit run --all-files --show-diff-on-failure
git diff --check
```

Installed-wheel acceptance must build the current checkout into a fresh
artifact directory and use a fresh virtual environment; it must never select a
pre-existing `dist/` wheel. Keep README/docs, `CHANGELOG.md`, `py.typed`, the
packaged skill, version metadata, lockfile, CI, and release contracts aligned
with any public behavior change.
