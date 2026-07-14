# Changelog

## 0.1.0 (Unreleased)

- Add strict TOML-front-matter task and decision stores with opaque Markdown
  bodies, explicit federation, deterministic generated views, and typed IDs.
- Add bounded bootstrap, query, readiness, curation, history, relation,
  evidence, import, validation, formatting, rendering, and recovery commands.
- Add guarded, lock-safe, atomic mutations with bounded accepted fault states
  and caller-stable retry identities.
- Report canonical-success/stale-view mutations as exit 1 and attach durable,
  acknowledged-path receipts to typed view-finalization failures.
- Add JSON, Pipe v1 status trailers, raw projection, and byte-exact malformed
  front-matter recovery contracts.
- Add the packaged agent skill, strict type marker, installed-wheel acceptance,
  CI, and approval-gated TestPyPI/PyPI release workflow.
- Audit exact wheel/sdist archive metadata, contents, exclusions, and RECORD
  integrity offline; PR CI additionally runs a dependency-resolving isolated
  install of the exact fresh wheel without checkout or development-environment
  leakage.
