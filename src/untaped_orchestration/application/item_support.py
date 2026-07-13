from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import overload

from pydantic import ValidationError

from untaped_orchestration.application.mutations import (
    IntendedMutation,
    MutationExecutor,
    SnapshotValidator,
)
from untaped_orchestration.application.ports import CanonicalFormatter, FileReplacement
from untaped_orchestration.application.results import (
    FederatedSnapshot,
    LoadedRecord,
    MutationReceipt,
    StoreLocation,
)
from untaped_orchestration.domain.canonical import CanonicalItem
from untaped_orchestration.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticError,
    expected_diagnostic,
)
from untaped_orchestration.domain.evidence import EvidenceReference, EvidenceRelation
from untaped_orchestration.domain.ids import DecisionId, Slug, StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    LinkRelation,
    Revision,
    TaskPriority,
)


class ItemMutationConflict(DiagnosticError):
    code: DiagnosticCode = "ORC006"

    def __init__(
        self,
        message: str,
        diagnostics: tuple[Diagnostic, ...] | None = None,
    ) -> None:
        super().__init__(diagnostics or expected_diagnostic(self.code, message))


class RevisionConflict(ItemMutationConflict):
    code: DiagnosticCode = "ORC007"


class CreateConflict(ItemMutationConflict):
    code: DiagnosticCode = "ORC003"


class ItemStateConflict(ItemMutationConflict):
    pass


class RelationConflict(ItemMutationConflict):
    code: DiagnosticCode = "ORC004"


def validate_force_current(
    force_current: bool,
    revisions: tuple[Revision | None, ...],
) -> None:
    if force_current and any(value is not None for value in revisions):
        raise RevisionConflict("--force-current is mutually exclusive with revision guards")
    if not force_current and any(value is None for value in revisions):
        raise RevisionConflict("revision guards are required without --force-current")


def guard_revision(
    current: Revision,
    expected: Revision | None,
    *,
    force_current: bool,
    message: str,
) -> None:
    validate_force_current(force_current, (expected,))
    if not force_current and current != expected:
        raise RevisionConflict(message)


@dataclass(frozen=True, slots=True)
class MutationExecutionScope:
    locations: tuple[StoreLocation, ...]
    selected: StoreLocation
    load: Callable[[], FederatedSnapshot]


@dataclass(frozen=True, slots=True)
class MutationScope:
    recursive: MutationExecutionScope
    selected_local: MutationExecutionScope


@dataclass(frozen=True, slots=True)
class CreateTaskRequest:
    item_id: TaskId
    title: str
    body: bytes
    tags: tuple[Slug, ...]
    priority: TaskPriority
    waiting_on: tuple[Slug, ...]
    expected_store_revision: Revision


@dataclass(frozen=True, slots=True)
class CreateDecisionRequest:
    item_id: DecisionId
    title: str
    body: bytes
    tags: tuple[Slug, ...]
    expected_store_revision: Revision


@dataclass(frozen=True, slots=True)
class UpdateTaskRequest:
    item_id: TaskId
    expected_revision: Revision | None
    force_current: bool = False
    title: str | None = None
    body: bytes | None = None
    priority: TaskPriority | None = None
    tags: tuple[Slug, ...] | None = None
    waiting_on: tuple[Slug, ...] | None = None


@dataclass(frozen=True, slots=True)
class UpdateDecisionRequest:
    item_id: DecisionId
    expected_revision: Revision | None
    force_current: bool = False
    title: str | None = None
    body: bytes | None = None
    tags: tuple[Slug, ...] | None = None


@dataclass(frozen=True, slots=True)
class LinkRequest:
    source_id: TaskId | DecisionId
    relation: LinkRelation
    target_store_id: StoreId
    target_id: TaskId | DecisionId
    expected_revision: Revision | None
    force_current: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    item_id: TaskId | DecisionId
    relation: EvidenceRelation
    reference: EvidenceReference
    expected_revision: Revision | None
    force_current: bool = False


@dataclass(frozen=True, slots=True)
class ItemMutationResult:
    record: LoadedRecord
    receipt: MutationReceipt


@dataclass(slots=True)
class PlannedRecord:
    path: PurePosixPath | None = None
    metadata: CanonicalItem | None = None
    body: bytes | None = None


def selected_store_id(snapshot: FederatedSnapshot) -> StoreId:
    config = snapshot.selected.store
    if config is None:
        raise ItemStateConflict("selected store configuration is unavailable")
    return config.id


def selected_record(
    snapshot: FederatedSnapshot,
    item_id: TaskId | DecisionId,
) -> LoadedRecord | None:
    matches = tuple(record for record in snapshot.selected.records if record.metadata.id == item_id)
    if len(matches) > 1:
        raise ItemStateConflict("item identity is ambiguous in the selected store")
    return matches[0] if matches else None


def record_result(planned: PlannedRecord, receipt: MutationReceipt) -> ItemMutationResult:
    if planned.path is None or planned.metadata is None or planned.body is None:
        raise AssertionError("mutation completed without a planned item result")
    revision = next(
        (value.revision for value in receipt.item_revisions if value.path == planned.path),
        None,
    )
    if revision is None:
        raise AssertionError("mutation receipt omitted the resulting item revision")
    return ItemMutationResult(
        LoadedRecord(planned.path, revision, planned.metadata, planned.body),
        receipt,
    )


def replacement(
    formatter: CanonicalFormatter,
    path: PurePosixPath,
    metadata: CanonicalItem,
    body: bytes,
) -> IntendedMutation:
    return IntendedMutation(
        replacements=(FileReplacement(path, formatter.item_bytes(metadata, body)),)
    )


@overload
def validated_copy(metadata: ActiveTask, updates: dict[str, object]) -> ActiveTask: ...


@overload
def validated_copy(metadata: ArchivedTask, updates: dict[str, object]) -> ArchivedTask: ...


@overload
def validated_copy(metadata: Decision, updates: dict[str, object]) -> Decision: ...


@overload
def validated_copy(metadata: CanonicalItem, updates: dict[str, object]) -> CanonicalItem: ...


def validated_copy(
    metadata: CanonicalItem,
    updates: dict[str, object],
) -> CanonicalItem:
    values = metadata.model_dump(by_alias=True)
    values.update(updates)
    try:
        if isinstance(metadata, ActiveTask):
            return ActiveTask.model_validate(values)
        if isinstance(metadata, ArchivedTask):
            return ArchivedTask.model_validate(values)
        return Decision.model_validate(values)
    except ValidationError as error:
        message = error.errors()[0]["msg"]
        raise ItemStateConflict(f"invalid item lifecycle state: {message}") from error


def execute_mutation(
    executor: MutationExecutor,
    scope: MutationExecutionScope,
    guard: Callable[[FederatedSnapshot], None],
    build: Callable[[FederatedSnapshot], IntendedMutation],
    *,
    validator: SnapshotValidator | None = None,
    dry_run: bool = False,
) -> MutationReceipt:
    return executor.execute(
        locations=scope.locations,
        selected=scope.selected,
        load=scope.load,
        guard=guard,
        build=build,
        validator=validator,
        dry_run=dry_run,
    )


def decision_inactive(snapshot: FederatedSnapshot, item_id: DecisionId) -> bool:
    record = selected_record(snapshot, item_id)
    if record is None or not isinstance(record.metadata, Decision):
        return False
    if record.metadata.retired_at is not None:
        return True
    selected_id = selected_store_id(snapshot)
    return any(
        link.relation is LinkRelation.SUPERSEDES
        and link.target_store_id == selected_id
        and link.target == item_id
        for store in snapshot.stores
        for candidate in store.records
        if isinstance(candidate.metadata, Decision)
        for link in candidate.metadata.links
    )
