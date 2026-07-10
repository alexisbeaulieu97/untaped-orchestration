# AGENTS.md — untaped-orchestration

This public repository owns the `untaped-orchestration` standalone CLI: a
Git-native, typed orchestration store with bounded agent bootstrap output.

## Current phase: specification only

The authoritative design is
`docs/superpowers/specs/2026-07-09-orchestration-v1-design.md`. Until that
specification is reviewed and merged, do not add implementation code,
`pyproject.toml`, lockfiles, workflows, package configuration, or generated
store state. The next implementation session must re-read verified `main` and
produce a separate implementation plan from the landed specification.

## Repository boundaries

- This repository owns the tool's domain, CLI, package documentation, release
  workflow, packaged agent skill, and its own future decision store.
- Generic SDK behavior remains in `untaped`; do not copy SDK helpers here.
- The tool validates local files and offline references. It does not call Git,
  GitHub, PyPI, or other providers.
- Every push, PR, merge, workflow dispatch, release, tag, environment change,
  or publication remains an explicit approval gate.
- Commits are unsigned (`--no-gpg-sign`).

## Implementation conventions after the spec gate

- Python 3.14 and `uv`.
- Public imports come from `untaped.api`.
- Dependency direction is domain → application ports → infrastructure → CLI;
  inward layers never import outward layers.
- Use test-driven development and verify Ruff, formatting, mypy, pytest, and
  `uv build --no-sources` before proposing a PR.
- Update the packaged `SKILL.md`, README/docs, version metadata, lockfile, and
  release contracts whenever public behavior changes.
