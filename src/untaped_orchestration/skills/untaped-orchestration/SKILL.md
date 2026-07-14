---
name: untaped-orchestration
description: Use typed repository orchestration stores safely.
---

# Untaped Orchestration

Use `untaped-orchestration` as the only agent interface to canonical tasks and
decisions. Do not scan `.untaped/orchestration` to bootstrap context and do not
use generated views as machine input.

## Start bounded

1. Run `brief --format json` before orchestration work.
2. Check `complete` and `truncated`. If `complete=false` or `truncated=true`,
   do not infer omitted readiness; narrow the query, repair federation, or stop.
3. Use returned IDs instead of scanning files.
4. Load only needed bodies with `show`.

## Mutate safely

1. Allocate one ID before init/create and reuse it through every retry. The ID
   is caller-stable: acknowledgement loss is retried with the same ID and
   identical caller-owned inputs.
2. Pass revisions on every guarded mutation, including the expected revision
   for each item/store/registry/anchor named by that command.
3. Never use `--force-current`. Do not use `--force-current` even when a guard
   is stale; reread and reconsider the mutation.
4. Never read or edit generated views. Use parsed CLI reads and regenerate
   views with `render --write` when an authorized human workflow requires it.
5. Run `check` after hand edits or recovery, followed by `fmt --check` and
   `render --check` as applicable.
6. Supply only regular nonsymlink files for import manifests, replacement front
   matter, and body-file inputs. Keep front matter within 64 KiB and bodies
   within 1 MiB.

## Privacy, evidence, and readiness

1. Verify external evidence before recording it.
2. Never place tasks in a public store. Do not create or move tasks into a public store;
   v1 public stores are decision-only.
3. Stop readiness and delivery work on incomplete federation. Fail closed
   rather than treating partial results as ready.

If a mutation is interrupted, inspect the reported paths/revisions and
`git diff`. Retry only an accepted fault state with the same caller-stable ID
and guarded intent. Otherwise preserve unrelated work and restore or repair
only the affected paths. Never guess, broadly restore the store, invent a
replacement ID, or edit a generated view.

A failure receipt lists all intended paths but only acknowledged changed paths
whose writer calls returned. False/empty means no write was acknowledged, not
that an interrupted writer definitely left disk unchanged. Any receipt with
`views_current=false` requires `check`; after confirmed canonical success use
`render --write` to repair derived views.
