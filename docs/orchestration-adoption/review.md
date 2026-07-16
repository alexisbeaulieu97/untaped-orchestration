# Orchestration v1 self-adoption review

Date: 2026-07-16

Independent reviewer: Codex review subagent `market_permission_auditor`

Reviewed range:
`01318c5a6ecf58a8afb897d4f34cc5b350a5c6ae..dbd3f856eab8af1c91ce8393b923f89d9d1da615`

## Verdict: ACCEPT

No blocking, important, or minor findings.

## Frozen source and selected decisions

The frozen normative design is exactly 75,697 bytes and 1,477 LF-terminated
lines with SHA-256
`52d973e40559b2607c04031afc6ac84bc8a341bf599d653abf27501f99db1320`.
All six inclusive ranges independently match their locked byte counts and
SHA-256 values: `60-84` / 794, `284-306` / 942, `637-666` / 1,694,
`848-882` / 1,964, `1032-1093` / 6,326, and `1206-1226` / 809. Each
range ends in the required terminal blank-line bytes and each record is
byte-identical to its frozen range. IDs, titles, timestamp, and tracked-by
evidence are exact.

`decision-sources.toml` truthfully uses the selected-decision schema and scope,
sets `full_file_coverage=false`, and states that all non-selected lines remain
authoritative and are neither migrated nor dispositioned. The full design
remains normative. Its only changes are narrow rollout-truth corrections: the
self-adoption gate, accepted but nonauthoritative pilot, current ten-decision
GitHub source and hash, superseded five-decision pilot source, and remaining
adoption gates. The corrected design has Git blob
`718808d892707e56e87ddc5bfe66b69d054a4f1c` and SHA-256
`44ed8ff16da38e66223d1c9350136d763b7f3e6bc62eae5614a04487dadf529b`.

## Store, workflow, privacy, and scope

The public task-disabled store is childless and contains exactly six decisions
pinned in source order at final revision
`sha256:eaaf016bba996d9a62712439518759a4cc9b861dfc68f77d3e55fd2e6de212ea`.
A released-0.1.0 brief returned all six IDs, complete non-recovery bodies, the
exact 4,096-byte recovery prefix, truthful envelope-level truncation, the same
revision, and 13,193 total encoded bytes. A public task probe returned ORC009
without changing canonical state.

The dedicated workflow is read-only, path-filtered, bounded, exactly pinned,
and runs only `untaped-orchestration==0.1.0`. The path-scoped Git and
pre-commit whitespace rules preserve only the imported bodies' mandated final
blank-line LFs. Package code, skill, version, dependencies, lockfile, release
workflow, changelog, historical plan, and all fleet/child state are unchanged.
Wheel and sdist audits exclude the repository store and adoption evidence.

Focused contracts passed (14 tests), as did released `check --local`,
`fmt --check --local`, `render --check`, frozen-source recomputation, and exact
base-to-head `git diff --check`. The independently reviewed implementation
evidence records 1,083 passing tests, exactly one intended isolated-install
skip, 92.30% coverage, and green lint, formatting, typing, hooks, build, and
archive audits.
