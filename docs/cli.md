# CLI and output contracts

Run `untaped-orchestration COMMAND --help` for the exact leaf options. SDK root
options remain `--profile`, `--verbose`/`-v`, and `--quiet`/`-q`; orchestration
leaf options such as `--store`, `--local`, `--format`, `--columns`, `--limit`,
and `--debug` follow the complete command path.

## Safe agent flow

1. Run `brief --format json`.
2. Use returned IDs and revisions; do not scan canonical files or generated
   views.
3. Allocate one ID before init/create and reuse it through every retry.
4. Load only a needed body with `show ID`.
5. Pass every required revision guard. Agents never use `--force-current`.
6. Stop readiness and delivery work if federation is incomplete.

`id new`, `brief`, `list`, `show`, `inspect`, `search`, `trace`, `next`,
`curate next`, and `history` provide bounded reads. Typed mutations cover init,
task and decision lifecycles, relations, evidence, curation, child registry,
provider-neutral import, validation, format, render, and targeted repair.

`brief` enforces the selected store's configured `brief.max_total_bytes` on
the final encoded JSON or table output, including its trailing LF. `curate
next` and recursive `fmt --check` preserve completeness, truncation, and
ordered diagnostics in the top-level output contract.

Federation is explicit in `registry.toml`. Reads recurse by default.
`--local` is true-local: it selects only the chosen store and does not resolve
or report registered children. Writes modify only the selected store, even
when recursive validation locks and checks all resolved participants. Partial
reads may return `complete=false`. `next`, readiness/delivery, and structural
changes fail closed when required federation is incomplete. Public stores are
decision-only; never place tasks in them.

## Output

Table is for humans. Raw row projection starts with stable ID and supports
repeatable dotted `--columns`. JSON deliberately uses one orchestration
envelope with `schema`, `command`, `complete`, `truncated`, `data`, and
`diagnostics`; YAML is not a v1 format.

Pipe is SDK Pipe v1 NDJSON. Stable data records come first, followed by real
canonical `orchestration.diagnostic` records, followed by exactly one
`orchestration.status` trailer:

```json
{"untaped":"1","kind":"orchestration.status","record":{"complete":true,"truncated":false}}
```

Completeness and truncation are stream status, never synthetic ORC diagnostics.
Malformed-file byte-mode recovery is a separate command-specific `--raw`
contract; see [recovery](recovery.md#broken-front-matter-and-byte-mode-recovery).

Exit codes are 0 success, 1 invalid canonical data/stale views, 2 usage, 3
required federation incomplete, 4 lock/revision conflict, and 5 I/O or
unexpected internal failure. Partial-tolerant reads can exit 0 with
`complete=false`; never treat that as readiness.

Typed expected failures retain their exact public diagnostics and mapped exit
code. Unexpected exceptions use a generic ORC002 and do not expose their
message unless the operator explicitly requests `--debug`. A canonical writer
failure also emits a failure receipt in JSON/table data: `intended_paths` is
complete, while `changed_paths` contains only writer calls that returned
successfully. A typed view-finalization failure after canonical success emits
the durable post-canonical revisions and only the canonical and view paths
whose writer calls returned successfully; it never guesses the failed path.
Any such failure reports `views_current=false`. A mutation that returns a
canonical-success/stale-view receipt exits 1, unless a typed diagnostic maps to
a higher-precedence exit code.

## Release and rollout gates

Release availability is determined by package indexes and GitHub releases, not
this document. The 0.1.0 release workflow is approval-gated: it verifies the
version and unused target before publication, validates internal dependency
availability, smokes the exact local wheel, publishes with trusted publishing,
retries a fresh published-wheel smoke, and creates a GitHub release only after
the production smoke. A failed post-upload version may be burned and requires a
patch bump.

Self-adoption is a separate gate: version `0.1.0` is released, its fresh
released-package smoke passed, and self-adoption approved state is represented
by this repository's public decision-only store. The dedicated workflow uses
the exact released pin; later repository adoptions remain separately gated.

Fleet adoption order is:

1. Dry-run pilot on temporary copies of the private hub and one public store.
2. The content cohort: core, GitHub, Recipe, Market, and orchestration.
3. The empty-store cohort: AWX, Ansible, Jira, Workspace, and Apple Health.
4. The private hub last, after every child is accepted.

Each repository receives one separately reviewed adoption PR; no cohort is a
single cross-repository change. Market requires Market PR #6 on verified main.
Apple Health must base from verified GitHub HTTPS `FETCH_HEAD`. The
`pypi-rollout/` directory is outside this program and can be evidence only.
