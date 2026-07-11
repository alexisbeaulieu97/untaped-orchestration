from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import overload

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


class ItemMutationConflict(ValueError):
    pass


class RevisionConflict(ItemMutationConflict):
    pass


class CreateConflict(ItemMutationConflict):
    pass


class ItemStateConflict(ItemMutationConflict):
    pass


class RelationConflict(ItemMutationConflict):
    pass


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
    expected_revision: Revision
    title: str | None = None
    body: bytes | None = None
    priority: TaskPriority | None = None
    tags: tuple[Slug, ...] | None = None
    waiting_on: tuple[Slug, ...] | None = None


@dataclass(frozen=True, slots=True)
class UpdateDecisionRequest:
    item_id: DecisionId
    expected_revision: Revision
    title: str | None = None
    body: bytes | None = None
    tags: tuple[Slug, ...] | None = None


@dataclass(frozen=True, slots=True)
class LinkRequest:
    source_id: TaskId | DecisionId
    relation: LinkRelation
    target_store_id: StoreId
    target_id: TaskId | DecisionId
    expected_revision: Revision


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    item_id: TaskId | DecisionId
    relation: EvidenceRelation
    reference: EvidenceReference
    expected_revision: Revision


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
    if isinstance(metadata, ActiveTask):
        return ActiveTask.model_validate(values)
    if isinstance(metadata, ArchivedTask):
        return ArchivedTask.model_validate(values)
    return Decision.model_validate(values)


def execute_mutation(
    executor: MutationExecutor,
    scope: MutationExecutionScope,
    guard: Callable[[FederatedSnapshot], None],
    build: Callable[[FederatedSnapshot], IntendedMutation],
    *,
    validator: SnapshotValidator | None = None,
) -> MutationReceipt:
    return executor.execute(
        locations=scope.locations,
        selected=scope.selected,
        load=scope.load,
        guard=guard,
        build=build,
        validator=validator,
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
