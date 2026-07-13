from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import ValidationError

from untaped_orchestration.application.decision_recovery import (
    SupersedePhase,
    exact_successor_shape,
    pins_after_supersede,
    recognize_supersede_phase,
    retirement_prior_configs,
    store_with_pins,
    supersedes_links,
)
from untaped_orchestration.application.item_support import (
    ItemMutationResult,
    ItemStateConflict,
    MutationScope,
    PlannedRecord,
    RevisionConflict,
    execute_mutation,
    guard_revision,
    item_validation_conflict,
    record_result,
    selected_record,
    selected_store_id,
    validate_force_current,
    validated_copy,
)
from untaped_orchestration.application.mutations import IntendedMutation, MutationExecutor
from untaped_orchestration.application.ports import (
    CanonicalFormatter,
    Clock,
    FileDeletion,
    FileReplacement,
)
from untaped_orchestration.application.results import FederatedSnapshot, LoadedRecord
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.ids import DecisionId, Slug, item_filename
from untaped_orchestration.domain.models import Decision, ItemKind, Link, LinkRelation, Revision
from untaped_orchestration.domain.time import UtcTimestamp


class DecisionLifecycleConflict(ItemStateConflict):
    pass


@dataclass(frozen=True, slots=True)
class DecisionGuard:
    item_id: DecisionId
    expected_revision: Revision | None


@dataclass(frozen=True, slots=True)
class SupersedeDecisionRequest:
    successor_id: DecisionId
    title: str
    body: bytes
    tags: tuple[Slug, ...]
    predecessors: tuple[DecisionGuard, ...]
    expected_store_revision: Revision | None
    force_current: bool = False


@dataclass(frozen=True, slots=True)
class RetireDecisionRequest:
    item_id: DecisionId
    note: str
    expected_revision: Revision | None
    expected_store_revision: Revision | None
    force_current: bool = False


def _decision(snapshot: FederatedSnapshot, item_id: DecisionId) -> LoadedRecord | None:
    record = selected_record(snapshot, item_id)
    if record is not None and not isinstance(record.metadata, Decision):
        raise DecisionLifecycleConflict("decision identity resolves to another item kind")
    return record


def _incoming(snapshot: FederatedSnapshot, item_id: DecisionId) -> tuple[LoadedRecord, ...]:
    store_id = selected_store_id(snapshot)
    return tuple(
        record
        for record in snapshot.selected.records
        if isinstance(record.metadata, Decision)
        and any(
            link.relation is LinkRelation.SUPERSEDES
            and link.target_store_id == store_id
            and link.target == item_id
            for link in record.metadata.links
        )
    )


def _phase_validator(
    snapshot: FederatedSnapshot, allowed_inactive_pins: frozenset[DecisionId]
) -> tuple[Diagnostic, ...]:
    diagnostics = validate_snapshot(snapshot, require_children=True)
    config = snapshot.selected.store
    if config is None:
        return diagnostics
    allowed_fields = {
        f"brief.pinned_decisions.{index}"
        for index, value in enumerate(config.brief.pinned_decisions)
        if value in allowed_inactive_pins
    }
    return tuple(
        value
        for value in diagnostics
        if not (
            value.code == "ORC006"
            and value.field in allowed_fields
            and value.message == "pinned decision must resolve to an active local decision"
        )
    )


def _projected_revision(
    executor: MutationExecutor,
    snapshot: FederatedSnapshot,
    replacements: tuple[FileReplacement, ...] = (),
    deletions: tuple[FileDeletion, ...] = (),
) -> Revision:
    return executor.project(snapshot, replacements, deletions).snapshot.selected.store_revision


def _validate_supersede_request(request: SupersedeDecisionRequest) -> tuple[DecisionId, ...]:
    if not request.predecessors:
        raise DecisionLifecycleConflict("decision supersede requires at least one predecessor")
    predecessor_ids = tuple(value.item_id for value in request.predecessors)
    if len(set(predecessor_ids)) != len(predecessor_ids):
        raise DecisionLifecycleConflict("decision supersede predecessors must be distinct")
    if request.successor_id in predecessor_ids:
        raise DecisionLifecycleConflict("successor must be distinct from every predecessor")
    validate_force_current(
        request.force_current,
        (
            request.expected_store_revision,
            *(value.expected_revision for value in request.predecessors),
        ),
    )
    return predecessor_ids


class _SupersedeOperation:
    def __init__(
        self,
        executor: MutationExecutor,
        formatter: CanonicalFormatter,
        clock: Clock,
        scope: MutationScope,
        request: SupersedeDecisionRequest,
    ) -> None:
        self.executor = executor
        self.formatter = formatter
        self.clock = clock
        self.scope = scope
        self.request = request
        self.predecessor_ids = _validate_supersede_request(request)
        self.predecessor_set = frozenset(self.predecessor_ids)
        self.links: tuple[Link, ...] = ()
        self.phase: SupersedePhase | None = None
        self.planned = PlannedRecord()

    def execute(self) -> ItemMutationResult:
        receipt = execute_mutation(
            self.executor,
            self.scope.recursive,
            self.guard,
            self.build,
            validator=lambda snapshot: _phase_validator(snapshot, self.predecessor_set),
        )
        return record_result(self.planned, receipt)

    def _guard_predecessors(self, snapshot: FederatedSnapshot) -> None:
        for guarded in self.request.predecessors:
            record = _decision(snapshot, guarded.item_id)
            if record is None or record.body is None:
                raise DecisionLifecycleConflict("predecessor must be a local decision")
            metadata = record.metadata
            assert isinstance(metadata, Decision)
            guard_revision(
                record.revision,
                guarded.expected_revision,
                force_current=self.request.force_current,
                message="decision predecessor revision is stale",
            )
            if metadata.retired_at is not None:
                raise DecisionLifecycleConflict("decision supersede requires active predecessors")

    def _guard_initial(self, snapshot: FederatedSnapshot) -> None:
        if any(_incoming(snapshot, item_id) for item_id in self.predecessor_ids):
            raise DecisionLifecycleConflict("decision supersede requires active predecessors")
        guard_revision(
            snapshot.selected.store_revision,
            self.request.expected_store_revision,
            force_current=self.request.force_current,
            message="decision supersede store revision is stale",
        )

    def _reverse_successor_revision(
        self, snapshot: FederatedSnapshot, successor: LoadedRecord
    ) -> Revision:
        return _projected_revision(
            self.executor,
            snapshot,
            deletions=(FileDeletion(successor.path),),
        )

    def _guard_existing(
        self, snapshot: FederatedSnapshot, successor: LoadedRecord
    ) -> SupersedePhase:
        if successor.body is None or not exact_successor_shape(
            successor,
            title=self.request.title,
            body=self.request.body,
            tags=self.request.tags,
            links=self.links,
        ):
            raise DecisionLifecycleConflict("existing successor diverges from caller-owned content")
        if any(
            _incoming(snapshot, guarded.item_id) != (successor,)
            for guarded in self.request.predecessors
        ):
            raise DecisionLifecycleConflict("predecessor has a divergent incoming successor")
        config = snapshot.selected.store
        assert config is not None
        if self.request.force_current:
            return (
                SupersedePhase.FRESH_FINAL
                if pins_after_supersede(
                    config.brief.pinned_decisions,
                    self.predecessor_set,
                    self.request.successor_id,
                )
                == config.brief.pinned_decisions
                else SupersedePhase.SUCCESSOR_ONLY
            )
        assert self.request.expected_store_revision is not None
        phase = recognize_supersede_phase(
            current_revision=snapshot.selected.store_revision,
            expected_revision=self.request.expected_store_revision,
            pins=config.brief.pinned_decisions,
            predecessor_ids=self.predecessor_ids,
            reverse_revision=lambda: self._reverse_successor_revision(snapshot, successor),
        )
        if phase is None:
            raise RevisionConflict("decision supersede phase cannot reconstruct guarded base")
        return phase

    def guard(self, snapshot: FederatedSnapshot) -> None:
        self.links = supersedes_links(selected_store_id(snapshot), self.predecessor_ids)
        self._guard_predecessors(snapshot)
        successor = _decision(snapshot, self.request.successor_id)
        if successor is None:
            self._guard_initial(snapshot)
            self.phase = None
            return
        self.phase = self._guard_existing(snapshot, successor)
        self.planned.path = successor.path
        self.planned.metadata = successor.metadata
        self.planned.body = successor.body

    def _successor_replacement(self) -> FileReplacement:
        try:
            metadata = Decision(
                schema="untaped.orchestration.decision/v1",
                id=self.request.successor_id,
                kind=ItemKind.DECISION,
                title=self.request.title,
                created_at=UtcTimestamp.from_datetime(self.clock.now()),
                tags=self.request.tags,
                links=self.links,
            )
        except ValidationError as error:
            raise item_validation_conflict(error) from error
        path = PurePosixPath("decisions") / item_filename(
            self.request.successor_id, self.request.title
        )
        self.planned.path = path
        self.planned.metadata = metadata
        self.planned.body = self.request.body
        return FileReplacement(path, self.formatter.item_bytes(metadata, self.request.body))

    def build(self, snapshot: FederatedSnapshot) -> IntendedMutation:
        if self.phase is SupersedePhase.FRESH_FINAL:
            return IntendedMutation(finalize_views=False)
        config = snapshot.selected.store
        assert config is not None
        replacements: list[FileReplacement] = []
        successor = _decision(snapshot, self.request.successor_id)
        if successor is None:
            replacements.append(self._successor_replacement())
        pins = pins_after_supersede(
            config.brief.pinned_decisions,
            self.predecessor_set,
            self.request.successor_id,
        )
        if pins != config.brief.pinned_decisions:
            replacements.append(
                FileReplacement(
                    PurePosixPath("store.toml"),
                    self.formatter.store_bytes(store_with_pins(config, pins)),
                )
            )
        replayed = self.phase is SupersedePhase.SUCCESSOR_ONLY and not replacements
        return IntendedMutation(replacements=tuple(replacements), replayed=replayed)


class _RetireOperation:
    def __init__(
        self,
        executor: MutationExecutor,
        formatter: CanonicalFormatter,
        clock: Clock,
        scope: MutationScope,
        request: RetireDecisionRequest,
    ) -> None:
        self.executor = executor
        self.formatter = formatter
        self.clock = clock
        self.scope = scope
        self.request = request
        self.phase = "initial"
        self.planned = PlannedRecord()

    def execute(self) -> ItemMutationResult:
        receipt = execute_mutation(
            self.executor,
            self.scope.recursive,
            self.guard,
            self.build,
            validator=lambda snapshot: _phase_validator(
                snapshot, frozenset({self.request.item_id})
            ),
        )
        return record_result(self.planned, receipt)

    def _reverse_item(self, record: LoadedRecord) -> FileReplacement:
        metadata = record.metadata
        assert isinstance(metadata, Decision) and record.body is not None
        active = validated_copy(metadata, {"retired_at": None, "retire_note": None})
        return FileReplacement(record.path, self.formatter.item_bytes(active, record.body))

    def _reverse_matches(self, snapshot: FederatedSnapshot, record: LoadedRecord) -> bool:
        assert self.request.expected_store_revision is not None
        item = self._reverse_item(record)
        if (
            _projected_revision(self.executor, snapshot, (item,))
            == self.request.expected_store_revision
        ):
            return True
        config = snapshot.selected.store
        if config is None:
            return False
        return any(
            _projected_revision(
                self.executor,
                snapshot,
                (
                    item,
                    FileReplacement(PurePosixPath("store.toml"), self.formatter.store_bytes(prior)),
                ),
            )
            == self.request.expected_store_revision
            for prior in retirement_prior_configs(config, self.request.item_id)
        )

    def _guard_active(self, snapshot: FederatedSnapshot, record: LoadedRecord) -> None:
        if _incoming(snapshot, self.request.item_id):
            raise DecisionLifecycleConflict("superseded decisions cannot be retired")
        guard_revision(
            record.revision,
            self.request.expected_revision,
            force_current=self.request.force_current,
            message="decision revision is stale",
        )
        guard_revision(
            snapshot.selected.store_revision,
            self.request.expected_store_revision,
            force_current=self.request.force_current,
            message="decision retirement store revision is stale",
        )
        self.phase = "initial"

    def _guard_retired(self, snapshot: FederatedSnapshot, record: LoadedRecord) -> None:
        metadata = record.metadata
        assert isinstance(metadata, Decision)
        if metadata.retire_note != self.request.note:
            raise DecisionLifecycleConflict("retirement note diverges from durable phase")
        if not self.request.force_current and not self._reverse_matches(snapshot, record):
            raise RevisionConflict("decision retirement phase cannot reconstruct guarded base")
        config = snapshot.selected.store
        assert config is not None
        self.phase = (
            "final" if self.request.item_id not in config.brief.pinned_decisions else "retired"
        )

    def guard(self, snapshot: FederatedSnapshot) -> None:
        record = _decision(snapshot, self.request.item_id)
        if record is None or record.body is None:
            raise DecisionLifecycleConflict("retirement requires a local decision")
        metadata = record.metadata
        assert isinstance(metadata, Decision)
        if metadata.retired_at is None:
            self._guard_active(snapshot, record)
        else:
            self._guard_retired(snapshot, record)
        self.planned.path = record.path
        self.planned.metadata = record.metadata
        self.planned.body = record.body

    def _retirement_replacement(self, record: LoadedRecord) -> FileReplacement:
        metadata = record.metadata
        assert isinstance(metadata, Decision) and record.body is not None
        retired = validated_copy(
            metadata,
            {
                "retired_at": UtcTimestamp.from_datetime(self.clock.now()),
                "retire_note": self.request.note,
            },
        )
        self.planned.metadata = retired
        return FileReplacement(record.path, self.formatter.item_bytes(retired, record.body))

    def build(self, snapshot: FederatedSnapshot) -> IntendedMutation:
        if self.phase == "final":
            return IntendedMutation(replayed=True)
        record = _decision(snapshot, self.request.item_id)
        assert record is not None
        config = snapshot.selected.store
        assert config is not None
        replacements: list[FileReplacement] = []
        if self.phase == "initial":
            replacements.append(self._retirement_replacement(record))
        pins = tuple(
            value for value in config.brief.pinned_decisions if value != self.request.item_id
        )
        if pins != config.brief.pinned_decisions:
            replacements.append(
                FileReplacement(
                    PurePosixPath("store.toml"),
                    self.formatter.store_bytes(store_with_pins(config, pins)),
                )
            )
        return IntendedMutation(replacements=tuple(replacements))


class DecisionService:
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

    def supersede(self, request: SupersedeDecisionRequest) -> ItemMutationResult:
        return _SupersedeOperation(
            self._executor, self._formatter, self._clock, self._scope, request
        ).execute()

    def retire(self, request: RetireDecisionRequest) -> ItemMutationResult:
        validate_force_current(
            request.force_current,
            (request.expected_revision, request.expected_store_revision),
        )
        if not request.note.strip():
            raise DecisionLifecycleConflict("retirement note must be nonempty")
        return _RetireOperation(
            self._executor, self._formatter, self._clock, self._scope, request
        ).execute()
