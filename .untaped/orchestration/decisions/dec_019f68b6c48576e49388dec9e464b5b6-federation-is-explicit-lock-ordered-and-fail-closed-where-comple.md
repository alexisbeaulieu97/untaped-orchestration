+++
schema = "untaped.orchestration.decision/v1"
id = "dec_019f68b6c48576e49388dec9e464b5b6"
kind = "decision"
title = "Federation is explicit, lock-ordered, and fail-closed where completeness matters"
created_at = "2026-07-16T00:23:30.000Z"
tags = []

[[evidence]]
relation = "tracked-by"
reference = "git:01318c5a6ecf58a8afb897d4f34cc5b350a5c6ae:docs/superpowers/specs/2026-07-09-orchestration-v1-design.md#sha256:52d973e40559b2607c04031afc6ac84bc8a341bf599d653abf27501f99db1320"
+++
## 9. Discovery, federation, and completeness

Store discovery walks upward for `.untaped/orchestration/store.toml`;
`--store PATH` overrides it. Reads federate recursively by default and
`--local` restricts them. Writes modify one selected store only; permitted
cross-store references are validated but targets are never mutated.

Recursive reads resolve stores, sort normalized real paths, and acquire
store-wide `filelock` locks in that order with a ten-second default timeout. A
timeout makes the affected store explicitly incomplete.

`store child add` first resolves the current graph and the proposed child
subtree optimistically, computes the normalized-path union, acquires that union
in the same global order, then rereads every participating anchor and registry
under lock before validating/writing the selected parent's registry. Any
changed anchor, path, or registry causes a conflict and a fresh retry; the
command never acquires a newly discovered child lock out of order. Child remove
locks the current graph, rereads it under lock, and writes only the selected
parent. Neither operation mutates a child store.

| Command class | Missing/invalid child behavior |
|---|---|
| `show`, raw inspect | Targeted local recovery proceeds; unrelated warnings |
| `brief`, `list`, `search`, `trace` | Bounded partial data with `complete=false` |
| `check` | Report all; missing children warn unless `--require-children` |
| `next`, `curate next` | Fail closed unless `--local` |
| Start/deliver/structural mutation | Fail closed when required federation incomplete |
| Local decision clarification/evidence | Proceed when selected local store is valid |
| `render` | Always local-only |

