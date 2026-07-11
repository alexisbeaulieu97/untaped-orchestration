from __future__ import annotations

from dataclasses import dataclass

from untaped_orchestration.application.item_support import (
    ItemMutationResult,
    ItemStateConflict,
    MutationScope,
    PlannedRecord,
    RevisionConflict,
    decision_inactive,
    execute_mutation,
    record_result,
    selected_record,
    validated_copy,
)
from untaped_orchestration.application.mutations import (
    IntendedMutation,
    InvalidMutationState,
    MutationExecutor,
    validate_selected_local,
)
from untaped_orchestration.application.ports import CanonicalFormatter, Clock, FileReplacement
from untaped_orchestration.application.results import FederatedSnapshot
from untaped_orchestration.application.validation import _graph_state
from untaped_orchestration.domain.curation import (
    CurationEntry,
    StoreCurationContext,
    curation_queue,
)
from untaped_orchestration.domain.ids import DecisionId, TaskId
from untaped_orchestration.domain.models import ActiveTask, Decision, Revision
from untaped_orchestration.domain.time import CalendarDate, UtcTimestamp


@dataclass(frozen=True, slots=True)
class CurateNextRequest:
    local: bool = False


@dataclass(frozen=True, slots=True)
class AcknowledgeRequest:
    item_id: TaskId | DecisionId
    expected_revision: Revision


@dataclass(frozen=True, slots=True)
class SnoozeRequest:
    item_id: TaskId | DecisionId
    until: CalendarDate
    expected_revision: Revision


class CurationService:
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

    def next(self, request: CurateNextRequest) -> tuple[CurationEntry, ...]:
        snapshot = (self._scope.selected_local if request.local else self._scope.recursive).load()
        if not request.local and not snapshot.completeness.complete:
            raise InvalidMutationState(snapshot.completeness.diagnostics)
        stores = (snapshot.selected,) if request.local else snapshot.stores
        contexts = []
        for store in stores:
            if store.store is None:
                continue
            contexts.append(
                StoreCurationContext(store.store.id, store.store.timezone, store.store.curation)
            )
        graph = _graph_state(
            snapshot
            if not request.local
            else snapshot.__class__(
                snapshot.selected, (snapshot.selected,), snapshot.completeness.__class__()
            )
        )
        return curation_queue(
            graph,
            now=UtcTimestamp.from_datetime(self._clock.now()),
            contexts=tuple(contexts),
        )

    def acknowledge(
        self, request: AcknowledgeRequest, *, require_task: bool = False
    ) -> ItemMutationResult:
        return self._change(
            request.item_id,
            request.expected_revision,
            {"reviewed_at": UtcTimestamp.from_datetime(self._clock.now()), "review_on": None},
            require_task=require_task,
        )

    def snooze(self, request: SnoozeRequest) -> ItemMutationResult:
        return self._change(
            request.item_id,
            request.expected_revision,
            {"review_on": request.until},
            require_task=False,
        )

    def _change(
        self,
        item_id: TaskId | DecisionId,
        expected_revision: Revision,
        updates: dict[str, object],
        *,
        require_task: bool,
    ) -> ItemMutationResult:
        initial = self._scope.selected_local.load()
        initial_record = selected_record(initial, item_id)
        if initial_record is None or not isinstance(
            initial_record.metadata, (ActiveTask, Decision)
        ):
            raise ItemStateConflict("curation requires an active task or decision")
        if require_task and not isinstance(initial_record.metadata, ActiveTask):
            raise ItemStateConflict("task review requires an active task")
        decision = isinstance(initial_record.metadata, Decision)
        scope = self._scope.selected_local if decision else self._scope.recursive
        planned = PlannedRecord()

        def guard(snapshot: FederatedSnapshot) -> None:
            record = selected_record(snapshot, item_id)
            if (
                record is None
                or not isinstance(record.metadata, (ActiveTask, Decision))
                or record.body is None
            ):
                raise ItemStateConflict("curation requires an active task or decision")
            if isinstance(record.metadata, Decision) and decision_inactive(
                snapshot, record.metadata.id
            ):
                raise ItemStateConflict("inactive decision cannot be curated")
            if record.revision != expected_revision:
                raise RevisionConflict("curation revision is stale")

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            record = selected_record(snapshot, item_id)
            assert (
                record is not None
                and isinstance(record.metadata, (ActiveTask, Decision))
                and record.body is not None
            )
            metadata = validated_copy(record.metadata, updates)
            planned.path, planned.metadata, planned.body = record.path, metadata, record.body
            return IntendedMutation(
                replacements=(
                    FileReplacement(record.path, self._formatter.item_bytes(metadata, record.body)),
                )
            )

        receipt = execute_mutation(
            self._executor,
            scope,
            guard,
            build,
            validator=validate_selected_local if decision else None,
        )
        return record_result(planned, receipt)
