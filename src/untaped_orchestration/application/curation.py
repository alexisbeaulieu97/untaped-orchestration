from __future__ import annotations

from dataclasses import dataclass

from untaped_orchestration.application.federation import FederationRead, FederationService
from untaped_orchestration.application.item_support import (
    ItemMutationResult,
    ItemStateConflict,
    MutationScope,
    PlannedRecord,
    decision_inactive,
    execute_mutation,
    guard_revision,
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
from untaped_orchestration.application.results import Completeness, FederatedSnapshot, StoreLocation
from untaped_orchestration.application.validation import _graph_state, validate_snapshot
from untaped_orchestration.domain.curation import (
    CurationEntry,
    StoreCurationContext,
    curation_queue,
)
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.ids import DecisionId, TaskId
from untaped_orchestration.domain.models import ActiveTask, Decision, Revision
from untaped_orchestration.domain.time import CalendarDate, UtcTimestamp


@dataclass(frozen=True, slots=True)
class CurateNextRequest:
    local: bool = False
    limit: int = 50


@dataclass(frozen=True, slots=True)
class CurationPage:
    entries: tuple[CurationEntry, ...]
    complete: bool
    truncated: bool
    diagnostics: tuple[Diagnostic, ...]


def _curation_page(
    snapshot: FederatedSnapshot,
    request: CurateNextRequest,
    clock: Clock,
) -> CurationPage:
    scoped = (
        FederatedSnapshot(snapshot.selected, (snapshot.selected,), Completeness())
        if request.local
        else snapshot
    )
    diagnostics = validate_snapshot(scoped, require_children=not request.local)
    if any(value.severity == "error" for value in diagnostics):
        raise InvalidMutationState(diagnostics)
    contexts = tuple(
        StoreCurationContext(store.store.id, store.store.timezone, store.store.curation)
        for store in scoped.stores
        if store.store is not None
    )
    queue = curation_queue(
        _graph_state(scoped),
        now=UtcTimestamp.from_datetime(clock.now()),
        contexts=contexts,
    )
    return CurationPage(
        queue[: request.limit],
        scoped.completeness.complete,
        len(queue) > request.limit,
        diagnostics,
    )


class CurationReadService:
    def __init__(
        self,
        federation: FederationService,
        location: StoreLocation,
        clock: Clock,
    ) -> None:
        self._federation = federation
        self._location = location
        self._clock = clock

    def next(self, request: CurateNextRequest) -> CurationPage:
        if not 1 <= request.limit <= 200:
            raise ValueError("limit must be in range 1..200")
        return self._federation.run(
            self._location,
            local=request.local,
            action=lambda lease: self._next_locked(lease, request),
        )

    def _next_locked(self, lease: FederationRead, request: CurateNextRequest) -> CurationPage:
        if lease.reader is None:
            return CurationPage(
                (),
                False,
                False,
                lease.snapshot.completeness.diagnostics,
            )
        return _curation_page(lease.snapshot, request, self._clock)


@dataclass(frozen=True, slots=True)
class AcknowledgeRequest:
    item_id: TaskId | DecisionId
    expected_revision: Revision | None
    force_current: bool = False


@dataclass(frozen=True, slots=True)
class SnoozeRequest:
    item_id: TaskId | DecisionId
    until: CalendarDate
    expected_revision: Revision | None
    force_current: bool = False


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

    def acknowledge(
        self, request: AcknowledgeRequest, *, require_task: bool = False
    ) -> ItemMutationResult:
        return self._change(
            request.item_id,
            request.expected_revision,
            request.force_current,
            {"reviewed_at": UtcTimestamp.from_datetime(self._clock.now()), "review_on": None},
            require_task=require_task,
        )

    def snooze(self, request: SnoozeRequest) -> ItemMutationResult:
        return self._change(
            request.item_id,
            request.expected_revision,
            request.force_current,
            {"review_on": request.until},
            require_task=False,
        )

    def _change(
        self,
        item_id: TaskId | DecisionId,
        expected_revision: Revision | None,
        force_current: bool,
        updates: dict[str, object],
        *,
        require_task: bool,
    ) -> ItemMutationResult:
        decision = isinstance(item_id, DecisionId)
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
            if require_task and not isinstance(record.metadata, ActiveTask):
                raise ItemStateConflict("task review requires an active task")
            if isinstance(record.metadata, Decision) and decision_inactive(
                snapshot, record.metadata.id
            ):
                raise ItemStateConflict("inactive decision cannot be curated")
            guard_revision(
                record.revision,
                expected_revision,
                force_current=force_current,
                message="curation revision is stale",
            )

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
