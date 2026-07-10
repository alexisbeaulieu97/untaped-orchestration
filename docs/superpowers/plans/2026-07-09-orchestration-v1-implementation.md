# Untaped Orchestration v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and package `untaped-orchestration` 0.1.0 as the typed, file-backed orchestration CLI specified in `docs/superpowers/specs/2026-07-09-orchestration-v1-design.md`, with strict validation, bounded agent reads, safe guarded mutations, deterministic recovery, and release-ready verification.

**Architecture:** Keep domain rules pure and immutable; put orchestration use cases behind narrow ports; implement TOML/filesystem/locking/view adapters in infrastructure; and keep Cyclopts plus SDK wiring in the CLI composition layer. Canonical repository files remain the only source of truth. No Markdown AST, database, cache, Git adapter, provider client, write-ahead journal, or SDK state model is introduced.

**Tech Stack:** Python 3.14, Cyclopts 4.x, Pydantic 2.x, `tomllib`, `tomli-w`, `filelock`, untaped SDK 3.1.x, pytest 9, Ruff, strict mypy, uv/uv_build.

## Global Constraints

- Execute only after PR #2 is merged, from a fresh `codex/` implementation branch cut from verified `origin/main` that contains both this plan and its reviewed design clarifications; the pre-plan base `041c477fbf9eba85eac8bbd4b491ce58e8f6cdd9` is not sufficient. Do not implement on the plan branch.
- Treat `docs/superpowers/specs/2026-07-09-orchestration-v1-design.md` as the behavioral authority. If code pressure exposes an ambiguity or contradiction, stop and amend the design through review instead of silently choosing a new contract.
- Use test-driven development for every behavior slice: add one focused failing test, observe the expected failure, add the smallest implementation, run the focused test, then run the affected suite.
- Import SDK APIs only from `untaped.api`. Domain imports neither application, infrastructure, CLI, nor SDK. Application imports domain and its own ports only. Infrastructure imports domain and application ports but never CLI. CLI is the composition root.
- All canonical models are immutable. Normal mutations construct validated replacements; only explicit repair/import paths accept complete replacement data.
- All filesystem mutation tests use injected ports and fault boundaries. Production code never shells out to Git and never attempts broad recovery.
- Keep commits reviewable at task boundaries. Do not combine unrelated cleanup with a behavior slice.
- Run commands with `uv --cache-dir .uv-cache` when the checkout's default cache is not writable.
- Do not publish, tag, release, create a repository store, or begin fleet adoption during implementation. Those are later approval gates. The implementation PR may add the release workflow, but it must not dispatch it.

---

## File Map

### Repository and package surfaces

- Create `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.pre-commit-config.yaml`, `CHANGELOG.md`, `CONTRIBUTING.md`, and `SECURITY.md` for the Python package and development contract; preserve and verify the existing MIT `LICENSE` without rewriting it.
- Create `.github/workflows/ci.yml` for source CI. Add `.github/workflows/release.yml` only in the final package-readiness slice, from the reviewed core release template pinned to core commit `80bb8411cd0017f3e0cde818656aaf6fd0233368`.
- Update `README.md` from specification-only status to installation, CLI orientation, file-format safety, and recovery guidance.
- Update `AGENTS.md` so it describes the implemented layers, field ownership, test commands, and release/adoption gates instead of the specification-only phase.

### Package entry and settings

- Create `src/untaped_orchestration/__init__.py` with a lazy `app` export.
- Create `src/untaped_orchestration/__main__.py` with the exact `ToolSpec`, console entry point, and packaged skill.
- Create `src/untaped_orchestration/settings.py` with the empty, frozen, extra-ignoring `OrchestrationSettings` model.
- Create `src/untaped_orchestration/errors.py` with typed application failures and their stable exit classification.
- Create `src/untaped_orchestration/py.typed`.

### Domain

- Create `src/untaped_orchestration/domain/ids.py` for typed UUIDv7 IDs and immutable creation slugs.
- Create `src/untaped_orchestration/domain/time.py` for injected-clock timestamp/date conversion.
- Create `src/untaped_orchestration/domain/models.py` for store, registry, task, decision, link, evidence, archive, and import models.
- Create `src/untaped_orchestration/domain/diagnostics.py` for ORC001-ORC009 diagnostics and deterministic ordering.
- Create `src/untaped_orchestration/domain/canonical.py` for canonical metadata/admin projections and semantic source projections.
- Create `src/untaped_orchestration/domain/graph.py` for containment, dependency, supersession, combined precedence, lifecycle derivation, and readiness.
- Create `src/untaped_orchestration/domain/ordering.py` for sparse rank placement, interruption-safe rebalance plans, and global task order.
- Create `src/untaped_orchestration/domain/curation.py` for due-date calculation and curation ordering.
- Create `src/untaped_orchestration/domain/evidence.py` for evidence parsing and canonicalization.
- Re-export the deliberate public domain surface from `src/untaped_orchestration/domain/__init__.py`.

### Application

- Create `src/untaped_orchestration/application/ports.py` with `Clock`, `IdGenerator`, `StoreReader`, `StoreWriter`, `LockManager`, and `ViewRenderer` protocols.
- Create `src/untaped_orchestration/application/results.py` for snapshots, mutation receipts, query pages, completeness, and raw-recovery records.
- Create `src/untaped_orchestration/application/mutations.py` for the shared lock/load/validate/guard/write/render/receipt protocol.
- Create `src/untaped_orchestration/application/validation.py` for whole-store validation and check/fmt inputs.
- Create `src/untaped_orchestration/application/bootstrap.py` for init and caller-stable ID allocation.
- Create `src/untaped_orchestration/application/items.py` for create/update and link/evidence mutations only.
- Create `src/untaped_orchestration/application/tasks.py` for transition, move, review, close, replay, and duplicate repair.
- Create `src/untaped_orchestration/application/decisions.py` for supersede, retire, curation, pin maintenance, and replay.
- Create `src/untaped_orchestration/application/curation.py` as the kind-aware acknowledge/snooze/next facade; `task review` delegates to it.
- Create `src/untaped_orchestration/application/federation.py` for registry changes, recursive resolution, completeness, and ordered multi-store locking.
- Create `src/untaped_orchestration/application/queries.py` for list/show/search/trace/next/curate/history and bounded brief assembly.
- Create `src/untaped_orchestration/application/maintenance.py` for check, fmt, render, raw inspect, front-matter repair, duplicate repair, and import.

### Infrastructure

- Create `src/untaped_orchestration/infrastructure/codec.py` for bounded byte splitting, strict TOML validation, canonical serialization, and exact body preservation.
- Create `src/untaped_orchestration/infrastructure/filesystem.py` for store paths, discovery, safe path resolution, hashing, scans, and atomic replacements.
- Create `src/untaped_orchestration/infrastructure/locking.py` for `filelock`-backed single/multi-store locks.
- Create `src/untaped_orchestration/infrastructure/repository.py` as the filesystem implementation of the reader/writer ports and fault-state-aware write primitives.
- Create `src/untaped_orchestration/infrastructure/runtime.py` for the system clock and UUIDv7 generator adapters.
- Create `src/untaped_orchestration/infrastructure/views.py` for deterministic local-only Markdown views.
- Re-export concrete adapters from `src/untaped_orchestration/infrastructure/__init__.py`.

### CLI and skill

- Create `src/untaped_orchestration/cli/__init__.py` with the Cyclopts root app.
- Create `src/untaped_orchestration/cli/options.py` for shared leaf store/federation/format/limit/debug and mutation-guard option aliases.
- Create `src/untaped_orchestration/cli/output.py` for the orchestration JSON envelope, SDK Pipe v1 records, raw binary recovery, tables, columns, diagnostics, and exits.
- Create `src/untaped_orchestration/cli/context.py` to discover/override a store and compose ports once per invocation.
- Create command modules `id_commands.py`, `read_commands.py`, `task_commands.py`, `decision_commands.py`, `relation_commands.py`, `store_commands.py`, and `maintenance_commands.py`; each translates flags into one application request and contains no domain logic.
- Create `src/untaped_orchestration/skills/untaped-orchestration/SKILL.md` with the eleven agent rules from design section 14.

### Tests

- Create reusable stubs/builders in `tests/conftest.py`, `tests/builders.py`, and `tests/cli_fixtures.py`.
- Create focused unit tests under `tests/unit/domain/`, `tests/unit/application/`, `tests/unit/infrastructure/`, and `tests/unit/cli/` matching the task slices below.
- Create `tests/unit/test_layering.py`, `tests/unit/test_tool_entrypoint.py`, `tests/unit/test_ci_workflow.py`, `tests/unit/test_release_workflow.py`, and `tests/unit/test_packaged_skill.py` for repository contracts.
- Create end-to-end tests under `tests/integration/` for local stores, federation, recovery/fault boundaries, output goldens, and installed-wheel smokes.
- Create `tests/performance/test_bounded_federation.py` for the 11-store/1,000-item acceptance fixture and measured bounds.

---

## Pre-Implementation Gate

Run this read-only gate before creating the implementation branch or package
files. Record the returned repository/PR/main SHAs in the execution log:

```bash
set -euo pipefail
git fetch --prune origin
git rev-parse origin/main
gh repo view alexisbeaulieu97/untaped-orchestration --json nameWithOwner,owner,visibility
gh pr view 2 --json state,mergedAt,mergeCommit
git show origin/main:docs/superpowers/plans/2026-07-09-orchestration-v1-implementation.md
git show origin/main:docs/superpowers/specs/2026-07-09-orchestration-v1-design.md
uv run --no-project --python 3.14 --with 'untaped==3.1.0' python -c 'from importlib.metadata import version; assert version("untaped") == "3.1.0"'
test "$(curl -sS -o /dev/null -w '%{http_code}' https://pypi.org/pypi/untaped-orchestration/json)" = 404
remote_branch_status=0
git ls-remote --exit-code --heads origin refs/heads/codex/orchestration-v1-implementation || remote_branch_status=$?
test "$remote_branch_status" -eq 2
```

The unauthenticated PyPI query must return HTTP 404; HTTP 200 means the
distribution name is no longer available and implementation stops. Confirm
the repository is still public and owned by `alexisbeaulieu97`, PR #2 is
merged, and both reviewed documents are on the fetched `origin/main`. Stop and
replan on any mismatch, unavailable SDK 3.1.0, network result that cannot
establish the PyPI state, or pre-existing intended implementation branch. Only
after this gate passes may branch `codex/orchestration-v1-implementation` and
its worktree be created. Adoption-branch checks remain mandatory again at each
later cohort gate.

---

## Task 1: Scaffold the Package and Lock the Architectural Boundary

**Files:**

- Create: the package entry/settings files plus `src/untaped_orchestration/cli/__init__.py` and the initial complete packaged skill.
- Create: `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.pre-commit-config.yaml`, and `.github/workflows/ci.yml`; verify the existing `LICENSE` remains byte-identical.
- Create: `tests/unit/test_tool_entrypoint.py`
- Create: `tests/unit/test_layering.py`
- Create: `tests/unit/test_ci_workflow.py`
- Create: `tests/unit/test_packaged_skill.py`

- [ ] **Step 1: Bootstrap only the test runner and empty import package**

Create `pyproject.toml` with version `0.1.0`, the exact runtime/dev dependencies,
Python `>=3.14`, the console-script declaration, strict Ruff/mypy/pytest
settings, and `uv_build`. Add only an empty package `__init__.py` and `py.typed`
so `uv sync` can install the editable project; do not create `__main__.py`,
settings, CLI app, or skill yet. Generate the lock with `uv lock`, then run
`uv --cache-dir .uv-cache sync --frozen`.

- [ ] **Step 2: Write failing metadata, entry-point, CI, skill, and layering tests**

Pin these assertions before package code exists:

```python
def test_tool_spec_is_exact() -> None:
    assert SPEC.command == "untaped-orchestration"
    assert SPEC.distribution == "untaped-orchestration"
    assert SPEC.section == "orchestration"
    assert SPEC.profile_model is OrchestrationSettings
    assert SPEC.state_model is None
    (skill,) = SPEC.skills
    assert skill.name == "untaped-orchestration"
    assert skill.source.joinpath("SKILL.md").is_file()


def test_settings_are_empty_and_ignore_foreign_keys() -> None:
    assert OrchestrationSettings.model_fields == {}
    assert OrchestrationSettings.model_validate({"future": "ignored"}) == OrchestrationSettings()


def test_layers_point_inward_only() -> None:
    assert scan_layer_violations() == []
```

The layer scanner must reject domain imports of `application`, `infrastructure`, `cli`, or `untaped`; application imports of `infrastructure` or `cli`; infrastructure imports of `cli`; and any non-CLI `untaped` import.

- [ ] **Step 3: Run the focused tests and confirm the intended red state**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/test_tool_entrypoint.py tests/unit/test_layering.py tests/unit/test_ci_workflow.py tests/unit/test_packaged_skill.py -q --no-cov`

Expected: collection fails because `untaped_orchestration.__main__`, the CLI
root, and the packaged skill do not exist. The test runner itself succeeds in
starting from the bootstrapped project.

- [ ] **Step 4: Add the minimal real CLI root and exact SDK composition root**

Use these package contracts:

```python
# src/untaped_orchestration/settings.py
from pydantic import BaseModel, ConfigDict


class OrchestrationSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
```

```python
# src/untaped_orchestration/cli/__init__.py
from untaped.api import create_app

app = create_app(
    name="orchestration",
    help="Coordinate typed repository tasks and decisions.",
)
```

```python
# src/untaped_orchestration/__main__.py
from importlib.resources import files
from pathlib import Path

from untaped.api import SkillAsset, ToolSpec, run_tool

from untaped_orchestration.cli import app
from untaped_orchestration.settings import OrchestrationSettings

ORCHESTRATION_SKILL = SkillAsset(
    name="untaped-orchestration",
    source=Path(str(files("untaped_orchestration").joinpath("skills", "untaped-orchestration"))),
    description="Use typed repository orchestration stores safely.",
)

SPEC = ToolSpec(
    command="untaped-orchestration",
    distribution="untaped-orchestration",
    section="orchestration",
    profile_model=OrchestrationSettings,
    skills=(ORCHESTRATION_SKILL,),
)


def main() -> object:
    return run_tool(app, SPEC)
```

Add the lazy package-root `app` export. Write the initial packaged skill with all
eleven design-section-14 safety rules; Task 14 may improve examples but may not
defer any rule. Add source CI with full-SHA action pins, checkout credentials
disabled, reviewed uv version/cache, frozen sync, least permissions,
concurrency cancellation, pre-commit, mypy, and pytest. Keep the release
workflow out of this first slice.

- [ ] **Step 5: Make the scaffold and CI contract tests pass**

Run:

```bash
uv --cache-dir .uv-cache sync --frozen
uv --cache-dir .uv-cache run pytest tests/unit/test_tool_entrypoint.py tests/unit/test_layering.py tests/unit/test_ci_workflow.py tests/unit/test_packaged_skill.py -q --no-cov
uv --cache-dir .uv-cache build --no-sources
```

Inspect the wheel and assert it contains `py.typed` and the skill, contains no `.untaped/`, and `untaped-orchestration --version` prints exactly `0.1.0\n` without a store.

- [ ] **Step 6: Commit the scaffold**

```bash
git add .github/workflows/ci.yml .gitignore .pre-commit-config.yaml .python-version pyproject.toml uv.lock src tests/unit/test_layering.py tests/unit/test_tool_entrypoint.py tests/unit/test_ci_workflow.py tests/unit/test_packaged_skill.py
git commit -m "build: scaffold orchestration tool"
```

---

## Task 2: Implement Typed Identity, Time, Diagnostics, and Canonical Models

**Files:**

- Create: `src/untaped_orchestration/domain/ids.py`
- Create: `src/untaped_orchestration/domain/time.py`
- Create: `src/untaped_orchestration/domain/models.py`
- Create: `src/untaped_orchestration/domain/diagnostics.py`
- Create: `src/untaped_orchestration/domain/evidence.py`
- Create: `src/untaped_orchestration/domain/__init__.py`
- Create: `tests/unit/domain/test_ids.py`
- Create: `tests/unit/domain/test_models.py`
- Create: `tests/unit/domain/test_diagnostics.py`
- Create: `tests/unit/domain/test_evidence.py`

- [ ] **Step 1: Add table-driven failing tests for every primitive boundary**

Cover typed UUIDv7 prefixes, lowercase hex, wrong UUID version/variant, safe filename prefixes, immutable 64-character creation slugs, exact millisecond UTC timestamps, IANA timezones, exact calendar dates, title/name/body bounds, lowercase tags/waiting parties, extra fields, evidence schemes, duplicate canonical evidence, and deterministic diagnostic ordering.

Use explicit edge cases, including a 65-character slug, a timestamp without milliseconds, a non-HTTPS URL, zero GitHub number, mixed-case PEP 503 name, and unknown lowercase evidence accepted opaquely.

- [ ] **Step 2: Run the domain tests and confirm missing-model failures**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/domain/test_ids.py tests/unit/domain/test_models.py tests/unit/domain/test_diagnostics.py tests/unit/domain/test_evidence.py -q --no-cov`

Expected: import failures for the new domain modules.

- [ ] **Step 3: Implement typed primitives and frozen Pydantic models**

Use explicit parsers rather than accepting bare strings throughout:

```python
class ItemKind(StrEnum):
    TASK = "task"
    DECISION = "decision"


class TypedId(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @classmethod
    def parse(cls, value: str, *, prefix: Literal["sto", "tsk", "dec"]) -> Self:
        match = ID_RE.fullmatch(value)
        if match is None or match.group("prefix") != prefix:
            raise ValueError(f"expected {prefix}_ UUIDv7 identifier")
        parsed = UUID(hex=match.group("hex"))
        if parsed.version != 7 or parsed.variant != RFC_4122:
            raise ValueError("identifier payload must be an RFC 4122 UUIDv7")
        return cls(value)
```

Model store/admin and item records as discriminated frozen models with `extra="forbid"`. Persist task active/archive shapes separately so lifecycle-owned field sets cannot coexist. Keep decision lifecycle derived: decision records have only the optional paired retirement fields plus links.

Represent diagnostics exactly:

```python
class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: Literal["ORC001", "ORC002", "ORC003", "ORC004", "ORC005", "ORC006", "ORC007", "ORC008", "ORC009"]
    severity: Literal["error", "warning"]
    path: str
    field: str
    line: int | None = None
    column: int | None = None
    byte_offset: int | None = None
    message: str
    hint: str
```

Keep syntax/location extraction out of these models; it belongs to the codec.

- [ ] **Step 4: Run the focused domain suite and strict type checks**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/domain -q --no-cov
uv --cache-dir .uv-cache run mypy src/untaped_orchestration/domain
```

Expected: all pass.

- [ ] **Step 5: Commit the domain vocabulary**

```bash
git add src/untaped_orchestration/domain tests/unit/domain
git commit -m "feat: define orchestration domain models"
```

---

## Task 3: Build the Strict TOML Envelope and Canonical Serializer

**Files:**

- Create: `src/untaped_orchestration/domain/canonical.py`
- Create: `src/untaped_orchestration/infrastructure/codec.py`
- Create: `tests/unit/infrastructure/test_codec.py`
- Create: `tests/unit/infrastructure/test_admin_toml.py`
- Create: `tests/fixtures/codec/` golden byte fixtures

- [ ] **Step 1: Write failing byte-level codec tests**

Test valid task/decision files; BOM, invalid UTF-8, missing/open/extra delimiters, duplicate TOML keys, syntax line/column, front matter over 64 KiB, body over 1 MiB, unknown fields, schema/kind mismatch, and filename/metadata ID mismatch. Prove that canonicalization changes only front matter and preserves body bytes exactly, including CRLF, invalid final newline state, and Markdown containing `+++` away from a delimiter line.

Test canonical fixed key order, omitted optional slots, sorted unique tags, sorted links/evidence, admin key/table order, removal of TOML comments, and serialize-reparse-revalidate before returning bytes.

- [ ] **Step 2: Observe the missing-codec failure**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/infrastructure/test_codec.py tests/unit/infrastructure/test_admin_toml.py -q --no-cov`

- [ ] **Step 3: Implement one bounded splitter and one canonical writer**

The infrastructure boundary returns exact body bytes and never parses Markdown:

```python
@dataclass(frozen=True, slots=True)
class ItemDocument:
    metadata: ActiveTask | ArchivedTask | Decision
    body: bytes
    original: bytes


class ItemCodec:
    def parse(self, raw: bytes, *, relative_path: PurePosixPath) -> ItemDocument: ...
    def canonical_bytes(self, document: ItemDocument) -> bytes: ...
    def parse_replacement_frontmatter(
        self, raw_toml: bytes, *, relative_path: PurePosixPath
    ) -> ActiveTask | ArchivedTask | Decision: ...
```

Search for the first closing line equal to `+++` after the byte-zero opener while enforcing the 64 KiB bound. Decode metadata as UTF-8, parse with `tomllib`, validate through the discriminated Pydantic models, serialize a canonical ordered mapping with `tomli_w.dumps`, reparse it, revalidate it, then join `b"+++\n" + metadata + b"+++\n" + body`.

Admin codecs must validate the complete `store.toml` and `registry.toml` shapes before canonical serialization. Do not expose a generic “dict to TOML” mutation API.

- [ ] **Step 4: Prove byte stability and canonical idempotence**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/infrastructure/test_codec.py tests/unit/infrastructure/test_admin_toml.py -q --no-cov
uv --cache-dir .uv-cache run ruff check src/untaped_orchestration/infrastructure/codec.py tests/unit/infrastructure
```

Add a property-style parametrized test asserting `canonical(canonical(raw)) == canonical(raw)` for every valid model combination.

- [ ] **Step 5: Commit the codec**

```bash
git add src/untaped_orchestration/domain/canonical.py src/untaped_orchestration/infrastructure/codec.py tests/unit/infrastructure tests/fixtures/codec
git commit -m "feat: add canonical TOML item codec"
```

---

## Task 4: Implement Store Ports, Safe Paths, Revisions, Locks, and Atomic Writes

**Files:**

- Create: `src/untaped_orchestration/application/ports.py`
- Create: `src/untaped_orchestration/application/results.py`
- Create: `src/untaped_orchestration/infrastructure/filesystem.py`
- Create: `src/untaped_orchestration/infrastructure/locking.py`
- Create: `src/untaped_orchestration/infrastructure/repository.py`
- Create: `src/untaped_orchestration/infrastructure/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/builders.py`
- Create: `tests/unit/infrastructure/test_filesystem.py`
- Create: `tests/unit/infrastructure/test_repository.py`
- Create: `tests/unit/infrastructure/test_locking.py`

- [ ] **Step 1: Define failing port-contract and filesystem safety tests**

Cover upward discovery and `--store` roots; absent anchor; real sibling `..` registry paths; symlinked repository/store roots; rejected symlinks below canonical/view roots; case-fold aliases; lazy missing item directories; prefix lookup ambiguity; regular-file-only raw paths; ignored `.lock`/temporary/editor artifacts; item, exact-registry-byte, and store SHA-256 revisions; deterministic relative-path ordering; lock timeout; and exact atomic-write event ordering.

Load three simultaneous malformed files and prove `load_local` returns the
valid records plus one ordered per-path codec diagnostic/raw reference for each
bad file rather than failing fast. A malformed `store.toml` may prevent a
validated store model, but it must not erase diagnostics for the remaining
paths.

Use a fault-injecting filesystem stub whose events are `open-temp`, `flush`, `fsync-temp`, `replace`, `fsync-parent`, and `before-ack`. Each boundary raises a dedicated injected exception so later lifecycle tests can stop at every durable phase.

- [ ] **Step 2: Run the focused tests and observe missing adapters**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/infrastructure/test_filesystem.py tests/unit/infrastructure/test_repository.py tests/unit/infrastructure/test_locking.py -q --no-cov`

- [ ] **Step 3: Implement narrow ports and immutable snapshots**

Keep ports independent of concrete paths and lock libraries:

```python
class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self, kind: Literal["store", "task", "decision"]) -> str: ...


class StoreReader(Protocol):
    def discover(self, start: Path, override: Path | None = None) -> StoreLocation: ...
    def load_local(self, location: StoreLocation, *, headers_only: bool) -> StoreSnapshot: ...
    def read_raw(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord: ...


class StoreWriter(Protocol):
    def replace(self, location: StoreLocation, change: FileReplacement) -> None: ...
    def delete(self, location: StoreLocation, change: FileDeletion) -> None: ...


class LockManager(Protocol):
    def acquire(self, locations: Sequence[StoreLocation], *, timeout: float) -> ContextManager[None]: ...


class ViewRenderer(Protocol):
    def expected(self, snapshot: StoreSnapshot) -> Mapping[PurePosixPath, bytes]: ...
```

`StoreSnapshot` carries `records`, `load_diagnostics`, and a filename-first
`raw_index`; whole-store validation can therefore aggregate malformed and
valid paths in one pass. Registry revision hashes exact `registry.toml` bytes.
Store revisions hash sorted `(relative POSIX path, exact file hash)` pairs for
precisely the anchor/admin/instructions/active/archive inputs defined by the
design; views, locks, and temporaries are excluded. `replace` uses a sibling
temporary, file flush/fsync, `os.replace`, then parent-directory fsync.
`delete` fsyncs the parent directory after unlink. Neither method groups files
into a synthetic transaction.

- [ ] **Step 4: Run adapter tests, architecture tests, and mypy**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/infrastructure tests/unit/test_layering.py -q --no-cov
uv --cache-dir .uv-cache run mypy src/untaped_orchestration/application/ports.py src/untaped_orchestration/infrastructure
```

- [ ] **Step 5: Commit the storage boundary**

```bash
git add src/untaped_orchestration/application/ports.py src/untaped_orchestration/application/results.py src/untaped_orchestration/infrastructure tests/conftest.py tests/builders.py tests/unit/infrastructure
git commit -m "feat: add safe filesystem store adapters"
```

---

## Task 5: Resolve Recursive Federation Before Structural Mutations

**Files:**

- Create: `src/untaped_orchestration/application/federation.py`
- Create: `tests/unit/application/test_federation.py`
- Create: `tests/integration/test_federation.py`

- [ ] **Step 1: Write failing resolver, completeness, and lock-order tests**

Cover recursive explicit traversal only; expected child store ID; normalized
real paths; sibling `..` paths; symlinked repository/store roots; duplicate
IDs, paths, and case-fold aliases; self/ancestor cycles; missing, invalid, and
wrong-ID children; ten-second timeout; deterministic real-path lock order; and
`--local` restriction. Prove one invalid child does not hide diagnostics from
other children and every incomplete result names its expected store ID.

- [ ] **Step 2: Run the federation tests and observe the missing service**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/application/test_federation.py tests/integration/test_federation.py -q --no-cov`

- [ ] **Step 3: Implement application-owned snapshots and recursive resolution**

```python
@dataclass(frozen=True, slots=True)
class FederatedSnapshot:
    selected: StoreSnapshot
    stores: tuple[StoreSnapshot, ...]
    completeness: Completeness


class FederationService:
    def load(
        self,
        location: StoreLocation,
        *,
        local: bool,
        headers_only: bool,
    ) -> FederatedSnapshot: ...
```

Keep `FederatedSnapshot` in `application/results.py`, never in domain. Resolve
the registry graph before locking, normalize/dedupe paths and IDs, acquire all
resolved store locks in real-path order, then reread registry anchors under the
locks before accepting the snapshot. Missing stores contribute completeness
records without being lockable. This slice is read-only; child registry
mutations remain in Task 11.

- [ ] **Step 4: Prove the resolver composes with malformed local snapshots**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/application/test_federation.py tests/integration/test_federation.py tests/unit/infrastructure/test_repository.py -q --no-cov
uv --cache-dir .uv-cache run pytest tests/unit/test_layering.py -q --no-cov
```

- [ ] **Step 5: Commit the federation read boundary**

```bash
git add src/untaped_orchestration/application/federation.py src/untaped_orchestration/application/results.py tests/unit/application/test_federation.py tests/integration/test_federation.py
git commit -m "feat: resolve recursive orchestration federation"
```

---

## Task 6: Validate Graphs, Rank Plans, Curation, and Readiness

**Files:**

- Create: `src/untaped_orchestration/domain/graph.py`
- Create: `src/untaped_orchestration/domain/ordering.py`
- Create: `src/untaped_orchestration/domain/curation.py`
- Create: `src/untaped_orchestration/application/validation.py`
- Create: `tests/unit/domain/test_graph.py`
- Create: `tests/unit/domain/test_ordering.py`
- Create: `tests/unit/domain/test_curation.py`
- Create: `tests/unit/application/test_validation.py`

- [ ] **Step 1: Write failing invariant matrices**

Exhaustively cover child-owned containment cycles, dependency cycles, per-kind supersession cycles/cardinality, combined child-before-parent plus prerequisite-before-dependent cycles, relation kind/locality, missing targets under complete/incomplete federation, active parent requirements, every archived dependency outcome, descendant and waiting-party blockers, decision state derivation, public/decision-only task rejection, pin validity, inactive governed-by warnings, and deterministic diagnostic sorting.

For ordering, cover 1000-step append, midpoint, half-first prepend, signed-64-bit boundaries, same-scope placement, neutral rebalance before move, decrease first-to-last, increase last-to-first, interruption after every replacement, unchanged relative order, and global priority/ancestor-rank/own-rank/ID ordering.

For curation, inject UTC instants across timezone date boundaries and cover all stage/state rules, explicit `review_on`, and task-before-decision due sorting.

- [ ] **Step 2: Run tests and observe missing pure services**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/domain/test_graph.py tests/unit/domain/test_ordering.py tests/unit/domain/test_curation.py tests/unit/application/test_validation.py -q --no-cov`

- [ ] **Step 3: Implement pure graph and planning functions**

Keep write ordering as data, not I/O:

```python
@dataclass(frozen=True, slots=True)
class RankReplacement:
    task_id: str
    old_rank: int
    new_rank: int


@dataclass(frozen=True, slots=True)
class PlacementPlan:
    rebalance: tuple[RankReplacement, ...]
    primary: ActiveTask


def plan_placement(
    tasks: Sequence[ActiveTask],
    primary: ActiveTask,
    target: RankScope,
    anchor: PlacementAnchor,
) -> PlacementPlan: ...


@dataclass(frozen=True, slots=True)
class GraphState:
    tasks: tuple[TaskNode, ...]
    decisions: tuple[DecisionNode, ...]
    completeness: GraphCompleteness


def readiness(task_id: str, graph: GraphState) -> Readiness: ...


def validate_snapshot(
    snapshot: FederatedSnapshot,
    *, require_children: bool,
) -> tuple[Diagnostic, ...]: ...
```

`GraphState` and readiness remain domain-owned and know nothing about
application snapshots. The application validator converts a
`FederatedSnapshot` into that domain input, aggregates pure diagnostics, and
does not stop on the first malformed item. The codec supplies syntax
diagnostics, while validation supplies schema/policy/graph/lifecycle
diagnostics.

- [ ] **Step 4: Prove all pure-state boundaries**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/domain tests/unit/application/test_validation.py -q --no-cov
uv --cache-dir .uv-cache run mypy src/untaped_orchestration/domain src/untaped_orchestration/application/validation.py
```

- [ ] **Step 5: Commit invariant services**

```bash
git add src/untaped_orchestration/domain src/untaped_orchestration/application/validation.py tests/unit/domain tests/unit/application/test_validation.py
git commit -m "feat: enforce orchestration graph invariants"
```

---

## Task 7: Initialize, Check, Format, and Render Local Stores

**Files:**

- Create: `src/untaped_orchestration/application/bootstrap.py`
- Create: `src/untaped_orchestration/application/maintenance.py`
- Create: `src/untaped_orchestration/application/mutations.py`
- Create: `src/untaped_orchestration/infrastructure/views.py`
- Create: `tests/unit/application/test_bootstrap.py`
- Create: `tests/unit/application/test_check_fmt_render.py`
- Create: `tests/unit/application/test_mutation_finalization.py`
- Create: `tests/unit/infrastructure/test_views.py`
- Create: `tests/integration/test_local_store.py`

- [ ] **Step 1: Write failing scaffold, fixpoint, and replay tests**

Test private/default, private decision-only, and public-implies-decision-only init. For every scaffold write boundary, assert: no anchor cleanup touches only the operation's validated temporary; matching anchored prefixes resume; byte-identical completion returns `replayed=true`; any divergence refuses; and acknowledgement loss after final fsync replays successfully.

Pin exact scaffold paths, exact `CLAUDE.md == b"@AGENTS.md\n"`, lazy item
directories, store-name line-break rejection, applicable view set, and
byte-for-byte view goldens for every empty/nonempty view using the design's
exact prefix, literal table header/separator/row templates, ordered CR/LF then
backslash then pipe escaping, columns, row ordering, em dash, and final LF. Pin local-only data, render fixpoint,
missing/stale ORC008 diagnostics, `fmt --check` full-byte comparisons,
`fmt --write` guards, invalid metadata refusal, and view failure returning
`canonical_applied=true` without rollback.

Contract-test one shared mutation finalizer with this exact order: acquire the
complete resolved lock set; load and validate current state; validate revision
and semantic guards; build and validate the complete intended state; perform
selected-store canonical writes; render every applicable selected-store view
under the same lock; and return revisions/paths. A renderer failure preserves
canonical success and returns `views_current=false`. Later mutation tests reuse
this contract instead of reimplementing finalization.

- [ ] **Step 2: Run the focused tests and observe missing use cases**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/application/test_bootstrap.py tests/unit/application/test_check_fmt_render.py tests/unit/application/test_mutation_finalization.py tests/unit/infrastructure/test_views.py tests/integration/test_local_store.py -q --no-cov`

- [ ] **Step 3: Implement init as a deterministic expected-file plan**

Build the complete scaffold in memory before writing:

```python
@dataclass(frozen=True, slots=True)
class InitRequest:
    target: Path
    store_id: str
    name: str
    timezone: str
    public: bool = False
    decisions_only: bool = False


class InitializeStore:
    def __init__(self, reader: StoreReader, writer: StoreWriter, locks: LockManager, views: ViewRenderer) -> None: ...
    def execute(self, request: InitRequest) -> MutationReceipt: ...
```

The first durable canonical file is `store.toml`; later writes follow the design
order. Retry compares every existing expected file byte-for-byte and fills only
a valid prefix. Check/fmt/render share the same reader and validator but have
separate use-case methods so `--check` never writes. Implement
`MutationExecutor` in `application/mutations.py` with the tested shared phase
order; it accepts pure callbacks that build intended file changes and leaves
all filesystem and view effects behind ports.

- [ ] **Step 4: Run local-store integration and architecture suites**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/application/test_bootstrap.py tests/unit/application/test_check_fmt_render.py tests/unit/application/test_mutation_finalization.py tests/unit/infrastructure/test_views.py tests/integration/test_local_store.py -q --no-cov
uv --cache-dir .uv-cache run pytest tests/unit/test_layering.py -q --no-cov
```

- [ ] **Step 5: Commit local store lifecycle**

```bash
git add src/untaped_orchestration/application/bootstrap.py src/untaped_orchestration/application/maintenance.py src/untaped_orchestration/application/mutations.py src/untaped_orchestration/infrastructure/views.py tests/unit/application tests/unit/infrastructure/test_views.py tests/integration/test_local_store.py
git commit -m "feat: initialize and validate local stores"
```

---

## Task 8: Implement Item Creation, Clarification, Links, and Evidence

**Files:**

- Create: `src/untaped_orchestration/application/items.py`
- Create: `tests/unit/application/test_item_create.py`
- Create: `tests/unit/application/test_item_update.py`
- Create: `tests/unit/application/test_relations.py`
- Create: `tests/integration/test_item_mutations.py`

- [ ] **Step 1: Write failing creation and field-ownership tests**

Pin caller-supplied task/decision IDs, default inbox/normal/last-rank behavior, immutable filename slug, exact generated timestamp reporting, stale store revision conflicts, existing-ID exact replay before stale-guard rejection, mismatch/archive/inactive conflict, final-fsync acknowledgement replay, and no internally hidden ID generation.

For updates, prove the allowed field sets exactly: task update owns title/body/priority/tags/waiting; decision update owns title/body/tags; generic links own only depends-on/governed-by/follow-up-to; evidence commands own evidence; archive evidence is append-only; inactive-decision link/evidence matrix is enforced; generic code cannot set parent/rank/stage/revisit/outcome/supersedes/review fields. Cross-store relations consume the already-implemented federated snapshot, reject missing/invalid targets, and never mutate a target store. Every successful mutation passes through the shared view finalizer; inject one renderer failure per mutation family.

- [ ] **Step 2: Run focused tests and observe missing item services**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/application/test_item_create.py tests/unit/application/test_item_update.py tests/unit/application/test_relations.py tests/integration/test_item_mutations.py -q --no-cov`

- [ ] **Step 3: Implement request-specific use cases and exact replay comparison**

Use separate immutable request types rather than a generic patch mapping:

```python
@dataclass(frozen=True, slots=True)
class CreateTaskRequest:
    item_id: str
    title: str
    body: bytes
    tags: tuple[str, ...]
    priority: Priority
    waiting_on: tuple[str, ...]
    expected_store_revision: str


@dataclass(frozen=True, slots=True)
class UpdateTaskRequest:
    item_id: str
    expected_revision: str
    title: str | None = None
    body: bytes | None = None
    priority: Priority | None = None
    tags: tuple[str, ...] | None = None
    waiting_on: tuple[str, ...] | None = None
```

Creation compares only caller-owned inputs against an existing record, then returns the existing generated fields and revisions with `replayed=true`. It never equates a stale revision alone with success. Each service supplies a pure intended-change builder to `MutationExecutor`, validates the complete federated intended snapshot before the one selected-store replacement, and lets the shared finalizer render views and form the receipt.

- [ ] **Step 4: Run mutation tests plus whole-store validation**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/application/test_item_create.py tests/unit/application/test_item_update.py tests/unit/application/test_relations.py tests/integration/test_item_mutations.py -q --no-cov
uv --cache-dir .uv-cache run pytest tests/unit/application/test_validation.py -q --no-cov
```

- [ ] **Step 5: Commit ordinary item mutations**

```bash
git add src/untaped_orchestration/application/items.py tests/unit/application/test_item_create.py tests/unit/application/test_item_update.py tests/unit/application/test_relations.py tests/integration/test_item_mutations.py
git commit -m "feat: add guarded item mutations"
```

---

## Task 9: Implement Task Placement, Transitions, Curation, and Close Recovery

**Files:**

- Create: `src/untaped_orchestration/application/tasks.py`
- Create: `src/untaped_orchestration/application/curation.py`
- Create: `tests/unit/application/test_task_transition.py`
- Create: `tests/unit/application/test_task_move.py`
- Create: `tests/unit/application/test_task_close.py`
- Create: `tests/unit/application/test_curation_commands.py`
- Create: `tests/integration/test_task_fault_states.py`

- [ ] **Step 1: Write failing lifecycle and interruption matrices**

Exercise every allowed/rejected transition, backlog `revisit_when`, one-time `started_at`, same-stage backlog trigger replacement, default last placement, explicit first/last/before/after anchors, current-parent assertion including explicit none, primary/store/anchor revision guards, blocked start and delivery under incomplete federation, and task review as an alias of the kind-aware curation acknowledge use case. Test generic acknowledge/snooze routing for both tasks and decisions without CLI kind inspection.

Exercise all four close outcomes and preconditions; archive shape; body/field preservation; archive-before-active-delete ordering; duplicate active/archive detection; exact duplicate repair; ordinary acknowledgement replay after deletion; superseded successor-link-first order; predecessor/successor guards; exact final-state replay; and divergent refusal. Inject a stop after every rebalance, final move/transition primary fsync before stdout, successor-link, archive, delete, and final close fsync. Exact final move/transition state is replayed idempotently; a merely stale or divergent state still conflicts.

- [ ] **Step 2: Run lifecycle tests and confirm they fail before services exist**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/application/test_task_transition.py tests/unit/application/test_task_move.py tests/unit/application/test_task_close.py tests/unit/application/test_curation_commands.py tests/integration/test_task_fault_states.py -q --no-cov`

- [ ] **Step 3: Apply pure plans to one selected store under the consistent federation lock set**

```python
class TaskService:
    def transition(self, request: TransitionTaskRequest) -> MutationReceipt: ...
    def move(self, request: MoveTaskRequest) -> MutationReceipt: ...
    def review(self, request: ReviewTaskRequest) -> MutationReceipt: ...
    def close(self, request: CloseTaskRequest) -> MutationReceipt: ...
    def repair_duplicate(self, request: RepairDuplicateRequest) -> MutationReceipt: ...


class CurationService:
    def next(self, request: CurateNextRequest) -> QueryResult: ...
    def acknowledge(self, request: AcknowledgeRequest) -> MutationReceipt: ...
    def snooze(self, request: SnoozeRequest) -> MutationReceipt: ...
```

Use the shared finalizer to reread the complete resolved federation under its
ordered locks while writing only the selected store. Validate every guard,
compute and validate the complete intended result, execute a rank rebalance
fully before the final primary replacement, render selected-store views, and
expose changed/intended paths plus current revisions. Fresh guards are required
unless the service proves an exact accepted final move, transition, or close
state after acknowledgement loss.

- [ ] **Step 4: Prove every accepted intermediate state remains safe**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/application/test_task_transition.py tests/unit/application/test_task_move.py tests/unit/application/test_task_close.py tests/unit/application/test_curation_commands.py tests/integration/test_task_fault_states.py -q --no-cov
uv --cache-dir .uv-cache run pytest tests/unit/domain/test_ordering.py tests/unit/domain/test_graph.py -q --no-cov
```

- [ ] **Step 5: Commit task lifecycle**

```bash
git add src/untaped_orchestration/application/tasks.py src/untaped_orchestration/application/curation.py tests/unit/application/test_task_transition.py tests/unit/application/test_task_move.py tests/unit/application/test_task_close.py tests/unit/application/test_curation_commands.py tests/integration/test_task_fault_states.py
git commit -m "feat: implement task lifecycle recovery"
```

---

## Task 10: Implement Decision Supersession, Retirement, and Pin Maintenance

**Files:**

- Create: `src/untaped_orchestration/application/decisions.py`
- Create: `tests/unit/application/test_decision_lifecycle.py`
- Create: `tests/integration/test_decision_fault_states.py`

- [ ] **Step 1: Write failing lifecycle, pin-order, and replay tests**

Cover active/superseded/retired derivation, state-by-command mutation matrix, one successor per predecessor, multi-predecessor consolidation, same-store/kind requirements, retired-vs-superseded exclusion, required retirement note, predecessor and store guards, exact successor reuse after interruption, divergent incoming successor refusal, linked-successor-before-pins ordering, retirement-fields-before-pin-removal ordering, earliest-predecessor pin placement, unrelated pin order, duplicate removal, inactive-pin diagnostics, and acknowledgement loss after final fsync.

- [ ] **Step 2: Run focused tests and observe missing decision service**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/application/test_decision_lifecycle.py tests/integration/test_decision_fault_states.py -q --no-cov`

- [ ] **Step 3: Implement deterministic multi-file phase recognition**

```python
class DecisionService:
    def supersede(self, request: SupersedeDecisionRequest) -> MutationReceipt: ...
    def retire(self, request: RetireDecisionRequest) -> MutationReceipt: ...
```

Recognize only exact accepted phases from design section 12.2. The service
validates the complete federated final snapshot before the first selected-store
write, writes the successor or retirement fields first, then replaces
`store.toml` pins, renders views through the shared finalizer, and returns
replay only after proving the requested final content and predecessor set.

- [ ] **Step 4: Run decision, validation, and fault suites**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/application/test_decision_lifecycle.py tests/integration/test_decision_fault_states.py -q --no-cov
uv --cache-dir .uv-cache run pytest tests/unit/application/test_validation.py tests/unit/domain/test_graph.py -q --no-cov
```

- [ ] **Step 5: Commit decision lifecycle**

```bash
git add src/untaped_orchestration/application/decisions.py tests/unit/application/test_decision_lifecycle.py tests/integration/test_decision_fault_states.py
git commit -m "feat: implement decision lifecycle"
```

---

## Task 11: Build Registry Mutations, Recursive Maintenance, and Bounded Queries

**Files:**

- Modify: `src/untaped_orchestration/application/federation.py`
- Modify: `src/untaped_orchestration/application/maintenance.py`
- Create: `src/untaped_orchestration/application/queries.py`
- Create: `tests/unit/application/test_registry_mutations.py`
- Create: `tests/unit/application/test_queries.py`
- Create: `tests/unit/application/test_brief.py`
- Create: `tests/integration/test_recursive_maintenance.py`
- Create: `tests/performance/test_bounded_federation.py`

- [ ] **Step 1: Write failing registry/completeness/query tests**

Cover child add/remove/list with the exact-registry-byte revision guard,
`--force-current` revision-only semantics, complete final validation, shared
view finalization, and no child-store mutation. For add, optimistically resolve
the proposed subtree, lock the normalized union of current/proposed stores in
global order, reread all anchors/registries, and reject a changed or newly
discovered path instead of acquiring it out of order. Reuse the Task 5 resolver
tests for recursive traversal rather than duplicating a second federation
loader.

Extend maintenance with default recursive `check`, `check --local`, warning-only
missing children, `--require-children` promotion, all invalid children in one
report, recursive read-only `fmt --check`, local-only `fmt --write`, and
always-local render. Prove recursive check never renders or writes a child.

For queries, pin deterministic list filters including `--waiting-on`, parsed
show/history-show recursive defaults plus `--local`, selected-store-only raw
show, streaming search, the design's cycle-safe breadth-first
trace shape/direction/order/limit, history, and limits 1..200/default 50. Pin
the behavior matrix: partial reads return `complete=false`; `next` and
`curate next` fail closed unless local; targeted show/raw ignores unrelated
invalid children.

For brief candidate assembly, assert every section and selection cap: ten
pinned decisions, 4096 body bytes per decision, 16384 aggregate decision-body
bytes, ten rows per dynamic section including diagnostics and missing-store
IDs, full counts beside capped rows, UTF-8 code-point truncation, required
revisions, named missing store IDs, inactive rulings, no globally ready label
while incomplete, and deterministic selection/order. The exact final
32768-byte serialized stdout bound belongs to Task 13 because JSON escaping,
Pipe framing, tables, and diagnostics are output-layer behavior.

- [ ] **Step 2: Run query tests and confirm missing registry/query services**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/application/test_registry_mutations.py tests/unit/application/test_queries.py tests/unit/application/test_brief.py tests/integration/test_recursive_maintenance.py -q --no-cov`

- [ ] **Step 3: Add registry writes, recursive maintenance, and bounded loading strategies**

```python
class FederationService:
    def add_child(self, request: AddChildRequest) -> MutationReceipt: ...
    def remove_child(self, request: RemoveChildRequest) -> MutationReceipt: ...


class QueryService:
    def brief(self, request: BriefRequest) -> QueryResult: ...
    def next(self, request: NextRequest) -> QueryResult: ...
```

Header-only scans must not load bodies. `show` loads one body, `search` streams bodies, and brief loads only selected bounded decision bodies. Query results carry `complete`, `truncated`, data, diagnostics, and store/item revisions; output formatting remains outside these services.

- [ ] **Step 4: Measure rather than invent performance thresholds**

Build the 11-store/1,000-item fixture with maximum headers/bodies. Instrument body reads and peak memory. Record the observed local baseline in the test docstring and assert structural bounds: brief reads at most ten bodies; show one body; list/next/curation zero bodies; search holds at most its result/snippet limit. Avoid brittle wall-clock assertions; Task 13 measures final serialized brief bytes.

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/application/test_registry_mutations.py tests/unit/application/test_queries.py tests/unit/application/test_brief.py tests/integration/test_recursive_maintenance.py tests/performance/test_bounded_federation.py -q --no-cov
```

- [ ] **Step 5: Commit registry, maintenance, and queries**

```bash
git add src/untaped_orchestration/application/federation.py src/untaped_orchestration/application/maintenance.py src/untaped_orchestration/application/queries.py tests/unit/application/test_registry_mutations.py tests/unit/application/test_queries.py tests/unit/application/test_brief.py tests/integration/test_recursive_maintenance.py tests/performance/test_bounded_federation.py
git commit -m "feat: add bounded federated queries"
```

---

## Task 12: Implement Import and Explicit Recovery Operations

**Files:**

- Modify: `src/untaped_orchestration/application/maintenance.py`
- Modify: `src/untaped_orchestration/infrastructure/repository.py`
- Create: `tests/unit/application/test_import.py`
- Create: `tests/unit/application/test_repair.py`
- Create: `tests/integration/test_import_fault_states.py`
- Create: `tests/integration/test_raw_recovery.py`

- [ ] **Step 1: Write failing import/resume/repair tests**

Pin the exact external manifest schema, its sole
`expected_store_revision` guard and mismatch behavior, the absence of a second
CLI revision authority, explicit separated metadata/body files,
the design's NFKD/ASCII slug algorithm and derived destination filename,
source-ref canonical evidence insertion, dry-run default, `--apply`, expected
base revision, `require_empty_items`, explicit `--if-clean`,
collision/graph/policy validation, task import refusal in public/decision-only
stores, exact destination/hash reports, exact-subset resume, virtual
reconstructed base revision, unexpected/divergent file refusal,
acknowledgement loss, and shared view finalization.

For recovery, cover filename-prefix raw show with invalid TOML, ambiguous prefix, path-targeted inspect with broken IDs, internal-symlink rejection, invalid UTF-8 exact bytes, delimiter-boundary proof, dry-run front-matter diff, `--body-file` required when unprovable, body-byte exactness, all guards, and semantic duplicate projection repair only.

- [ ] **Step 2: Run focused tests and observe missing maintenance branches**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/application/test_import.py tests/unit/application/test_repair.py tests/integration/test_import_fault_states.py tests/integration/test_raw_recovery.py -q --no-cov`

- [ ] **Step 3: Implement provider-neutral import and narrowly scoped repair**

Import parses the external manifest but delegates every destination record to the normal codec and full-snapshot validation. Before retrying, remove only exact manifest destinations in memory, recompute the original revision, reject any unexpected item, then write remaining records atomically one by one.

Front-matter repair accepts only a replacement TOML file and an item revision. It preserves the existing body only when the codec proves the boundary; otherwise it requires and validates explicit body bytes. Duplicate repair delegates to the exact semantic source projection used by close recovery. Neither command guesses missing fields or renames a file.

- [ ] **Step 4: Run recovery and fault-state suites**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/application/test_import.py tests/unit/application/test_repair.py tests/integration/test_import_fault_states.py tests/integration/test_raw_recovery.py -q --no-cov
uv --cache-dir .uv-cache run pytest tests/integration/test_task_fault_states.py tests/integration/test_decision_fault_states.py -q --no-cov
```

- [ ] **Step 5: Commit import and recovery**

```bash
git add src/untaped_orchestration/application/maintenance.py src/untaped_orchestration/infrastructure/repository.py tests/unit/application/test_import.py tests/unit/application/test_repair.py tests/integration/test_import_fault_states.py tests/integration/test_raw_recovery.py
git commit -m "feat: add import and recovery operations"
```

---

## Task 13: Wire the Complete CLI and Stable Output Contracts

**Files:**

- Create: all CLI files listed in “CLI and skill”; the already-complete Task 1 skill is refined only in Task 14.
- Create: `tests/cli_fixtures.py`
- Create: `tests/unit/cli/test_options.py`
- Create: `tests/unit/cli/test_output.py`
- Create: `tests/unit/cli/test_commands.py`
- Create: `tests/integration/test_cli_contract.py`
- Create: `tests/fixtures/output/` golden output fixtures

- [ ] **Step 1: Write failing command-tree and output-golden tests**

Assert every signature, per-command format availability/rejection, default,
mutual exclusion, required anchor guard, and result shape from design section
10.6. Pin shared leaf options after the full
command path and their rejection before a command; preserve only SDK
profile/verbose/quiet position independence. Reject YAML. Pin command-specific
recovery `--raw` precedence; usage errors; human-only `--force-current`
revision bypass without invariant bypass; the generic curation facade; and
`id new` requiring no store.

Pin JSON envelope key order/schema/command/complete/truncated/data/diagnostics for success and expected domain failure; exact SDK Pipe v1 envelopes/kinds only on row-producing commands; compound-command pipe/raw rejection; pipe ignoring columns; raw default stable-ID first field and dotted columns; table stderr diagnostics; exact binary recovery stdout/stderr matrix including raw-meta key order; base64 JSON recovery; invalid table/pipe recovery exit 2; warning-only check exit 0, `--require-children` promotion, and exits 0/1/2/3/4/5.

Render escape-heavy maximum briefs in both supported formats and assert the
actual encoded stdout including trailing newline never exceeds 32768 bytes,
remains syntactically valid, and sets `truncated=true` when rows or bodies are
removed. Exercise every stage of the design's total reducer, including capped
diagnostics/missing IDs, human-text shortening, and the fixed minimal summary.
This is the output-layer half of Task 11's bounded candidate tests.

- [ ] **Step 2: Run CLI tests and observe missing command composition**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/cli tests/integration/test_cli_contract.py -q --no-cov`

- [ ] **Step 3: Compose ports once and keep command bodies declarative**

Use a single output model:

```python
class OutputEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema: Literal["untaped.orchestration.output/v1"] = "untaped.orchestration.output/v1"
    command: str
    complete: bool
    truncated: bool
    data: object
    diagnostics: tuple[Diagnostic, ...]


def emit_result(result: CommandResult, *, fmt: OutputFormat, columns: tuple[str, ...]) -> None: ...


def exit_for(error: OrchestrationError) -> NoReturn: ...
```

`OutputFormat` here is the orchestration-owned
`Literal["table", "json", "pipe", "raw"]`, not the SDK enum that also permits
YAML. Each Cyclopts leaf repeats the applicable annotated aliases from the
section-10.6 matrix; do not build a second meta-root option parser.

Command modules build typed requests and call one service method. `cli/context.py` resolves SDK settings once, resolves the selected store only for commands that need one, and injects system clock/ID generator/filesystem/locks/views. Expected failures are converted into the requested structured output before raising the stable exit; unexpected traces appear only with `--debug`.

Write binary recovery through `sys.stdout.buffer.write` and one compact metadata JSON line through stderr. Do not route exact bytes through Rich, SDK `emit`, or text decoding.

- [ ] **Step 4: Run all output goldens and verify help/version independently**

Run:

```bash
uv --cache-dir .uv-cache run pytest tests/unit/cli tests/integration/test_cli_contract.py tests/integration/test_raw_recovery.py -q --no-cov
uv --cache-dir .uv-cache run untaped-orchestration --help
uv --cache-dir .uv-cache run untaped-orchestration --version
```

Expected version stdout: exactly `0.1.0` plus one newline, with empty stderr and exit 0.

- [ ] **Step 5: Commit the public CLI**

```bash
git add src/untaped_orchestration/cli tests/cli_fixtures.py tests/unit/cli tests/integration/test_cli_contract.py tests/fixtures/output
git commit -m "feat: expose orchestration CLI contracts"
```

---

## Task 14: Finish Agent Guidance, Documentation, Package Acceptance, and PR Readiness

**Files:**

- Modify: `src/untaped_orchestration/skills/untaped-orchestration/SKILL.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Create: `CHANGELOG.md`
- Create: `CONTRIBUTING.md`
- Create: `SECURITY.md`
- Create: `.github/workflows/release.yml`
- Create: `tests/unit/test_release_workflow.py`
- Create: `docs/file-format.md`
- Create: `docs/recovery.md`
- Create: `docs/cli.md`
- Create: `tests/integration/test_installed_wheel.py`
- Modify: package files only if acceptance exposes a defect.

- [ ] **Step 1: Write failing documentation/skill/package contract tests**

Assert the packaged skill contains and operationalizes all eleven section-14 rules, uses `brief --format json` first, reuses caller-stable IDs, passes guards, forbids agents from `--force-current`, generated-view reads/edits, public tasks, and fail-open readiness. Assert README/docs link the design, file format, recovery procedure, SDK plugin guide, install commands, and explicit post-release self-adoption gate.

Installed-wheel tests must build the current checkout into a fresh temporary
artifact directory, create a fresh virtual environment from that exact wheel,
and verify help, exact version, init, check, fmt check, render check, packaged
skill, `py.typed`, and exclusion of repository `.untaped/` state. The fixture
must never reuse a pre-existing `dist/` artifact.

Copy the canonical release workflow and contract test from core commit
`80bb8411cd0017f3e0cde818656aaf6fd0233368`, changing only distribution,
console, version, and internal dependency configuration. Assert the core
checker SHA, full action SHAs, permissions, environments, burn-once checks,
local-wheel smoke, published-wheel retry/smoke, and GitHub-release target.

- [ ] **Step 2: Run the contract tests and close documentation gaps**

Run: `uv --cache-dir .uv-cache run pytest tests/unit/test_packaged_skill.py tests/unit/test_release_workflow.py tests/integration/test_installed_wheel.py -q --no-cov`

- [ ] **Step 3: Write user and operator documentation from the implemented commands**

Document:

- strict TOML front matter plus opaque Markdown and why no Markdown AST is needed;
- precise ORC diagnostics and the check/fmt workflow after hand edits;
- revision guards, locks, atomic replacement, accepted fault states, targeted Git recovery, and why there is no WAL;
- privacy/capability rules and explicit federation;
- JSON/pipe/raw deviations and byte-mode recovery;
- release and rollout gates: no self-store until `0.1.0` is published and
  approved; then pilot, content cohort, empty-store cohort, and hub last; one
  separately reviewed PR per repository; Market PR #6 on verified main; and
  Apple Health from verified HTTPS `FETCH_HEAD`.

Add a repository-contract assertion that this implementation branch contains
no `.untaped/orchestration` store, orchestration adoption workflow, migration
manifest, cohort branch, or fleet-repository edit.

Update `AGENTS.md` with actual module ownership and test commands. Update `CHANGELOG.md` with the unreleased 0.1.0 feature surface; do not claim publication.

- [ ] **Step 4: Run full verification from a clean source tree**

Run:

```bash
uv --cache-dir .uv-cache run ruff check .
uv --cache-dir .uv-cache run ruff format --check .
uv --cache-dir .uv-cache run mypy
uv --cache-dir .uv-cache build --no-sources
uv --cache-dir .uv-cache run pytest
uv --cache-dir .uv-cache run pre-commit run --all-files --show-diff-on-failure
git diff --check
git status --short
```

Then install that just-built wheel into a fresh Python 3.14 environment and
repeat the console smoke. Review coverage misses as missing behavior rather
than lowering the gate. Confirm no `TODO`, `TBD`, accidental stub
`NotImplementedError`, journal/VCS/provider adapter, repository store, or
untracked generated artifact remains.

- [ ] **Step 5: Commit final documentation and acceptance coverage**

```bash
git add .github/workflows/release.yml AGENTS.md CHANGELOG.md CONTRIBUTING.md README.md SECURITY.md docs src/untaped_orchestration/skills tests/unit/test_release_workflow.py tests/integration/test_installed_wheel.py
git commit -m "docs: complete orchestration v1 guidance"
```

- [ ] **Step 6: Request an independent Superpowers code review, then stop at the external-action gate**

Compare the implementation branch against verified `origin/main`, provide the
design and this plan to the reviewer, address only verified findings, and rerun
the full verification block. Stop with the clean reviewed local branch and
request explicit approval before any push or draft-PR creation. Later approvals
are separately required to mark a PR ready, merge, dispatch the release
workflow, or create the post-release self-adoption store.

---

## Specification Coverage Audit

Before declaring the local implementation branch ready for external review, map every design acceptance bullet to at least one test node:

| Design contract | Primary tasks/tests |
|---|---|
| Canonical TOML, opaque body, bounds | Task 3 codec goldens |
| IDs, timestamps, fields, evidence | Task 2 domain matrices |
| Revisions, paths, locks, atomicity | Task 4 adapter/fault tests |
| Recursive federation and completeness | Task 5 resolver integration |
| Graphs, rank, curation, readiness | Task 6 pure invariant matrices |
| Init/check/fmt/render and view fixpoint | Task 7 local-store integration |
| Create replay and field ownership | Task 8 guarded mutation tests |
| Task lifecycle and crash states | Task 9 fault-state integration |
| Decision lifecycle and pin phases | Task 10 fault-state integration |
| Registry, recursive maintenance, queries, brief | Task 11 federation/performance tests |
| Import and raw/explicit recovery | Task 12 recovery integration |
| CLI tree, envelopes, Pipe v1, exits | Task 13 output goldens |
| Skill, wheel, release readiness | Tasks 1 and 14 package acceptance |

The implementation phase is complete only when this table has no unproved row, the full verification block passes from a clean checkout, and independent review has no unresolved actionable finding. Release, self-adoption, content cohorts, empty-store cohorts, and private-hub migration remain later phases with their own approval gates.

_Independently reviewed (4 reviewers): 28 findings — 28 incorporated, 0 surfaced above, 0 dismissed. Ask to see the full review._
