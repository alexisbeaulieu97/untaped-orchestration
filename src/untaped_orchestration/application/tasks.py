from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from untaped_orchestration.application.curation import AcknowledgeRequest, CurationService
from untaped_orchestration.application.item_support import (
    ItemMutationResult,
    ItemStateConflict,
    MutationScope,
    PlannedRecord,
    RevisionConflict,
    execute_mutation,
    record_result,
    selected_store_id,
    validated_copy,
)
from untaped_orchestration.application.mutations import IntendedMutation, MutationExecutor
from untaped_orchestration.application.ports import (
    CanonicalFormatter,
    Clock,
    FileDeletion,
    FileReplacement,
)
from untaped_orchestration.application.results import (
    FederatedSnapshot,
    ItemRevision,
    LoadedRecord,
    MutationReceipt,
)
from untaped_orchestration.application.task_recovery import (
    accepted_close_base_matches,
    active_source,
    canonical_revision,
    semantic_source_matches,
    successor_source,
    supersedes_link,
)
from untaped_orchestration.application.validation import _graph_state, validate_snapshot
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.graph import TaskRef, readiness
from untaped_orchestration.domain.ids import TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Revision,
    TaskOutcome,
    TaskStage,
)
from untaped_orchestration.domain.ordering import (
    PlacementAnchor,
    PlacementAnchorKind,
    RankScope,
    plan_placement,
)
from untaped_orchestration.domain.time import UtcTimestamp


class TaskLifecycleConflict(ItemStateConflict):
    pass


@dataclass(frozen=True, slots=True)
class TransitionTaskRequest:
    item_id: TaskId
    to_stage: TaskStage
    expected_parent: TaskId | None
    expected_revision: Revision
    expected_store_revision: Revision
    placement: PlacementAnchor
    revisit_when: str | None = None
    expected_anchor_revision: Revision | None = None


@dataclass(frozen=True, slots=True)
class MoveTaskRequest:
    item_id: TaskId
    parent: TaskId | None
    expected_parent: TaskId | None
    expected_revision: Revision
    expected_store_revision: Revision
    placement: PlacementAnchor
    expected_anchor_revision: Revision | None = None


@dataclass(frozen=True, slots=True)
class CloseTaskRequest:
    item_id: TaskId
    outcome: TaskOutcome
    note: str
    expected_revision: Revision
    expected_store_revision: Revision
    successor_id: TaskId | None = None
    expected_successor_revision: Revision | None = None


@dataclass(frozen=True, slots=True)
class RepairDuplicateRequest:
    item_id: TaskId
    expected_active_revision: Revision
    expected_archive_revision: Revision
    apply: bool = False


ReviewTaskRequest = AcknowledgeRequest


def _task_records(snapshot: FederatedSnapshot, item_id: TaskId) -> tuple[LoadedRecord, ...]:
    return tuple(
        record
        for record in snapshot.selected.records
        if record.metadata.id == item_id and isinstance(record.metadata, (ActiveTask, ArchivedTask))
    )


def _active(snapshot: FederatedSnapshot, item_id: TaskId) -> LoadedRecord | None:
    values = tuple(
        record
        for record in _task_records(snapshot, item_id)
        if isinstance(record.metadata, ActiveTask)
    )
    if len(values) > 1:
        raise TaskLifecycleConflict("active task identity is ambiguous")
    return values[0] if values else None


def _archive(snapshot: FederatedSnapshot, item_id: TaskId) -> LoadedRecord | None:
    values = tuple(
        record
        for record in _task_records(snapshot, item_id)
        if isinstance(record.metadata, ArchivedTask)
    )
    if len(values) > 1:
        raise TaskLifecycleConflict("archived task identity is ambiguous")
    return values[0] if values else None


def _archive_metadata(
    task: ActiveTask, outcome: TaskOutcome, note: str, now: UtcTimestamp
) -> ArchivedTask:
    values = task.model_dump(by_alias=True)
    stage = values.pop("stage")
    values.update(closed_from=stage, outcome=outcome, closed_at=now, close_note=note)
    return ArchivedTask.model_validate(values)


def _placement_matches(
    snapshot: FederatedSnapshot,
    task: ActiveTask,
    scope: RankScope,
    placement: PlacementAnchor,
) -> bool:
    if task.parent != scope.parent or task.stage is not scope.stage:
        return False
    members = sorted(
        (
            record.metadata
            for record in snapshot.selected.records
            if isinstance(record.metadata, ActiveTask)
            and record.metadata.parent == scope.parent
            and record.metadata.stage is scope.stage
        ),
        key=lambda value: (value.rank, value.id.root),
    )
    index = next((index for index, value in enumerate(members) if value.id == task.id), -1)
    if index < 0:
        return False
    if placement.kind is PlacementAnchorKind.FIRST:
        return index == 0
    if placement.kind is PlacementAnchorKind.LAST:
        return index == len(members) - 1
    assert placement.task_id is not None
    anchor = next((i for i, value in enumerate(members) if value.id == placement.task_id), -1)
    return (
        index + 1 == anchor if placement.kind is PlacementAnchorKind.BEFORE else index == anchor + 1
    )


def _guard_anchor(
    snapshot: FederatedSnapshot,
    placement: PlacementAnchor,
    expected_revision: Revision | None,
    scope: RankScope,
) -> None:
    relative = placement.kind in {PlacementAnchorKind.BEFORE, PlacementAnchorKind.AFTER}
    if not relative:
        if expected_revision is not None:
            raise TaskLifecycleConflict("first/last placement rejects an anchor revision")
        return
    if expected_revision is None or placement.task_id is None:
        raise TaskLifecycleConflict("relative placement requires an anchor revision")
    anchor = _active(snapshot, placement.task_id)
    anchor_metadata = anchor.metadata if anchor is not None else None
    if (
        anchor is None
        or anchor.revision != expected_revision
        or not isinstance(anchor_metadata, ActiveTask)
        or anchor_metadata.parent != scope.parent
        or anchor_metadata.stage is not scope.stage
    ):
        raise TaskLifecycleConflict("placement anchor is stale or outside the target scope")


def _placement_replacements(
    snapshot: FederatedSnapshot,
    primary_record: LoadedRecord,
    primary: ActiveTask,
    scope: RankScope,
    placement: PlacementAnchor,
    formatter: CanonicalFormatter,
) -> tuple[tuple[FileReplacement, ...], ActiveTask]:
    tasks = tuple(
        record.metadata
        for record in snapshot.selected.records
        if isinstance(record.metadata, ActiveTask)
    )
    by_id = {
        record.metadata.id: record
        for record in snapshot.selected.records
        if isinstance(record.metadata, ActiveTask)
    }
    plan = plan_placement(tasks, primary, scope, placement)
    changes: list[FileReplacement] = []
    for change in plan.rebalance:
        record = by_id[change.task_id]
        assert isinstance(record.metadata, ActiveTask) and record.body is not None
        neutral = validated_copy(record.metadata, {"rank": change.new_rank})
        changes.append(FileReplacement(record.path, formatter.item_bytes(neutral, record.body)))
    assert primary_record.body is not None
    return tuple(changes), plan.primary


_ALLOWED_TRANSITIONS = {
    (TaskStage.INBOX, TaskStage.BACKLOG),
    (TaskStage.INBOX, TaskStage.PLANNED),
    (TaskStage.BACKLOG, TaskStage.PLANNED),
    (TaskStage.PLANNED, TaskStage.BACKLOG),
    (TaskStage.PLANNED, TaskStage.IN_PROGRESS),
    (TaskStage.IN_PROGRESS, TaskStage.PLANNED),
    (TaskStage.BACKLOG, TaskStage.BACKLOG),
}


def _transition_target_matches(
    snapshot: FederatedSnapshot,
    task: ActiveTask,
    request: TransitionTaskRequest,
) -> bool:
    return (
        task.stage is request.to_stage
        and task.parent == request.expected_parent
        and (
            task.revisit_when == request.revisit_when
            if request.to_stage is TaskStage.BACKLOG
            else task.revisit_when is None
        )
        and _placement_matches(
            snapshot,
            task,
            RankScope(request.expected_parent, request.to_stage),
            request.placement,
        )
    )


def _validate_transition_change(
    snapshot: FederatedSnapshot,
    task: ActiveTask,
    request: TransitionTaskRequest,
) -> None:
    if (task.stage, request.to_stage) not in _ALLOWED_TRANSITIONS:
        raise TaskLifecycleConflict("task stage transition is not allowed")
    if request.to_stage is TaskStage.BACKLOG:
        if request.revisit_when is None or not request.revisit_when.strip():
            raise TaskLifecycleConflict("backlog transition requires revisit_when")
    elif request.revisit_when is not None:
        raise TaskLifecycleConflict("revisit_when is allowed only for backlog")
    if request.to_stage is TaskStage.IN_PROGRESS:
        result = readiness(
            TaskRef(selected_store_id(snapshot), request.item_id),
            _graph_state(snapshot),
        )
        if not result.ready:
            raise TaskLifecycleConflict("blocked task cannot enter in-progress")


def _guard_transition(
    snapshot: FederatedSnapshot,
    request: TransitionTaskRequest,
    planned: PlannedRecord,
) -> bool:
    record = _active(snapshot, request.item_id)
    if record is None or not isinstance(record.metadata, ActiveTask) or record.body is None:
        raise TaskLifecycleConflict("transition requires an active task")
    if record.revision != request.expected_revision:
        raise RevisionConflict("task transition revision is stale")
    if snapshot.selected.store_revision != request.expected_store_revision:
        raise RevisionConflict("task transition store revision is stale")
    if record.metadata.parent != request.expected_parent:
        raise TaskLifecycleConflict("current parent assertion failed")
    target = RankScope(request.expected_parent, request.to_stage)
    _guard_anchor(snapshot, request.placement, request.expected_anchor_revision, target)
    if request.to_stage is not TaskStage.INBOX and _transition_target_matches(
        snapshot, record.metadata, request
    ):
        planned.path, planned.metadata, planned.body = (
            record.path,
            record.metadata,
            record.body,
        )
        return True
    _validate_transition_change(snapshot, record.metadata, request)
    return False


def _guard_move(
    snapshot: FederatedSnapshot,
    request: MoveTaskRequest,
    planned: PlannedRecord,
) -> bool:
    record = _active(snapshot, request.item_id)
    if record is None or not isinstance(record.metadata, ActiveTask) or record.body is None:
        raise TaskLifecycleConflict("move requires an active task")
    if record.revision != request.expected_revision:
        raise RevisionConflict("task move revision is stale")
    if snapshot.selected.store_revision != request.expected_store_revision:
        raise RevisionConflict("task move store revision is stale")
    if record.metadata.parent != request.expected_parent:
        raise TaskLifecycleConflict("current parent assertion failed")
    if request.parent == request.item_id:
        raise TaskLifecycleConflict("task cannot parent itself")
    if request.parent is not None and _active(snapshot, request.parent) is None:
        raise TaskLifecycleConflict("target parent must be an active same-store task")
    ancestor = request.parent
    seen = {request.item_id}
    while ancestor is not None:
        if ancestor in seen:
            raise TaskLifecycleConflict("task move would create a containment cycle")
        seen.add(ancestor)
        parent_record = _active(snapshot, ancestor)
        ancestor = (
            parent_record.metadata.parent
            if parent_record is not None and isinstance(parent_record.metadata, ActiveTask)
            else None
        )
    target = RankScope(request.parent, record.metadata.stage)
    _guard_anchor(snapshot, request.placement, request.expected_anchor_revision, target)
    if not _placement_matches(snapshot, record.metadata, target, request.placement):
        return False
    planned.path, planned.metadata, planned.body = record.path, record.metadata, record.body
    return True


def _validate_close_preconditions(
    snapshot: FederatedSnapshot,
    request: CloseTaskRequest,
    active: LoadedRecord,
) -> None:
    if _archive(snapshot, request.item_id) is not None:
        return
    assert isinstance(active.metadata, ActiveTask)
    result = readiness(
        TaskRef(selected_store_id(snapshot), request.item_id),
        _graph_state(snapshot),
    )
    if request.outcome is TaskOutcome.DELIVERED and not result.ready:
        raise TaskLifecycleConflict("delivered close requires complete unblocked readiness")
    descendants = {"descendant-active", "descendant-undelivered"}
    if request.outcome is not TaskOutcome.DELIVERED and any(
        blocker.kind.value in descendants for blocker in result.blockers
    ):
        raise TaskLifecycleConflict("close outcome requires every descendant archived")
    if request.outcome is TaskOutcome.CANCELLED and active.metadata.started_at is None:
        raise TaskLifecycleConflict("cancelled close requires a previously started task")


def _guard_close(
    snapshot: FederatedSnapshot,
    request: CloseTaskRequest,
    planned: PlannedRecord,
    formatter: CanonicalFormatter,
    *,
    successor_matches: Callable[[FederatedSnapshot], bool],
    accepted_base_matches: Callable[[FederatedSnapshot], bool],
) -> bool:
    active = _active(snapshot, request.item_id)
    archive = _archive(snapshot, request.item_id)
    if active is None:
        if not (
            archive is not None
            and isinstance(archive.metadata, ArchivedTask)
            and archive.body is not None
            and canonical_revision(formatter, active_source(archive.metadata), archive.body)
            == request.expected_revision
            and archive.metadata.outcome is request.outcome
            and archive.metadata.close_note == request.note
            and successor_matches(snapshot)
        ):
            raise TaskLifecycleConflict("final archive does not exactly match close request")
        if not accepted_base_matches(snapshot):
            raise RevisionConflict("task close store revision is stale")
        planned.path, planned.metadata, planned.body = archive.path, archive.metadata, archive.body
        return True
    if not isinstance(active.metadata, ActiveTask) or active.body is None:
        raise TaskLifecycleConflict("close requires an active task")
    if archive is not None and not semantic_source_matches(active, archive):
        raise TaskLifecycleConflict("active/archive duplicate is divergent")
    if active.revision != request.expected_revision:
        raise RevisionConflict("task close revision is stale")
    if (
        snapshot.selected.store_revision != request.expected_store_revision
        and not accepted_base_matches(snapshot)
    ):
        raise RevisionConflict("task close store revision is stale")
    if request.successor_id is not None:
        successor = _active(snapshot, request.successor_id)
        if successor is None or successor.metadata.id == request.item_id:
            raise TaskLifecycleConflict("successor must be a distinct active same-store task")
        if successor.revision != request.expected_successor_revision and not successor_matches(
            snapshot
        ):
            raise RevisionConflict("successor revision is stale")
    _validate_close_preconditions(snapshot, request, active)
    return False


def _validate_close_snapshot(
    snapshot: FederatedSnapshot,
    request: CloseTaskRequest,
    *,
    intended_archive: Callable[[LoadedRecord], ArchivedTask],
    successor_matches: Callable[[FederatedSnapshot], bool],
) -> tuple[Diagnostic, ...]:
    active = _active(snapshot, request.item_id)
    archive = _archive(snapshot, request.item_id)
    projected_record: LoadedRecord | None = None
    removed: LoadedRecord | None = None
    if active is not None and archive is not None:
        removed = active
    elif active is not None and request.successor_id is not None and successor_matches(snapshot):
        projected_record = LoadedRecord(
            PurePosixPath("archive/tasks") / active.path.name,
            active.revision,
            intended_archive(active),
            active.body,
        )
    else:
        return validate_snapshot(snapshot, require_children=True)
    selected = snapshot.selected.__class__(
        snapshot.selected.location,
        snapshot.selected.store,
        snapshot.selected.registry,
        tuple(
            projected_record if record is active and projected_record is not None else record
            for record in snapshot.selected.records
            if record is not removed
        ),
        snapshot.selected.load_diagnostics,
        tuple(
            value
            for value in snapshot.selected.raw_index
            if removed is None or value.path != removed.path
        ),
        snapshot.selected.store_revision,
        snapshot.selected.registry_revision,
        snapshot.selected.store_config_revision,
    )
    stores = tuple(selected if store is snapshot.selected else store for store in snapshot.stores)
    return validate_snapshot(
        FederatedSnapshot(selected, stores, snapshot.completeness),
        require_children=True,
    )


def _build_close(
    snapshot: FederatedSnapshot,
    request: CloseTaskRequest,
    planned: PlannedRecord,
    formatter: CanonicalFormatter,
    *,
    replay: bool,
    intended_archive: Callable[[LoadedRecord], ArchivedTask],
    successor_matches: Callable[[FederatedSnapshot], bool],
) -> IntendedMutation:
    if replay:
        return IntendedMutation(replayed=True)
    active = _active(snapshot, request.item_id)
    assert (
        active is not None and isinstance(active.metadata, ActiveTask) and active.body is not None
    )
    archive = _archive(snapshot, request.item_id)
    archive_path = PurePosixPath("archive/tasks") / active.path.name
    if archive is not None:
        assert isinstance(archive.metadata, ArchivedTask) and archive.body is not None
        if (
            archive.metadata.outcome is not request.outcome
            or archive.metadata.close_note != request.note
        ):
            raise TaskLifecycleConflict("existing archive diverges from close request")
        metadata = archive.metadata
        planned.path, planned.metadata, planned.body = archive.path, archive.metadata, archive.body
    else:
        metadata = intended_archive(active)
        planned.path, planned.metadata, planned.body = archive_path, metadata, active.body
    replacements: list[FileReplacement] = []
    if request.successor_id is not None and not successor_matches(snapshot):
        successor = _active(snapshot, request.successor_id)
        assert (
            successor is not None
            and isinstance(successor.metadata, ActiveTask)
            and successor.body is not None
        )
        link = supersedes_link(selected_store_id(snapshot), request.item_id)
        linked = validated_copy(successor.metadata, {"links": (*successor.metadata.links, link)})
        replacements.append(
            FileReplacement(successor.path, formatter.item_bytes(linked, successor.body))
        )
    if archive is None:
        replacements.append(
            FileReplacement(archive_path, formatter.item_bytes(metadata, active.body))
        )
    return IntendedMutation(
        replacements=tuple(replacements),
        deletions=(FileDeletion(active.path),),
    )


class TaskService:
    def __init__(
        self,
        executor: MutationExecutor,
        formatter: CanonicalFormatter,
        clock: Clock,
        scope: MutationScope,
    ) -> None:
        self._executor = executor
        self._formatter = formatter
        self._clock = clock
        self._scope = scope

    def transition(self, request: TransitionTaskRequest) -> ItemMutationResult:
        planned = PlannedRecord()
        idempotent = False

        def guard(snapshot: FederatedSnapshot) -> None:
            nonlocal idempotent
            idempotent = _guard_transition(snapshot, request, planned)

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            if idempotent:
                return IntendedMutation()
            record = _active(snapshot, request.item_id)
            assert (
                record is not None
                and isinstance(record.metadata, ActiveTask)
                and record.body is not None
            )
            updates: dict[str, object] = {
                "stage": request.to_stage,
                "revisit_when": request.revisit_when
                if request.to_stage is TaskStage.BACKLOG
                else None,
            }
            if request.to_stage is TaskStage.IN_PROGRESS and record.metadata.started_at is None:
                updates["started_at"] = UtcTimestamp.from_datetime(self._clock.now())
            lifecycle = validated_copy(record.metadata, updates)
            assert isinstance(lifecycle, ActiveTask)
            if record.metadata.stage is request.to_stage:
                final = lifecycle
                replacements: tuple[FileReplacement, ...] = ()
            else:
                replacements, final = _placement_replacements(
                    snapshot,
                    record,
                    lifecycle,
                    RankScope(request.expected_parent, request.to_stage),
                    request.placement,
                    self._formatter,
                )
            planned.path, planned.metadata, planned.body = record.path, final, record.body
            return IntendedMutation(
                replacements=(
                    *replacements,
                    FileReplacement(record.path, self._formatter.item_bytes(final, record.body)),
                )
            )

        receipt = execute_mutation(self._executor, self._scope.recursive, guard, build)
        return record_result(planned, receipt)

    def move(self, request: MoveTaskRequest) -> ItemMutationResult:
        planned = PlannedRecord()
        idempotent = False

        def guard(snapshot: FederatedSnapshot) -> None:
            nonlocal idempotent
            idempotent = _guard_move(snapshot, request, planned)

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            if idempotent:
                return IntendedMutation()
            record = _active(snapshot, request.item_id)
            assert (
                record is not None
                and isinstance(record.metadata, ActiveTask)
                and record.body is not None
            )
            replacements, final = _placement_replacements(
                snapshot,
                record,
                record.metadata,
                RankScope(request.parent, record.metadata.stage),
                request.placement,
                self._formatter,
            )
            planned.path, planned.metadata, planned.body = record.path, final, record.body
            return IntendedMutation(
                replacements=(
                    *replacements,
                    FileReplacement(record.path, self._formatter.item_bytes(final, record.body)),
                )
            )

        receipt = execute_mutation(self._executor, self._scope.recursive, guard, build)
        return record_result(planned, receipt)

    def review(self, request: ReviewTaskRequest) -> ItemMutationResult:
        return CurationService(
            self._executor, self._formatter, self._clock, self._scope
        ).acknowledge(request, require_task=True)

    def close(self, request: CloseTaskRequest) -> ItemMutationResult:
        if not request.note.strip():
            raise TaskLifecycleConflict("close note must be nonempty")
        superseded = request.outcome is TaskOutcome.SUPERSEDED
        has_successor = request.successor_id is not None
        has_successor_guard = request.expected_successor_revision is not None
        if (superseded and not (has_successor and has_successor_guard)) or (
            not superseded and (has_successor or has_successor_guard)
        ):
            raise TaskLifecycleConflict("superseded close alone requires successor and revision")
        planned = PlannedRecord()
        replay = False
        now = UtcTimestamp.from_datetime(self._clock.now())

        def intended_archive(active: LoadedRecord) -> ArchivedTask:
            assert isinstance(active.metadata, ActiveTask)
            return _archive_metadata(active.metadata, request.outcome, request.note, now)

        def successor_matches(snapshot: FederatedSnapshot) -> bool:
            if not superseded:
                return True
            assert request.successor_id is not None
            return (
                successor_source(
                    self._formatter,
                    _active(snapshot, request.successor_id),
                    supersedes_link(selected_store_id(snapshot), request.item_id),
                    request.expected_successor_revision,
                )
                is not None
            )

        def accepted_phase_base_matches(snapshot: FederatedSnapshot) -> bool:
            active = _active(snapshot, request.item_id)
            archive = _archive(snapshot, request.item_id)
            successor = (
                _active(snapshot, request.successor_id)
                if request.successor_id is not None
                else None
            )
            return accepted_close_base_matches(
                self._executor,
                self._formatter,
                snapshot,
                active=active,
                archive=archive,
                successor=successor,
                successor_link=(
                    supersedes_link(selected_store_id(snapshot), request.item_id)
                    if superseded
                    else None
                ),
                expected_successor_revision=request.expected_successor_revision,
                expected_store_revision=request.expected_store_revision,
            )

        def validator(snapshot: FederatedSnapshot) -> tuple[Diagnostic, ...]:
            return _validate_close_snapshot(
                snapshot,
                request,
                intended_archive=intended_archive,
                successor_matches=successor_matches,
            )

        def guard(snapshot: FederatedSnapshot) -> None:
            nonlocal replay
            replay = _guard_close(
                snapshot,
                request,
                planned,
                self._formatter,
                successor_matches=successor_matches,
                accepted_base_matches=accepted_phase_base_matches,
            )

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            return _build_close(
                snapshot,
                request,
                planned,
                self._formatter,
                replay=replay,
                intended_archive=intended_archive,
                successor_matches=successor_matches,
            )

        receipt = execute_mutation(
            self._executor,
            self._scope.recursive,
            guard,
            build,
            validator=validator,
        )
        return record_result(planned, receipt)

    def repair_duplicate(self, request: RepairDuplicateRequest) -> ItemMutationResult:
        planned = PlannedRecord()

        def validator(snapshot: FederatedSnapshot) -> tuple[Diagnostic, ...]:
            active = _active(snapshot, request.item_id)
            archive = _archive(snapshot, request.item_id)
            if active is None or archive is None:
                return validate_snapshot(snapshot, require_children=True)
            selected = snapshot.selected.__class__(
                snapshot.selected.location,
                snapshot.selected.store,
                snapshot.selected.registry,
                tuple(record for record in snapshot.selected.records if record is not active),
                snapshot.selected.load_diagnostics,
                tuple(value for value in snapshot.selected.raw_index if value.path != active.path),
                snapshot.selected.store_revision,
                snapshot.selected.registry_revision,
                snapshot.selected.store_config_revision,
            )
            stores = tuple(
                selected if store is snapshot.selected else store for store in snapshot.stores
            )
            return validate_snapshot(
                FederatedSnapshot(selected, stores, snapshot.completeness), require_children=True
            )

        def guard(snapshot: FederatedSnapshot) -> None:
            active = _active(snapshot, request.item_id)
            archive = _archive(snapshot, request.item_id)
            if active is None or archive is None or not semantic_source_matches(active, archive):
                raise TaskLifecycleConflict(
                    "duplicate repair requires an exact semantic source projection"
                )
            if (
                active.revision != request.expected_active_revision
                or archive.revision != request.expected_archive_revision
            ):
                raise RevisionConflict("duplicate repair revisions are stale")
            planned.path, planned.metadata, planned.body = (
                archive.path,
                archive.metadata,
                archive.body,
            )

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            active = _active(snapshot, request.item_id)
            assert active is not None
            return (
                IntendedMutation(deletions=(FileDeletion(active.path),))
                if request.apply
                else IntendedMutation()
            )

        if not request.apply:
            snapshot = self._scope.recursive.load()
            validator(snapshot)
            guard(snapshot)
            assert (
                planned.path is not None
                and planned.metadata is not None
                and planned.body is not None
            )
            receipt = MutationReceipt(
                applied=False,
                replayed=False,
                canonical_applied=False,
                views_current=False,
                intended_paths=(),
                changed_paths=(),
                item_revisions=tuple(
                    ItemRevision(record.path, record.revision)
                    for record in snapshot.selected.records
                ),
                store_revision=snapshot.selected.store_revision,
                registry_revision=snapshot.selected.registry_revision,
            )
            return record_result(planned, receipt)
        receipt = execute_mutation(
            self._executor,
            self._scope.recursive,
            guard,
            build,
            validator=validator,
        )
        return record_result(planned, receipt)
