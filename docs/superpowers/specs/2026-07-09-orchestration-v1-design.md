# Untaped Orchestration v1 — design specification

Date: 2026-07-09
Status: proposed, docs-only
Repository: `alexisbeaulieu97/untaped-orchestration`
Package and console command: `untaped-orchestration`
Import package: `untaped_orchestration`

## 1. Purpose and authority

`untaped-orchestration` provides a portable contract for orchestration state
that is readable in Git, strict enough for deterministic automation, and
bounded enough for repeated agent bootstrap. Each adopting repository owns a
local store under `.untaped/orchestration/`; an explicit recursive registry
connects stores without filesystem discovery.

This specification implements the hub ruling **“2026-07-09 — standalone public
orchestration direction approved”** at hub commit `53a48ca`, plus the current
roadmap gate. The SDK prerequisite is satisfied by `untaped==3.1.0`, core commit
`80bb841`, and GitHub release/tag `v3.1.0`.

The design was previously approved in conversation and pressure-tested before
repository creation. This document is the owning-repository contract. It does
not authorize implementation, publication, or fleet migration.

### 1.1 Goals

- Keep canonical source human-readable, hand-editable, Git-diffable, and
  repository-owned.
- Give agents typed, revision-guarded commands instead of requiring Markdown
  scraping or full-corpus reads.
- Bound routine bootstrap with a generated `brief` contract.
- Make task/decision lifecycle, curation, relations, privacy, and federation
  explicit and checkable.
- Preserve useful human views as deterministic committed projections.
- Support exact, reviewed migration without permanently embedding legacy
  parsers.
- Detect malformed hand edits precisely and provide deterministic recovery.

### 1.2 Non-goals

V1 does not include:

- A database, daemon, persistent index, or derived cache authority.
- A Markdown parser, heading schema, or Markdown AST.
- Provider APIs or network verification.
- A Git adapter, Git history browser, or Git mutation commands.
- Filesystem crawling for repositories or stores.
- Cross-store structural writes or moving an item between stores.
- Automatic task aging, start, close, release, publication, PR, or migration.
- Cursor pagination.
- Compatibility shims for legacy orchestration documents after migration.

Git is the collaboration and disaster-recovery layer, but the tool neither
imports Git nor shells out to it. Crash recovery uses atomic per-file writes,
deterministic checks, safe operation retry, and documented `git restore`/retry
procedures—not a write-ahead journal.

## 2. Architecture

Canonical Markdown files are the only authority. The CLI parses typed TOML
front matter and treats every body byte after the delimiter as opaque. Reads
build bounded in-memory header models; body-heavy operations load or stream
only the bodies they need. Generated views are committed derived artifacts and
never become input to the tool.

Implementation dependency direction is:

```text
domain
  ↑
application
  ↑
infrastructure
  ↑
CLI/composition
```

The domain and application layers do not import filesystem, `filelock`, CLI,
or infrastructure modules. Application ports are defined inward before their
adapters: `Clock`, `IdGenerator`, `StoreReader`, `StoreWriter`, `LockManager`,
and `ViewRenderer`. There is no `Vcs`, transaction-journal, or provider port.

## 3. Canonical store

### 3.1 Layout

```text
.untaped/orchestration/
├── store.toml
├── registry.toml
├── AGENTS.md
├── CLAUDE.md
├── tasks/
├── decisions/
├── archive/
│   └── tasks/
├── views/
│   ├── roadmap.md
│   ├── backlog.md
│   ├── inbox.md
│   └── decisions.md
└── .lock                 # ignored runtime file
```

Rules:

- Item directories are lazy; empty directories require no sentinel.
- `.lock`, sibling atomic-write temporary files, and editor artifacts are
  ignored.
- Canonical files and applicable generated views are committed.
- Decision-only stores generate only `views/decisions.md`.
- A private task-capable store generates all four views. Public task-capable
  stores are forbidden in v1.
- Views project one local store only. They never contain recursive federation
  data or private data from another repository.
- Agents use the CLI and never read views as input. Humans may read but not edit
  them.
- `CLAUDE.md` is exactly `@AGENTS.md`; `AGENTS.md` is a concise store-local
  bootstrap and approval boundary.

### 3.2 `store.toml`

Schema: `untaped.orchestration.store/v1`.

| Field | Contract |
|---|---|
| `schema` | Exact schema constant |
| `id` | Immutable `sto_` UUIDv7 identifier |
| `name` | Nonempty display name, maximum 120 Unicode characters |
| `visibility` | `private` or `public` |
| `timezone` | Valid IANA timezone for date-based curation |
| `capabilities.active_tasks` | Whether active and archived tasks may exist |
| `curation.inbox_review_days` | Positive integer; default `7` |
| `curation.in_progress_review_days` | Positive integer; default `14` |
| `brief.pinned_decisions` | Ordered unique active local decision IDs; maximum 10 |
| `brief.max_decision_body_bytes` | Default `4096` |
| `brief.max_total_body_bytes` | Default `16384` |
| `brief.max_rows_per_section` | Default `10` |
| `brief.max_total_bytes` | Default `32768` |

Canonical private task-capable configuration:

```toml
schema = "untaped.orchestration.store/v1"
id = "sto_019f0000000070008000000000000000"
name = "Untaped orchestration hub"
visibility = "private"
timezone = "America/Montreal"

[capabilities]
active_tasks = true

[curation]
inbox_review_days = 7
in_progress_review_days = 14

[brief]
pinned_decisions = []
max_decision_body_bytes = 4096
max_total_body_bytes = 16384
max_rows_per_section = 10
max_total_bytes = 32768
```

`init` defaults to a private task-capable store. `init --public` creates a
public decision-only store; `visibility = "public"` requires
`capabilities.active_tasks = false`. `check` rejects any active/archive task in
a decision-only or public store, including after a hand-edited policy change.
V1 deliberately has no public-task escape hatch: unfinished tasks remain
private, and an explicit future schema is required before public task stores
can exist.

Visibility and capabilities remain hand-editable administrative declarations;
the tool does not query Git hosting visibility. A valid `check` is required
before subsequent typed mutations and in adoption CI.

### 3.3 `registry.toml`

Schema: `untaped.orchestration.registry/v1`.

```toml
schema = "untaped.orchestration.registry/v1"
store_id = "sto_019f0000000070008000000000000000"

[[children]]
id = "sto_019f0000000070008000000000000001"
path = "../../untaped/.untaped/orchestration"
```

- Every entry contains the expected immutable store ID and a POSIX-style path
  relative to the parent store root.
- `..` and a symlinked repository/store root are allowed for sibling checkouts.
- Symlinks below the resolved store root in canonical or view directories are
  rejected.
- Traversal compares normalized real paths and store IDs.
- Duplicate IDs, duplicate normalized paths, self-registration, ancestor
  cycles, and case-folding aliases are errors.
- There is no ambient filesystem discovery. A child exists only when
  registered.
- Every store may register children recursively.
- Missing or invalid children create explicit incompleteness, never silent
  omission.

The registry key order shown above is canonical: schema/store ID first, then
`[[children]]` records sorted by child ID with `id` before `path`. `fmt` covers
both administrative TOML files as well as item front matter; it validates and
canonicalizes the full `store.toml`/`registry.toml` shapes using the key/table
order in this section.

## 4. Item file format

### 4.1 Identity and filenames

- Store IDs: `sto_` plus 32 lowercase UUIDv7 hexadecimal characters.
- Task IDs: `tsk_` plus 32 lowercase UUIDv7 hexadecimal characters.
- Decision IDs: `dec_` plus 32 lowercase UUIDv7 hexadecimal characters.

```text
tsk_<id-body>-<creation-slug>.md
dec_<id-body>-<creation-slug>.md
```

The slug is derived once at creation, lowercase ASCII matching
`[a-z0-9]+(?:-[a-z0-9]+)*`, at most 64 characters, cosmetic, and immutable.
It need not follow later title edits. The filename ID must equal the metadata
ID. Raw recovery locates files by safe filename prefix before parsing content.

### 4.2 Front-matter grammar

Every item:

- Is UTF-8 without a byte-order mark.
- Starts at byte zero with `+++\n`.
- Contains TOML 1.0 metadata.
- Ends metadata at the next line containing exactly `+++`.
- Has at most 64 KiB of front matter and 1 MiB of Markdown body.
- Uses LF in rewritten front matter.
- Preserves every body byte after the closing delimiter, including newline
  style and final-newline presence.

`tomllib` parses the metadata and supplies syntax locations when available.
Pydantic models use `extra="forbid"`. Duplicate TOML keys are syntax errors.
No Markdown content is parsed or normalized.

Canonical metadata serialization uses fixed schema key order, canonical TOML
basic strings, sorted unique tags, links sorted by relation/store/ID, and
evidence sorted by relation/reference. Metadata comments are noncanonical and
`fmt --write` removes them. `tomli-w` output is reparsed and revalidated before
replacement.

### 4.3 Canonical examples

A task file is strict TOML metadata followed by an opaque Markdown body:

```toml
+++
schema = "untaped.orchestration.task/v1"
id = "tsk_019f0000000070008000000000000010"
kind = "task"
title = "Land the public orchestration specification"
created_at = "2026-07-10T01:02:03.004Z"
tags = ["orchestration", "specification"]
stage = "backlog"
priority = "high"
rank = 1000
revisit_when = "The SDK 3.1.0 release is independently verified."
waiting_on = ["alexis"]

[[links]]
relation = "governed-by"
target_store_id = "sto_019f0000000070008000000000000000"
target = "dec_019f0000000070008000000000000001"

[[evidence]]
relation = "tracked-by"
reference = "github-pr:alexisbeaulieu97/untaped-orchestration#1"
+++

## Context

This body is ordinary Markdown. The tool does not parse its headings.
```

A decision file uses the same envelope and has no persisted lifecycle-state
field:

```toml
+++
schema = "untaped.orchestration.decision/v1"
id = "dec_019f0000000070008000000000000001"
kind = "decision"
title = "Use TOML front matter and opaque Markdown bodies"
created_at = "2026-07-10T01:00:00.000Z"
tags = ["format", "orchestration"]
+++

The typed envelope is machine-owned. This prose remains human-owned.
```

The opening and closing delimiters shown inside these fenced examples are
literal lines in the item file. Hand edits may change bodies freely. A hand
edit to metadata becomes canonical only after `check` and `fmt --check` pass;
`fmt --write` can normalize valid metadata but never invent missing values.
Canonical key order is common scalars (`schema`, `id`, `kind`, `title`,
`created_at`, `tags`), kind-specific scalars in the order documented below,
then `[[links]]`, then `[[evidence]]`. Optional absent fields consume no slot.

### 4.4 Common fields

| Field | Type and invariant |
|---|---|
| `schema` | Exact task or decision schema |
| `id` | Immutable typed identifier |
| `kind` | `task` or `decision` |
| `title` | Nonempty, maximum 240 Unicode characters |
| `created_at` | UTC `YYYY-MM-DDTHH:MM:SS.sssZ` |
| `tags` | Sorted unique lowercase slugs; maximum 32 |
| `links` | Sorted typed link records |
| `evidence` | Sorted typed evidence records |

Tag slugs use the same 64-character lowercase slug grammar as waiting-party
slugs. The 64 KiB front-matter ceiling is also the hard aggregate bound for
links and evidence; commands refuse mutations that would exceed it.

Every persisted semantic timestamp ending in `_at` uses the same exact UTC
millisecond representation as `created_at`:
`YYYY-MM-DDTHH:MM:SS.sssZ`. Calendar fields ending in `_on` remain exact
`YYYY-MM-DD` dates interpreted in the store timezone.

The tool derives but never persists:

- `revision`: SHA-256 of exact item bytes, formatted `sha256:<hex>`.
- `store_revision`: SHA-256 over the sorted relative paths and file hashes of
  `store.toml`, `registry.toml`, store-local `AGENTS.md`/`CLAUDE.md`, and every
  active/archive item, excluding `views`, `.lock`, and temporary files.
- Task blocked/readiness and curation-due state.
- Decision lifecycle state.

There is no generic `updated_at`; Git records edits and domain timestamps exist
only where semantics require them.

## 5. Task lifecycle

### 5.1 Active fields

| Field | Contract |
|---|---|
| `stage` | `inbox`, `backlog`, `planned`, or `in-progress` |
| `priority` | `critical`, `high`, `normal`, or `low` |
| `rank` | Positive sparse signed-64-bit integer |
| `parent` | Optional same-store task ID; containment is child-owned |
| `started_at` | Set on first entry to `in-progress`; never cleared |
| `revisit_when` | Required/nonempty in backlog; forbidden elsewhere |
| `reviewed_at` | Optional last acknowledged-review timestamp |
| `review_on` | Optional exact `YYYY-MM-DD` |
| `waiting_on` | Sorted unique person/team slugs; maximum 8 |

`task create` defaults to `stage = "inbox"`, `priority = "normal"`, and the
last rank in its top-level inbox scope. `waiting_on` is an explicit manual
blocker: a nonempty list removes the task from `next` and makes entry into
`in-progress` or delivered closure fail until cleared. This makes queries such
as `list --waiting-on alexis` authoritative rather than a tag convention.
Each waiting-party slug matches `[a-z0-9]+(?:-[a-z0-9]+)*`, is at most 64
characters, and identifies a person or team according to repository-local
convention.

“Blocked” is derived from `waiting_on`, dependencies, descendants, and required
federation state; it is not a stage.

### 5.2 Sparse ordering

Rank scope is `(parent task or top level, stage)`. Initial ranks are 1000,
2000, 3000, and so on. Midpoint insertion, half-first prepend, and +1000 append
are used while an integer gap exists. When none exists, the complete scope is
deterministically renumbered in steps of 1000 under the store lock without
changing relative order. Rank decreases are written first-to-last; increases
are then written last-to-first; unchanged ranks are skipped. This keeps ranks
unique and strictly ordered after every individual replacement, so interruption
never reorders the scope.

Rebalance is a semantically neutral phase completed before a requested move or
transition. Only after that phase is durable does one atomic replacement of the
primary task change its `parent`, `stage`, and/or final rank. If rebalance is
interrupted, the task remains in its original parent/stage and the caller
rereads the item/store revisions before retrying. Users move items with
`--first`, `--last`, `--before`, or `--after`; generic update never sets rank
or parent.

Global ordering is priority, ancestor rank vector, own rank, then ID.

### 5.3 Transitions

| From | To | Required behavior |
|---|---|---|
| `inbox` | `backlog` | Require `revisit_when` |
| `inbox` | `planned` | Clear `revisit_when` |
| `backlog` | `planned` | Clear `revisit_when` |
| `planned` | `backlog` | Require `revisit_when` |
| `planned` | `in-progress` | Refuse when blocked/incomplete; set `started_at` once |
| `in-progress` | `planned` | Pause; preserve `started_at` |

All other transitions are rejected. Inbox-to-in-progress requires two explicit
transitions. Entering a new stage places the item last unless relative
placement is supplied, clears `revisit_when` except in backlog, and does not
alter `review_on`. `task transition --to backlog --revisit-when TEXT` is also
the only allowed same-stage transition and replaces the backlog trigger;
update cannot write that lifecycle-owned field. Other same-stage changes use
update, move, or review commands.

### 5.4 Curation

Due dates use the configured timezone and injected clock:

- Inbox: explicit `review_on`; otherwise local date of `reviewed_at` or
  `created_at`, plus `inbox_review_days`.
- In progress: explicit `review_on`; otherwise local date of `reviewed_at` or
  `started_at`, plus `in_progress_review_days`.
- Backlog/planned: due only when `review_on` is set; backlog always retains
  `revisit_when` as its semantic trigger.
- Decisions: due only when `review_on` is set.

`curate next` sorts due date, kind (`task` before `decision`), then task
priority/rank or decision title, then ID. `curate acknowledge` sets
`reviewed_at` and clears `review_on`; `curate snooze --until DATE` changes only
`review_on`. `task review` aliases acknowledge for tasks. No automatic
lifecycle change occurs.

### 5.5 Closing and archive

Closing moves a task to `archive/tasks/`, removes `stage`, and adds
`closed_from`, `outcome`, `closed_at`, and required nonempty `close_note`.
Other fields/body are preserved.

| Outcome | Preconditions |
|---|---|
| `delivered` | Dependencies delivered; no waiting party; federation complete; all descendants delivered |
| `declined` | All descendants archived; dependencies may remain unsatisfied |
| `superseded` | All descendants archived; same-store successor links with `supersedes` |
| `cancelled` | `started_at` exists and all descendants are archived |

Archived tasks are immutable except that `evidence add` may append newly
verified evidence. Evidence removal, body/metadata edits, and link edits are
refused.

Superseded close is one guarded command:
`task close PREDECESSOR --outcome superseded --successor SUCCESSOR --note ...`
with current revisions for both tasks. It adds the lifecycle-owned
`successor -> predecessor` link before archiving the predecessor. A successor
must be active, same-store, and distinct; the ordinary close preconditions
still apply.

## 6. Decision lifecycle

Decision fields add optional `reviewed_at`, `review_on`, `retired_at`, and
`retire_note`. Retirement fields are both present or both absent.

Derived state is one of:

- `active`: no incoming supersession and no retirement fields.
- `superseded`: exactly one incoming successor and no retirement fields.
- `retired`: `retired_at` and a nonempty `retire_note` are present and there is
  no incoming successor.

Typographical changes and clarifications that do not change the ruling use
`decision update`. A changed ruling creates a new decision through `decision
supersede`; the predecessor remains intact. A decision whose governed mechanism
simply ended uses `decision retire --note ...` and cannot later be superseded.
The same guarded supersede/retire operation also maintains `store.toml`: a
successor replaces its pinned predecessor, while retirement removes the pin.
These are ordered atomic-per-file writes, not a multi-file transaction;
interrupted exact phases are detected and completed by safe retry as specified
in section 12.2.

Each predecessor has at most one successor; one successor may consolidate
several predecessors. Decisions remain under `decisions/` rather than moving to
an archive. A multi-predecessor `decision supersede` supplies every predecessor
ID and current revision; all predecessors and the successor are same-store.
Pinned decision IDs are ordered, unique, and resolve to active local decisions.
When several pinned predecessors collapse, the successor occupies the earliest
predecessor position; later predecessor/successor duplicates are removed while
all unrelated pin order is preserved.

Allowed decision mutations by derived state:

| Command | Active | Superseded/retired |
|---|---|---|
| `decision update` (title/body/tags clarification only) | Allowed | Allowed |
| `curate acknowledge` / `curate snooze` | Allowed | Refused; inactive `review_on` is historical only |
| `evidence add` | Allowed | Allowed |
| `evidence remove`, generic link add/remove | Allowed | Refused |
| `decision supersede` / `decision retire` | Allowed | Refused |

Supersede retry with the same predecessor set and ruling content reuses one
already-written exact successor before finishing pin maintenance; a divergent
incoming successor refuses recovery. Retire retry accepts the same already
written retirement fields only to finish pin removal.

## 7. Relations and graph safety

Containment is persisted once, as the child's optional `parent` field. The CLI
derives parent-to-child traversal from those child-owned values; it never stores
reciprocal containment links. Every persisted link has `relation`,
`target_store_id`, and `target`.

| Relation | Direction/kinds | Locality | Semantics |
|---|---|---|---|
| `depends-on` | task dependent → prerequisite | Same store | Readiness DAG |
| `governed-by` | task → decision | Cross-store allowed | Policy navigation |
| `supersedes` | successor → same-kind predecessor | Same store | Lifecycle-owned |
| `follow-up-to` | newer task → active/archive task | Cross-store allowed | Navigation only |

`task move` exclusively owns `parent`; typed supersede/close flows own
`supersedes`; generic link commands handle only dependency, governance, and
follow-up relations. Parent and dependency targets are same-store. Cross-store
structural relations are forbidden because invariant maintenance cannot span
independent repositories.

An active task's parent, when present, must be active; archived tasks preserve
their historical parent ID. Validation builds the child-owned containment
forest, dependency and per-kind supersession graphs, plus a combined
completion-precedence graph where prerequisites precede dependents and children
precede parents. Any individual or combined cycle is an error.

A `governed-by` link to a superseded or retired decision remains valid
historical navigation, but `check` warns and `brief` identifies the inactive
ruling so a repository can point the task at the current decision explicitly.

Dependency readiness:

| Prerequisite state | Result |
|---|---|
| Active | Blocked |
| Archived `delivered` | Satisfied |
| Archived other outcome | Unsatisfied blocker |
| Missing target in complete federation | Invalid link |
| Missing/invalid target store | Unknown; fail closed |

Blocked tasks may be clarified, moved, reviewed, declined, superseded, or
cancelled when eligible. They may not enter in-progress or close delivered.

## 8. Evidence

Relations are `tracked-by`, `implemented-by`, `verified-by`, `released-as`,
and `published-as`. References are offline strings; the tool validates syntax,
not truth.

| Scheme | Example | Canonicalization |
|---|---|---|
| `github-pr` | `github-pr:owner/repo#35` | Lowercase owner/repo; positive number |
| `github-issue` | `github-issue:owner/repo#12` | Lowercase owner/repo; positive number |
| `github-release` | `github-release:owner/repo@v1.0.0` | Lowercase owner/repo; preserve tag |
| `github-commit` | `github-commit:owner/repo@<40-hex>` | Lowercase owner/repo/SHA |
| `pypi` | `pypi:untaped-orchestration@0.1.0` | PEP 503 project normalization |
| `url` | `url:https://example.com/path` | HTTPS; lowercase host; remove default port |

Unknown lowercase schemes matching `[a-z][a-z0-9-]*:<nonspace-payload>` are
accepted opaquely. Duplicates after canonicalization are rejected. Agents must
verify facts externally before adding evidence.

## 9. Discovery, federation, and completeness

Store discovery walks upward for `.untaped/orchestration/store.toml`;
`--store PATH` overrides it. Reads federate recursively by default and
`--local` restricts them. Writes modify one selected store only; permitted
cross-store references are validated but targets are never mutated.

Recursive reads resolve stores, sort normalized real paths, and acquire
store-wide `filelock` locks in that order with a ten-second default timeout. A
timeout makes the affected store explicitly incomplete.

| Command class | Missing/invalid child behavior |
|---|---|
| `show`, raw inspect | Targeted local recovery proceeds; unrelated warnings |
| `brief`, `list`, `search`, `trace` | Bounded partial data with `complete=false` |
| `check` | Report all; missing children warn unless `--require-children` |
| `next`, `curate next` | Fail closed unless `--local` |
| Start/deliver/structural mutation | Fail closed when required federation incomplete |
| Local decision clarification/evidence | Proceed when selected local store is valid |
| `render` | Always local-only |

## 10. Query and CLI contracts

### 10.1 Global options

```text
--store PATH
--local
--format table|json|pipe|raw
--columns FIELD  # repeatable; -c alias
--limit N
--debug
```

V1 intentionally omits cursors. Query limits are positive; default is 50 and
maximum is 200.
Results use deterministic ordering and report truncation; callers narrow
filters or raise the limit. Cursor pagination may be added compatibly only when
real store sizes justify it.

### 10.2 Read commands

| Command | Purpose |
|---|---|
| `brief` | Bounded agent bootstrap |
| `list` | Filter active tasks/decisions, including `--waiting-on` |
| `show ID` | Parsed item plus revision |
| `show ID --raw` | Filename-first raw bytes for malformed recovery |
| `inspect PATH --raw` | Raw malformed file without usable ID |
| `search QUERY` | Streaming metadata/body search |
| `trace ID` | Link/evidence traversal |
| `next` | Globally safe ready leaves |
| `curate next` | Due curation items |
| `history list/search/show` | Canonical archived tasks, not Git history |

`next` returns active containment leaves with no waiting party, active
descendant, unsatisfied dependency, invalid store, or invalid canonical data. It
orders priority, ancestor rank vector, rank, ID and reports ancestor path,
unblocks count, due state, governing decisions, evidence summary, and revision.
It recommends but never starts work.

### 10.3 Typed mutation commands

```text
init
task create|update|transition|move|review|close
decision create|update|supersede|retire
link add|remove
evidence add|remove
curate acknowledge|snooze
store child add|remove|list
store import
check
fmt --check|--write
render --check|--write
repair frontmatter PATH
repair duplicate ID
```

There is no generic item update. Every canonical mutation requires the current
primary item revision. Move and transition additionally require the store
revision and assert the current child-owned parent, including explicit `none`,
so every rank scope and relative anchor is guarded. A superseded task close
guards predecessor and successor revisions; multi-predecessor decision
supersede guards every predecessor. Decision supersede/retire also require the
store revision because they may rewrite pins. Registry writes require the
registry revision. Batch import/format requires the store revision.
Agents always pass guards and the packaged skill forbids `--force-current`; a
human may explicitly use it.
Mutations are noninteractive, with required notes, outcomes, dates, revisions,
and confirmations provided as flags. Repair and import default to dry-run and
write only with `--apply`.

Typed ownership:

- create/import: full-record initialization, including identity/creation time;
- `task update`: title, body, priority, tags, and `waiting_on`;
- transition: stage, transition timestamps, and `revisit_when`;
- move: parent;
- shared placement protocol invoked only by transition/move: rank;
- close: archive fields and, for superseded outcome, the successor link;
- decision update: title, body, and tags for non-ruling clarifications;
- decision supersede/retire: lifecycle fields and pin maintenance;
- link/evidence/curation commands: only their named fields;
- renderer: views only;
- revisions/readiness/activity/due state: derived only.

Create/import initialize complete records and explicit repair may replace
invalid bytes, so those are controlled exceptions rather than competing normal
mutation paths. Hand edits are supported, but `check` proves only the resulting
state, not the historical transition that produced it. Content/config edits
remain ordinary; direct lifecycle/relation edits are an emergency recovery
path that requires explicit Git review and a clean `check`/`fmt --check`.

### 10.4 Structured output deviation

JSON uses a stable command envelope rather than the SDK's ordinary bare
row/object JSON:

```json
{
  "schema": "untaped.orchestration.output/v1",
  "command": "next",
  "complete": true,
  "truncated": false,
  "data": [],
  "diagnostics": []
}
```

This deliberate deviation is required because federation completeness,
truncation, and diagnostics are part of safe machine interpretation, not human
stderr decoration. YAML is intentionally omitted in v1 so there is one
structured agent contract. Table/raw commands use stderr diagnostics. Pipe
preserves the SDK Pipe v1 envelope exactly, one NDJSON object per line:

```json
{"untaped":"1","kind":"orchestration.task","record":{"id":"tsk_..."}}
```

Kinds are `orchestration.store`, `.task`, `.decision`, `.evidence`, and
`.diagnostic`; `record` contains command data or the structured diagnostic.
Pipe ignores columns. Raw output defaults its first field to stable ID;
repeatable `--columns FIELD`/`-c FIELD` controls additions and supports the
SDK's dotted paths.

Expected domain failures remain structured JSON on stdout under JSON mode.
Unexpected traces appear only with `--debug`.

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

## 11. Diagnostics and exit codes

Diagnostics contain stable `code`, `severity`, `path`, `field`, optional
proven `line`, `column`, and `byte_offset`, plus `message` and `hint`. Sort
order is severity, normalized path, location, field, code.

| Code | Meaning |
|---|---|
| `ORC001` | Encoding, delimiter, or TOML syntax |
| `ORC002` | Schema/field/type violation |
| `ORC003` | ID, filename, path, or store identity mismatch |
| `ORC004` | Relation/cardinality/cycle/graph violation |
| `ORC005` | Registry/federation incomplete or invalid |
| `ORC006` | Lifecycle, curation, close, or retirement invariant |
| `ORC007` | Revision or lock conflict |
| `ORC008` | Generated view missing or stale |
| `ORC009` | Visibility/capability policy violation |

Exit codes extend the SDK's stable `0/1/2` convention deliberately:

| Exit | Meaning |
|---|---|
| `0` | Success |
| `1` | Invalid canonical data or stale views |
| `2` | CLI usage error |
| `3` | Required federation incomplete |
| `4` | Lock or revision conflict |
| `5` | I/O or unexpected internal failure |

No recovery-required exit exists because v1 has no journal. The CLI translates
domain errors explicitly rather than relying on SDK `finish()`, whose batch
failure contract remains exit 1.

Partial-tolerant reads such as `brief` and `list` exit 0 with
`complete=false`; fail-closed readiness, start, delivery, and structural
operations exit 3 when required federation is incomplete. `check` warnings
alone exit 0 unless `--require-children` promotes missing-child diagnostics to
errors.

## 12. Formatting, atomicity, and recovery

### 12.1 `fmt`

`fmt --check` covers `store.toml`, `registry.toml`, and every item. It
parses/validates metadata, serializes canonically in memory,
reparses/revalidates it, and compares expected full bytes. `fmt --write`
requires item/store/registry revision guards as applicable, refuses invalid
metadata, rewrites administrative TOML as a complete atomic file and item front
matter as a bounded region, preserves item body bytes, and never renames files
or guesses semantic repairs.

### 12.2 Atomic writes and multi-file interruption

Every individual file replacement uses a sibling temporary file, flush/fsync,
atomic rename, and parent-directory fsync under one store lock. Multi-file
operations validate their complete intended result before the first write and
use the fault-state protocol below.

- Close writes the complete archive destination before deleting the active
  source. Superseded close writes the successor link first. An interruption
  can leave a semantically matched active/archive pair, never a missing task.
- Decision supersession writes the linked successor before updating pins;
  retirement writes the retirement fields before removing a pin. Retry accepts
  an already-applied exact phase only when it matches the provided guarded
  intent, then completes the remaining phase.
- Rank rebalance uses the order-preserving protocol in section 5.2, finishes
  before the primary move/transition replacement, and is retryable from every
  replacement boundary with freshly read guards.
- Import accepts already-written byte-identical manifest records on retry and
  refuses divergent or unexpected files.

| Operation | Only accepted intermediate state | Detection and recovery |
|---|---|---|
| Move or stage transition | Optional same-order rebalance is partly/fully applied; primary task still has its old parent/stage/rank until its one final replacement | Store remains graph-valid and order-equivalent; inspect diff, reread item/store revisions, rerun. Final target state is an idempotent success. |
| Ordinary close | Complete archive exists while semantically matching active source remains | `check` reports the pair; guarded retry or `repair duplicate` removes only the matched active source. |
| Superseded close | Exact successor link may exist before the close pair | `check` reports a successor pointing at an active predecessor; reread both revisions and retry the same successor/outcome. |
| Decision supersede | One exact linked successor may exist before pin replacement | Inactive pin is reported; the same predecessor set/content reuses that successor and finishes deterministic pin replacement. |
| Decision retire | Retirement fields may exist before pin removal | Inactive pin is reported; same-note retry finishes removal. |
| Import | Exact subset of the external manifest may exist | Generic `check` cannot infer intent; rerun the same manifest/`--if-clean`, which reconstructs the guarded base and writes the remainder. |
| View render | Any subset of derived views may be stale after canonical success | `canonical_applied=true`, `views_current=false`; `render --write` replaces all applicable views deterministically. |

Multi-file commands and their errors include intended/changed relative paths
and current revisions in structured output whenever the process survives to
report them. Recovery never treats a syntactically valid but divergent file as
an accepted phase.

`check` reports duplicate active/archive copies, incomplete lifecycle phases,
inactive pins, invalid/duplicate ranks, orphan temporaries, and stale views.
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

### 12.3 Raw front-matter recovery

- `show ID --raw` finds a safe filename prefix and returns exact bytes/path and
  raw revision despite invalid TOML.
- `inspect PATH --raw` handles broken IDs/filenames.
- Raw inspect/repair paths must be regular nonsymlink files under the selected
  store's `tasks/`, `decisions/`, or `archive/tasks/` roots.
- `repair frontmatter PATH --frontmatter-file FILE --if-revision HASH` parses
  and validates replacement TOML, preserves body bytes when the existing
  delimiter split is provable, shows a dry-run diff, writes only with
  `--apply`, and never defaults missing semantic fields.
- If opening/closing delimiters or UTF-8 encoding make the body boundary
  unprovable, repair refuses without `--body-file FILE`. With that explicit
  file, it validates the replacement body as UTF-8/within bounds and writes its
  bytes exactly; it never guesses which raw bytes were body content.

### 12.4 Views

Canonical success is never rolled back because rendering failed. Applicable
views render under the lock after canonical writes. A rendering failure reports
`canonical_applied=true`, `views_current=false`, exit 1. Headers contain
canonical store revision but no wall-clock timestamp. `render --check` detects
missing/stale tracked views; `render --write` repairs them.

## 13. Import contract

Schema: `untaped.orchestration.import/v1`.

```toml
schema = "untaped.orchestration.import/v1"
target_store_id = "sto_..."
expected_store_revision = "sha256:..."
require_empty_items = true

[[records]]
destination = "decisions"
frontmatter_file = "records/decision-01.toml"
body_file = "records/decision-01.md"
source_ref = "git:<commit>:orchestration/DECISIONS.md#sha256:<hash>"
```

The provider-neutral importer reads already-separated metadata/body files. It
contains no legacy Markdown parser. It accepts explicit IDs, timestamps,
destinations, stages/outcomes, evidence, and links; validates normal schema,
policy, graph, filename, collision, and visibility rules; reports exact
destination/hash; defaults to dry-run; and writes with `--apply`.

Fleet manifests set `require_empty_items = true`; apply then also requires the
explicit `store import MANIFEST --if-clean --apply` guard. On the first attempt,
`--if-clean` requires zero item files, current views, and no orphan temporary
files. It is not a journal: the manifest remains external.

An interrupted import is resumed only by supplying that same manifest. Every
existing destination must be its exact byte-identical record and there may be
no item outside the reconstructed pre-import base plus manifest destinations.
`expected_store_revision` always names the original pre-import state; on retry
the tool virtually removes exact manifest destinations already present and
requires the reconstructed revision to match it. The already-written exact
subset is the sole retry exception to `--if-clean`; each remaining record uses
an atomic replacement. Any changed base file, divergent destination, or
unexpected item refuses recovery. Generic `check` cannot declare the external
manifest complete; import dry-run reports the subset, and coverage/count
acceptance gates prove migration completeness. Task import into a decision-only
or public store is forbidden.

Legacy conversion into this manifest is a reviewed one-off preparation step,
not permanent product code.

## 14. Privacy and agent workflow

Public stores are decision-only in v1. Unfinished tasks stay in the private
hub, and changing a populated private store's declaration to public makes
`check` fail. There is no public-task exception or silent private-to-public
relocation.

The packaged skill instructs agents to:

1. Run `brief --format json`.
2. Use returned IDs instead of scanning files.
3. Load only needed bodies with `show`.
4. Pass revisions on every mutation.
5. Never use `--force-current`.
6. Never read/edit generated views.
7. Run `check` after hand edits/recovery.
8. Verify external evidence before recording it.
9. Never place tasks in a public store.
10. Stop readiness/delivery work on incomplete federation.

## 15. Packaging and repository contract

Runtime:

```toml
requires-python = ">=3.14"
dependencies = [
  "cyclopts>=4.16,<5",
  "filelock>=3.29.7,<4",
  "pydantic>=2.13.3,<3",
  "tomli-w>=1.2,<2",
  "untaped>=3.1.0,<4",
]
```

The composition root registers:

```python
ToolSpec(
    command="untaped-orchestration",
    distribution="untaped-orchestration",
    section="orchestration",
    profile_model=OrchestrationSettings,
    skills=(ORCHESTRATION_SKILL,),
)
```

Settings are empty/extra-ignoring in v1; there is no SDK state model. The wheel
includes `py.typed` and the packaged skill, excludes repository `.untaped/`
state, and proves installed-wheel `--version`. Version stdout is exactly
`<distribution-version>\n` with exit 0 and requires no store or profile.
Release templates use reviewed core checker commit
`80bb8411cd0017f3e0cde818656aaf6fd0233368`.

Implementation CI may use the local source until `0.1.0` exists. After an
approved release, a separate self-adoption PR uses the exact released pin.

## 16. Fleet rollout

Rollout is staged, not a simultaneous eleven-repository mutation:

1. Dry-run pilot using temporary copies of the private hub and one public
   content-bearing decision store.
2. Content cohort: core, GitHub, Recipe, Market, and the orchestration tool.
3. Empty-store cohort: AWX, Ansible, Jira, Workspace, and Apple Health.
4. Private hub last, after every child store is accepted.

The full fleet remains in scope, but each repository gets one separately
reviewed adoption PR and no empty store is manufactured before its cohort gate.
Market requires PR #6 content on verified main. Apple Health bases from verified
GitHub HTTPS `FETCH_HEAD`. `pypi-rollout/` is outside this program and is not a
store; relevant results may be linked as evidence only.

The hub registry ultimately contains ten children: the eight pre-existing
workspace-manifest repositories, Apple Health, and this tool. The hub is the
eleventh store.
Apple Health remains intentionally absent from `untaped.yml` because it is not
part of the reconstructed workspace set; explicit federation registration is
independent of that workspace manifest.

Each adoption PR adds canonical store files, local views, concise store/root
instructions, a stable `docs/decisions.md` pointer, exact ignore rules, and a
dedicated read-only CI workflow running released `untaped-orchestration==0.1.0`
with `check --local`, `fmt --check --local`, and `render --check --local`.

The workflow invokes the released distribution directly:

```sh
uvx --python 3.14 --from 'untaped-orchestration==0.1.0' \
  untaped-orchestration check --local
uvx --python 3.14 --from 'untaped-orchestration==0.1.0' \
  untaped-orchestration fmt --check --local
uvx --python 3.14 --from 'untaped-orchestration==0.1.0' \
  untaped-orchestration render --check --local
```

Every dedicated `.github/workflows/orchestration.yml` uses full-commit-SHA
pinned checkout/setup actions, top-level `contents: read`, concurrency
cancellation for superseded branch runs, and no project dependency sync. It
triggers on the store, this workflow, and relevant root instructions/human
pointer files. The exact released package pin is the enforcement boundary; any
pre-commit snippet is convenience only.

Exact `0.1.0` pins are deliberate during v1 rollout for reproducibility. A
compatible range may be considered only after the schema and CLI stabilize.

### 16.1 Repository and migration matrix

| Repository/store | Visibility/capability | Initial decision content | Gate/source |
|---|---|---:|---|
| `untaped-dev` | Private, active tasks | Cross-cutting decisions plus tasks/archive | Adopt last; frozen hub sources include `5837a5258392205ba56b2e22b33fa52d04946caa` |
| `untaped` | Public, decision-only | 6 current core decisions | SDK 3.1.0 commit `80bb8411cd0017f3e0cde818656aaf6fd0233368` |
| `untaped-awx` | Public, decision-only | Empty initial store | Verified main |
| `untaped-ansible` | Public, decision-only | Empty initial store | Verified main |
| `untaped-github` | Public, decision-only | 5 decisions | Frozen source `045fed8bf1c240b8a93bd7a25389cfbe38f0bc8d` |
| `untaped-jira` | Public, decision-only | Empty initial store | Verified main |
| `untaped-market` | Private repository, decision-only policy | 6 decisions | PR #6 on verified main; frozen source `cd792a03cf33625871ed176348a2120d85b21c42` |
| `untaped-recipe` | Public, decision-only | 8 decisions | Frozen source `0fd6f8164329477f4627ba68987ed56ebea4ccb5` |
| `untaped-workspace` | Public, decision-only | Empty initial store | Verified main |
| `untaped-apple-health` | Private repository, decision-only policy | Empty initial store | Verified HTTPS `FETCH_HEAD` |
| `untaped-orchestration` | Public, decision-only | Tool architecture decisions | Post-release self-adoption |

The committed hub migration-input manifest owns full source hashes and exact
snapshot paths. A local-only historical source OID is evidence, not authority;
the coverage review consumes its committed frozen snapshot and records the
final disposition before deleting any legacy content.

### 16.2 Coverage manifest

Before deleting legacy content, a reviewed manifest records source repo/OID,
path/hash, exact heading/block or line range, destination item/path or explicit
disposition, and reviewer decision. Every preamble, table row, footnote, link,
inbox file, and operating paragraph receives a disposition. Whole-file
“migrated” assertions are insufficient.

Expected counts, destinations, references, views, and public-task privacy must
all validate before deletion. Source evidence is retained in import
`source_ref`.

### 16.3 Hub-specific final adoption

The whitelist-based hub adopts last. Its `.gitignore` must explicitly unignore
`.untaped/`, `.untaped/orchestration/**`, `.github/`, `.github/workflows/`, and
`.github/workflows/orchestration.yml`, while ignoring `.lock`, atomic-write
temporaries, and editor artifacts and continuing to track generated views.
Verify the exact tracked/ignored set with `git check-ignore` and
`git diff --name-only`.

Final hub validation runs local checks plus recursive
`check --require-children`; proves ten unique child stores, no public tasks or
recursive view data, complete migration coverage, and no stale legacy-path
references. Only then may legacy orchestration files and frozen inputs be
removed through a separately reviewed change.

## 17. Verification and acceptance

### 17.1 Schema/domain tests

- IDs, filenames, timestamps, dates, bounds, unknown fields, duplicate keys.
- Byte preservation through format and repair.
- Tag/link/evidence ordering and canonicalization.
- Rank operations/rebalance and signed-64-bit boundaries.
- Child-owned parent forest and move/transition placement guards.
- All task transitions, close outcomes, decision supersede/retire, pin updates.
- Decision state-by-command mutation matrix and consolidated-pin ordering.
- Waiting-party blocking and `--waiting-on` queries.
- Curation formulae with injected clocks/timezones.
- Relation locality/cardinality and all individual/combined graph cycles.
- Readiness for every archived dependency outcome.
- Diagnostic codes/paths/order and every output format golden contract.
- Exact SDK Pipe v1 envelope, repeatable columns, and version-only stdout.
- Brief and query limits/truncation.
- Public/private capability enforcement.

### 17.2 Filesystem/federation tests

- Discovery/override; recursive registry; missing/wrong/duplicate children.
- Sibling `..` paths, symlinked roots, rejected internal symlinks, path aliases.
- Registry cycles, lock contention/timeouts, header-only scans.
- Raw lookup with invalid TOML/ID mismatch and lazy empty directories.
- Atomic-write fault injection at file replacement boundaries.
- Interrupted order-preserving rebalance before move/transition final replace.
- Interrupted close duplicate detection/safe repair.
- Interrupted supersede/pin repair by retry or Git restoration.
- Interrupted import exact-subset resume and divergent refusal.
- Delimiter-corruption repair requiring an explicit body file.
- View-render failure after canonical success.

### 17.3 Performance bounds

Use an 11-store/1,000-item synthetic federation with maximum-size headers and
bodies. `brief`, list, next, graph, and curation load bounded headers; show
loads one body; search streams bodies; memory stays bounded by result/snippet
limits; brief never exceeds configured bytes. Record measured thresholds after
baseline measurement rather than inventing them in this specification.

### 17.4 Package acceptance

- Ruff, formatting, strict mypy, pytest, and `uv build --no-sources` pass.
- Architecture test enforces inward imports.
- Installed wheel verifies help/version/init/check/fmt/render.
- Wheel contains `py.typed` and skill; excludes repository store.
- TestPyPI/PyPI release follows explicit approvals and burn-once versioning.
- Fresh `uvx` smoke proves the released package before any fleet pin.

## 18. Decision and field walk

| Locked decision | Owning section |
|---|---|
| Public standalone CLI | 15 |
| Canonical files; no DB/cache | 2–4 |
| TOML metadata; opaque Markdown | 4 |
| No Markdown AST | 1, 4, 12 |
| Hand-edit diagnostics/recovery | 11–12 |
| Git recovery; no journal | 1, 12 |
| Task archive and fixed outcomes | 5 |
| Decision supersession and retirement | 6 |
| Tree/dependency DAG | 7 |
| Recursive explicit federation | 3, 9 |
| Committed local views; agent CLI | 3, 12, 14 |
| Bounded bootstrap | 10 |
| Curation and waiting-party state | 5 |
| Revision guards and locking | 9–12 |
| Offline evidence | 8 |
| Narrowest-repository authority | 16 |
| Private unfinished tasks | 3, 14, 16 |
| Staged full-fleet adoption | 16 |
| Tool first, migrate once | 15–16 |
| Exact SDK prerequisite | 1, 15 |
| Market/Apple special gates | 16 |
| No unapproved external actions | 1, 15–16 |

Within normal typed mutations after initialization, every field has one domain
owner: store policy by explicit admin edit; registry by child commands;
content/priority/waiting state by update; stage/start/revisit trigger by
transition; parent by move; rank by the shared placement protocol; outcome by
close; decision lifecycle by supersede/retire; review by curation; evidence by
evidence commands; views by renderer. Create/import initialization and explicit
repair are controlled exceptions. Valid hand edits are supported escape hatches
whose provenance cannot be proven; `check` validates the resulting invariants
and Git review validates semantic intent. Revisions, readiness, activity, due
state, and completeness are derived only. No generic CLI mutation may
manufacture a lifecycle-owned field.

## 19. Stop conditions and next gate

Stop and replan if the PyPI distribution name becomes unavailable, repository
ownership changes, SDK 3.1.0 is unavailable, the specification is not on
verified main, Market PR #6 is not on verified main for its cohort, Apple
Health cannot establish an HTTPS base, a source OID/hash changes, an adoption
branch exists, coverage lacks a disposition, public stores leak unfinished
tasks, or an external action lacks explicit approval.

After this specification is reviewed and merged, the only next action is a
fresh implementation plan grounded in verified repository main. No
implementation code belongs in this PR.
