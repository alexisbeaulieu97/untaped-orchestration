---
name: untaped-orchestration
description: Use typed repository orchestration stores safely.
---

# Untaped Orchestration

Use `untaped-orchestration` as the authoritative interface to a repository's
typed task and decision store. Keep bootstrap reads bounded and preserve the
store's revision guards and privacy boundaries.

## Safety rules

1. Run `brief --format json` before doing orchestration work.
2. Use returned IDs instead of scanning files.
3. Allocate one ID before init/create and reuse it through every retry.
4. Load only needed bodies with `show`.
5. Pass revisions on every guarded mutation.
6. Never use `--force-current`.
7. Never read or edit generated views.
8. Run `check` after hand edits or recovery.
9. Verify external evidence before recording it.
10. Never place tasks in a public store.
11. Stop readiness and delivery work on incomplete federation.

If a mutation is interrupted, inspect the reported paths and revisions, run
`check`, and retry only with the same caller-stable ID and guarded intent. Do
not guess at recovery or broadly restore the store.
