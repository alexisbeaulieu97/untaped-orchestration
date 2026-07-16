+++
schema = "untaped.orchestration.decision/v1"
id = "dec_019f68b6c58f73bc92ac6dbfd1983fab"
kind = "decision"
title = "Agent bootstrap is a deterministically bounded brief"
created_at = "2026-07-16T00:23:30.000Z"
tags = []

[[evidence]]
relation = "tracked-by"
reference = "git:01318c5a6ecf58a8afb897d4f34cc5b350a5c6ae:docs/superpowers/specs/2026-07-09-orchestration-v1-design.md#sha256:52d973e40559b2607c04031afc6ac84bc8a341bf599d653abf27501f99db1320"
+++
### 10.5 `brief` hard bounds

`brief` includes local pinned decision bodies, the first in-progress task by
global ordering, ready items only when federation is complete, current
blockers (including `waiting_on`), due curation, missing/invalid-child warnings,
store revision, and item revisions needed for the next mutation.

- Maximum 10 pinned decisions.
- Maximum 4096 body bytes per decision.
- Maximum 16384 aggregate body bytes.
- Maximum 10 rows per dynamic section.
- Maximum 32768 output bytes.
- UTF-8 truncation only at code-point boundaries; every truncated value marks
  `truncated=true`.
- Incomplete federation sets `complete=false`, names missing store IDs, and
  never labels a task globally ready.

The byte ceiling applies to the exact serialized stdout for the selected
format, including framing and the trailing newline. Brief assembly first
selects deterministic candidate data. Diagnostics and `missing_store_ids` are
dynamic sections capped by `brief.max_rows_per_section`; their full counts are
also reported. The output layer rerenders after every reduction so JSON
escaping, table escaping, and diagnostics count toward the bound. It applies
this total order until the encoded result fits: remove the last ready row,
blocker row, due row, diagnostic, and missing-store ID in that order, repeating;
shorten pinned decision bodies last-to-first at UTF-8 code-point boundaries;
shorten remaining variable human text last-to-first; then replace item detail
with ID/revision-only summaries while retaining store ID/revision,
completeness, truncation, and full counts. `truncated=true` records any step.
The resulting minimal table/JSON brief consists only of fixed keys, bounded
typed IDs/revisions/counts, and fits within the 4096-byte minimum. Brief
supports only table and JSON, so no Pipe framing or raw projection is involved.
Tests use escape-heavy Unicode and maximum diagnostics rather than bounding
only the pre-serialization model.

