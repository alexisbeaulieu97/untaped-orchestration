from __future__ import annotations

from pathlib import PurePosixPath

from untaped_orchestration.application.item_relations import (
    ChangeEvidence as ChangeEvidence,
)
from untaped_orchestration.application.item_relations import ChangeLink as ChangeLink
from untaped_orchestration.application.item_support import (
    CreateConflict,
    CreateDecisionRequest,
    CreateTaskRequest,
    ItemMutationResult,
    ItemStateConflict,
    MutationScope,
    PlannedRecord,
    RevisionConflict,
    UpdateDecisionRequest,
    UpdateTaskRequest,
    decision_inactive,
    execute_mutation,
    record_result,
    replacement,
    selected_record,
    validated_copy,
)
from untaped_orchestration.application.item_support import (
    EvidenceRequest as EvidenceRequest,
)
from untaped_orchestration.application.item_support import (
    ItemMutationConflict as ItemMutationConflict,
)
from untaped_orchestration.application.item_support import (
    LinkRequest as LinkRequest,
)
from untaped_orchestration.application.item_support import (
    RelationConflict as RelationConflict,
)
from untaped_orchestration.application.mutations import (
    IntendedMutation,
    MutationExecutor,
    validate_selected_local,
)
from untaped_orchestration.application.ports import CanonicalFormatter, Clock, FileReplacement
from untaped_orchestration.application.results import (
    FederatedSnapshot,
)
from untaped_orchestration.domain.ids import item_filename
from untaped_orchestration.domain.models import (
    ActiveTask,
    Decision,
    ItemKind,
    TaskStage,
)
from untaped_orchestration.domain.ordering import (
    PlacementAnchor,
    PlacementAnchorKind,
    RankScope,
    plan_placement,
)
from untaped_orchestration.domain.time import UtcTimestamp


class CreateTask:
    def __init__(
        self,
        executor: MutationExecutor,
        formatter: CanonicalFormatter,
        clock: Clock,
    ) -> None:
        self._executor = executor
        self._formatter = formatter
        self._clock = clock

    def execute(self, scope: MutationScope, request: CreateTaskRequest) -> ItemMutationResult:
        planned = PlannedRecord()
        replay = False

        def guard(snapshot: FederatedSnapshot) -> None:
            nonlocal replay
            existing = selected_record(snapshot, request.item_id)
            if existing is not None:
                metadata = existing.metadata
                if not isinstance(metadata, ActiveTask) or existing.body is None:
                    raise CreateConflict("existing task identity is not an active task")
                matches = (
                    metadata.title == request.title
                    and existing.body == request.body
                    and metadata.tags == tuple(sorted(request.tags, key=lambda value: value.root))
                    and metadata.priority is request.priority
                    and metadata.waiting_on
                    == tuple(sorted(request.waiting_on, key=lambda value: value.root))
                )
                if not matches:
                    raise CreateConflict("existing task does not match caller-owned create inputs")
                planned.path = existing.path
                planned.metadata = metadata
                planned.body = existing.body
                replay = True
                return
            if snapshot.selected.store_revision != request.expected_store_revision:
                raise RevisionConflict("task create store revision is stale")

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            if replay:
                return IntendedMutation(replayed=True)
            primary = ActiveTask(
                schema="untaped.orchestration.task/v1",
                kind=ItemKind.TASK,
                id=request.item_id,
                title=request.title,
                created_at=UtcTimestamp.from_datetime(self._clock.now()),
                tags=request.tags,
                stage=TaskStage.PLANNED,
                priority=request.priority,
                rank=1000,
                waiting_on=request.waiting_on,
            )
            records = {
                record.metadata.id: record
                for record in snapshot.selected.records
                if isinstance(record.metadata, ActiveTask)
            }
            active_tasks = tuple(
                record.metadata
                for record in snapshot.selected.records
                if isinstance(record.metadata, ActiveTask)
            )
            plan = plan_placement(
                (*active_tasks, primary),
                primary,
                RankScope(parent=None, stage=TaskStage.INBOX),
                PlacementAnchor(PlacementAnchorKind.LAST),
            )
            replacements: list[FileReplacement] = []
            for rank_change in plan.rebalance:
                record = records[rank_change.task_id]
                assert isinstance(record.metadata, ActiveTask) and record.body is not None
                rebalanced = validated_copy(
                    record.metadata,
                    {"rank": rank_change.new_rank},
                )
                replacements.append(
                    FileReplacement(
                        record.path,
                        self._formatter.item_bytes(rebalanced, record.body),
                    )
                )
            metadata = plan.primary
            path = PurePosixPath("tasks") / item_filename(request.item_id, request.title)
            planned.path = path
            planned.metadata = metadata
            planned.body = request.body
            replacements.append(
                FileReplacement(path, self._formatter.item_bytes(metadata, request.body))
            )
            return IntendedMutation(replacements=tuple(replacements))

        receipt = execute_mutation(self._executor, scope, guard, build)
        return record_result(planned, receipt)


class CreateDecision:
    def __init__(
        self,
        executor: MutationExecutor,
        formatter: CanonicalFormatter,
        clock: Clock,
    ) -> None:
        self._executor = executor
        self._formatter = formatter
        self._clock = clock

    def execute(self, scope: MutationScope, request: CreateDecisionRequest) -> ItemMutationResult:
        planned = PlannedRecord()
        replay = False

        def guard(snapshot: FederatedSnapshot) -> None:
            nonlocal replay
            existing = selected_record(snapshot, request.item_id)
            if existing is not None:
                metadata = existing.metadata
                if (
                    not isinstance(metadata, Decision)
                    or existing.body is None
                    or decision_inactive(snapshot, request.item_id)
                ):
                    raise CreateConflict("existing decision identity is not an active decision")
                matches = (
                    metadata.title == request.title
                    and existing.body == request.body
                    and metadata.tags == tuple(sorted(request.tags, key=lambda value: value.root))
                )
                if not matches:
                    raise CreateConflict(
                        "existing decision does not match caller-owned create inputs"
                    )
                planned.path = existing.path
                planned.metadata = metadata
                planned.body = existing.body
                replay = True
                return
            if snapshot.selected.store_revision != request.expected_store_revision:
                raise RevisionConflict("decision create store revision is stale")

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            del snapshot
            if replay:
                return IntendedMutation(replayed=True)
            metadata = Decision(
                schema="untaped.orchestration.decision/v1",
                kind=ItemKind.DECISION,
                id=request.item_id,
                title=request.title,
                created_at=UtcTimestamp.from_datetime(self._clock.now()),
                tags=request.tags,
            )
            path = PurePosixPath("decisions") / item_filename(request.item_id, request.title)
            planned.path = path
            planned.metadata = metadata
            planned.body = request.body
            return replacement(self._formatter, path, metadata, request.body)

        receipt = execute_mutation(self._executor, scope, guard, build)
        return record_result(planned, receipt)


class UpdateTask:
    def __init__(self, executor: MutationExecutor, formatter: CanonicalFormatter) -> None:
        self._executor = executor
        self._formatter = formatter

    def execute(self, scope: MutationScope, request: UpdateTaskRequest) -> ItemMutationResult:
        if all(
            value is None
            for value in (
                request.title,
                request.body,
                request.priority,
                request.tags,
                request.waiting_on,
            )
        ):
            raise ValueError("task update requires at least one changed field")
        planned = PlannedRecord()

        def guard(snapshot: FederatedSnapshot) -> None:
            record = selected_record(snapshot, request.item_id)
            if record is None:
                raise ItemStateConflict("task does not exist in the selected store")
            if not isinstance(record.metadata, ActiveTask) or record.body is None:
                raise ItemStateConflict("task update requires an active task")
            if record.revision != request.expected_revision:
                raise RevisionConflict("task revision is stale")

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            record = selected_record(snapshot, request.item_id)
            assert record is not None and isinstance(record.metadata, ActiveTask)
            assert record.body is not None
            updates: dict[str, object] = {}
            for name in ("title", "priority", "tags", "waiting_on"):
                value = getattr(request, name)
                if value is not None:
                    updates[name] = value
            metadata = validated_copy(record.metadata, updates)
            body = record.body if request.body is None else request.body
            planned.path = record.path
            planned.metadata = metadata
            planned.body = body
            return replacement(self._formatter, record.path, metadata, body)

        receipt = execute_mutation(self._executor, scope, guard, build)
        return record_result(planned, receipt)


class UpdateDecision:
    def __init__(self, executor: MutationExecutor, formatter: CanonicalFormatter) -> None:
        self._executor = executor
        self._formatter = formatter

    def execute(self, scope: MutationScope, request: UpdateDecisionRequest) -> ItemMutationResult:
        if request.title is None and request.body is None and request.tags is None:
            raise ValueError("decision update requires at least one changed field")
        planned = PlannedRecord()

        def guard(snapshot: FederatedSnapshot) -> None:
            record = selected_record(snapshot, request.item_id)
            if record is None or not isinstance(record.metadata, Decision) or record.body is None:
                raise ItemStateConflict("decision update requires a decision")
            if record.revision != request.expected_revision:
                raise RevisionConflict("decision revision is stale")

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            record = selected_record(snapshot, request.item_id)
            assert record is not None and isinstance(record.metadata, Decision)
            assert record.body is not None
            updates: dict[str, object] = {}
            if request.title is not None:
                updates["title"] = request.title
            if request.tags is not None:
                updates["tags"] = request.tags
            metadata = validated_copy(record.metadata, updates)
            body = record.body if request.body is None else request.body
            planned.path = record.path
            planned.metadata = metadata
            planned.body = body
            return replacement(self._formatter, record.path, metadata, body)

        receipt = execute_mutation(
            self._executor,
            scope,
            guard,
            build,
            validator=validate_selected_local,
        )
        return record_result(planned, receipt)
