### 12.2 Atomic writes and multi-file interruption

Every individual file replacement uses a sibling temporary file, flush/fsync,
atomic rename, and parent-directory fsync under one store lock. Multi-file
operations validate their complete intended result before the first write and
use the fault-state protocol below.

- Init locks the target root, writes matching `store.toml` as its first
  canonical anchor, then `registry.toml`, instructions, and applicable views.
  Rerunning with the same store ID/config fills only missing expected files,
  accepts byte-identical completed files, and refuses every divergence.
- Close writes the complete archive destination before deleting the active
  source. Superseded close writes the successor link first. An interruption
  can leave a semantically matched active/archive pair, never a missing task.
- Decision supersession writes the linked successor before updating pins;
  retirement writes the retirement fields before removing a pin. An old
  supersede guard accepts only the exact successor-only phase, where deleting
  the successor in projection reconstructs the complete guarded store, then
  completes pin replacement from the still-original list. It never searches
  possible destroyed pin histories or adds a journal, sidecar, hidden operation
  ID, or caller source snapshot. Retirement retains its bounded exact reverse
  projection because only one known pin can have been removed.
- Rank rebalance uses the order-preserving protocol in section 5.2, finishes
  before the primary move/transition replacement, and is retryable from every
  replacement boundary with freshly read guards.
- Import accepts already-written byte-identical manifest records on retry and
  refuses divergent or unexpected files.

| Operation | Only accepted intermediate state | Detection and recovery |
|---|---|---|
| Init | No anchor plus ignored lock/temp only; matching `store.toml` plus a prefix of exact scaffold; or complete scaffold before acknowledgement | Before the anchor, retry removes only its own validated temporary. `check` reports missing scaffold after the anchor; same-ID/config retry fills the exact remainder or returns complete state with `replayed=true`. Divergence refuses recovery. |
| Task/decision create | The caller-stable ID is absent or one matching active item is fully durable | Retry with the same ID/inputs returns the existing item and `replayed=true`; mismatch or archived/inactive state conflicts. Fault injection includes final fsync before stdout. |
| Move or stage transition | Optional complete-scope same-order rebalance is partly/fully applied; primary parent/stage remain old while its rank may be old or neutral-rebalanced until the one final replacement | Store remains graph-valid and order-equivalent; inspect diff and reread item/store/anchor revisions. The old stale request conflicts. Rerun with fresh full guards; an already exact final target is an `applied=false`, `replayed=false` idempotent no-op. |
| Ordinary close | Complete archive exists with matching active source, or the active source is gone and the archive is final but acknowledgement was lost | `check` reports a duplicate pair; guarded retry/`repair duplicate` removes only a match. Retry of the same outcome/note against the final archive returns it with `replayed=true`; divergent closure conflicts. |
| Superseded close | Exact successor link may exist before the close pair, or the linked successor/final archive remain after active deletion but acknowledgement is lost | `check` reports a successor pointing at an active predecessor; reread both revisions and retry. When predecessor archive, successor link, outcome, and note exactly match the requested final state, retry returns `replayed=true`; every divergence conflicts. |
| Decision supersede | One exact linked successor may exist before pin replacement; successor/pins may be final before acknowledgement, but the destroyed prior pin membership/order is not recoverable from the old hash | Inactive pin is reported during the successor-only phase; the same canonical predecessor set/content reuses that successor and finishes deterministic pin replacement. After pin replacement, the old stale request conflicts. Reread and pass fresh full guards: an exact successor/predecessor set with no predecessor IDs still pinned is an `applied=false`, `replayed=false` no-op, whether the successor is pinned or unpinned. |
| Decision retire | Retirement fields may exist before pin removal, or retirement/pins are final before acknowledgement | Inactive pin is reported; same-note retry finishes removal or returns the final state with `replayed=true`. |
| Import | Exact subset of the external manifest may exist | Generic `check` cannot infer intent; rerun the same manifest/`--if-clean`, which reconstructs the guarded base and writes the remainder. |
| View render | Any subset of derived views may be stale after canonical success | `canonical_applied=true`, `views_current=false`; `render --write` replaces all applicable views deterministically. |

Multi-file commands and their errors include intended/changed relative paths
and current revisions in structured output whenever the process survives to
report them. Recovery never treats a syntactically valid but divergent file as
an accepted phase.

`check` reports partial init scaffold, duplicate active/archive copies,
incomplete lifecycle phases, inactive pins, invalid/duplicate ranks, orphan
temporaries, and stale views.
It cannot infer an incomplete external import manifest from otherwise valid
records. `repair duplicate
ID --if-active-revision HASH --if-archive-revision HASH --apply` removes only
an active copy whose semantic source projection exactly matches a valid archive
copy: identical preserved fields/body, archive `closed_from` equal to the
active stage, and valid close-only fields. The dry run shows the comparison.
Other divergence is never auto-resolved.

The documented recovery procedure is: inspect `check` and `git diff`; reread
reported revisions; rerun an operation when files match an accepted state;
otherwise use Git to restore only the affected paths after preserving unrelated
work, or repair one file explicitly. A broad store restore is never the default.
This documentation is not a Git dependency in the tool.

