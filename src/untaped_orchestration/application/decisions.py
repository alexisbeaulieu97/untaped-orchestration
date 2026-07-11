from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import combinations, permutations
from pathlib import PurePosixPath

from untaped_orchestration.application.item_support import (
    ItemMutationResult,
    ItemStateConflict,
    MutationScope,
    PlannedRecord,
    RevisionConflict,
    execute_mutation,
    record_result,
    selected_record,
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
from untaped_orchestration.application.results import FederatedSnapshot, LoadedRecord
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.ids import DecisionId, Slug, StoreId, item_filename
from untaped_orchestration.domain.models import (
    Decision,
    ItemKind,
    Link,
    LinkRelation,
    Revision,
    StoreConfig,
)
from untaped_orchestration.domain.time import UtcTimestamp


class DecisionLifecycleConflict(ItemStateConflict):
    pass


@dataclass(frozen=True, slots=True)
class DecisionGuard:
    item_id: DecisionId
    expected_revision: Revision


@dataclass(frozen=True, slots=True)
class SupersedeDecisionRequest:
    successor_id: DecisionId
    title: str
    body: bytes
    tags: tuple[Slug, ...]
    predecessors: tuple[DecisionGuard, ...]
    expected_store_revision: Revision


@dataclass(frozen=True, slots=True)
class RetireDecisionRequest:
    item_id: DecisionId
    note: str
    expected_revision: Revision
    expected_store_revision: Revision


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


def _links(store_id: StoreId, predecessors: tuple[DecisionGuard, ...]) -> tuple[Link, ...]:
    return tuple(
        Link(relation=LinkRelation.SUPERSEDES, target_store_id=store_id, target=value.item_id)
        for value in predecessors
    )


def _pins_after_supersede(
    pins: tuple[DecisionId, ...], predecessors: frozenset[DecisionId], successor: DecisionId
) -> tuple[DecisionId, ...]:
    predecessor_positions = [index for index, value in enumerate(pins) if value in predecessors]
    stripped = [value for value in pins if value not in predecessors and value != successor]
    if not predecessor_positions:
        return tuple(stripped)
    earliest = min(predecessor_positions)
    insertion = sum(
        1 for value in pins[:earliest] if value not in predecessors and value != successor
    )
    stripped.insert(insertion, successor)
    return tuple(stripped)


def _store_with_pins(config: StoreConfig, pins: tuple[DecisionId, ...]) -> StoreConfig:
    return config.model_copy(
        update={"brief": config.brief.model_copy(update={"pinned_decisions": pins})}
    )


def _projected_revision(
    executor: MutationExecutor,
    snapshot: FederatedSnapshot,
    replacements: tuple[FileReplacement, ...] = (),
    deletions: tuple[FileDeletion, ...] = (),
) -> Revision:
    return executor.project(snapshot, replacements, deletions).snapshot.selected.store_revision


def _prior_pin_candidates(
    final: tuple[DecisionId, ...], predecessors: tuple[DecisionId, ...], successor: DecisionId
) -> Iterator[tuple[DecisionId, ...]]:
    unrelated = tuple(value for value in final if value != successor and value not in predecessors)
    if successor not in final:
        yield unrelated
        return
    capacity = 10 - len(unrelated)
    for count in range(1, min(capacity, len(predecessors)) + 1):
        for subset in combinations(predecessors, count):
            for ordered in permutations(subset):
                for positions in combinations(range(len(unrelated) + count), count):
                    position_set = set(positions)
                    unrelated_values = iter(unrelated)
                    ordered_values = iter(ordered)
                    candidate = tuple(
                        next(ordered_values) if index in position_set else next(unrelated_values)
                        for index in range(len(unrelated) + count)
                    )
                    if (
                        _pins_after_supersede(candidate, frozenset(predecessors), successor)
                        == final
                    ):
                        yield candidate


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

    def supersede(self, request: SupersedeDecisionRequest) -> ItemMutationResult:  # noqa: C901
        if not request.predecessors:
            raise DecisionLifecycleConflict("decision supersede requires at least one predecessor")
        predecessor_ids = tuple(value.item_id for value in request.predecessors)
        if len(set(predecessor_ids)) != len(predecessor_ids):
            raise DecisionLifecycleConflict("decision supersede predecessors must be distinct")
        if request.successor_id in predecessor_ids:
            raise DecisionLifecycleConflict("successor must be distinct from every predecessor")

        planned = PlannedRecord()
        phase = "initial"
        links: tuple[Link, ...] = ()

        def exact_successor(snapshot: FederatedSnapshot, record: LoadedRecord) -> bool:
            return (
                isinstance(record.metadata, Decision)
                and record.body == request.body
                and record.metadata.title == request.title
                and record.metadata.tags
                == tuple(sorted(request.tags, key=lambda value: value.root))
                and record.metadata.links == links
                and record.metadata.reviewed_at is None
                and record.metadata.review_on is None
                and record.metadata.retired_at is None
            )

        def recognized_phase(snapshot: FederatedSnapshot, successor: LoadedRecord) -> str | None:
            if (
                _projected_revision(
                    self._executor, snapshot, deletions=(FileDeletion(successor.path),)
                )
                == request.expected_store_revision
            ):
                config = snapshot.selected.store
                assert config is not None
                planned_pins = _pins_after_supersede(
                    config.brief.pinned_decisions,
                    frozenset(predecessor_ids),
                    request.successor_id,
                )
                return "final" if planned_pins == config.brief.pinned_decisions else "successor"
            config = snapshot.selected.store
            if config is None:
                return None
            for pins in _prior_pin_candidates(
                config.brief.pinned_decisions, predecessor_ids, request.successor_id
            ):
                prior = _store_with_pins(config, pins)
                if (
                    _projected_revision(
                        self._executor,
                        snapshot,
                        (
                            FileReplacement(
                                PurePosixPath("store.toml"), self._formatter.store_bytes(prior)
                            ),
                        ),
                        (FileDeletion(successor.path),),
                    )
                    == request.expected_store_revision
                ):
                    return "final"
            return None

        def guard(snapshot: FederatedSnapshot) -> None:
            nonlocal phase, links
            links = _links(selected_store_id(snapshot), request.predecessors)
            predecessors = []
            for guarded in request.predecessors:
                record = _decision(snapshot, guarded.item_id)
                if record is None or record.body is None:
                    raise DecisionLifecycleConflict("predecessor must be a local decision")
                assert isinstance(record.metadata, Decision)
                if record.revision != guarded.expected_revision:
                    raise RevisionConflict("decision predecessor revision is stale")
                predecessors.append(record)
            successor = _decision(snapshot, request.successor_id)
            if successor is None:
                for guarded, record in zip(request.predecessors, predecessors, strict=True):
                    metadata = record.metadata
                    assert isinstance(metadata, Decision)
                    if metadata.retired_at is not None or _incoming(snapshot, guarded.item_id):
                        raise DecisionLifecycleConflict(
                            "decision supersede requires active predecessors"
                        )
                if snapshot.selected.store_revision != request.expected_store_revision:
                    raise RevisionConflict("decision supersede store revision is stale")
                phase = "initial"
                return
            if successor.body is None or not exact_successor(snapshot, successor):
                raise DecisionLifecycleConflict(
                    "existing successor diverges from caller-owned content"
                )
            if any(
                _incoming(snapshot, value.item_id) != (successor,) for value in request.predecessors
            ):
                raise DecisionLifecycleConflict("predecessor has a divergent incoming successor")
            recognized = recognized_phase(snapshot, successor)
            if recognized is None:
                raise RevisionConflict("decision supersede phase cannot reconstruct guarded base")
            phase = recognized
            planned.path, planned.metadata, planned.body = (
                successor.path,
                successor.metadata,
                successor.body,
            )

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            config = snapshot.selected.store
            assert config is not None
            if phase == "final":
                return IntendedMutation(replayed=True)
            replacements: list[FileReplacement] = []
            successor = _decision(snapshot, request.successor_id)
            if successor is None:
                metadata = Decision(
                    schema="untaped.orchestration.decision/v1",
                    id=request.successor_id,
                    kind=ItemKind.DECISION,
                    title=request.title,
                    created_at=UtcTimestamp.from_datetime(self._clock.now()),
                    tags=request.tags,
                    links=links,
                )
                path = PurePosixPath("decisions") / item_filename(
                    request.successor_id, request.title
                )
                planned.path, planned.metadata, planned.body = path, metadata, request.body
                replacements.append(
                    FileReplacement(path, self._formatter.item_bytes(metadata, request.body))
                )
            else:
                planned.path, planned.metadata, planned.body = (
                    successor.path,
                    successor.metadata,
                    successor.body,
                )
            pins = _pins_after_supersede(
                config.brief.pinned_decisions, frozenset(predecessor_ids), request.successor_id
            )
            if pins != config.brief.pinned_decisions:
                replacements.append(
                    FileReplacement(
                        PurePosixPath("store.toml"),
                        self._formatter.store_bytes(_store_with_pins(config, pins)),
                    )
                )
            return IntendedMutation(replacements=tuple(replacements))

        receipt = execute_mutation(
            self._executor,
            self._scope.recursive,
            guard,
            build,
            validator=lambda snapshot: _phase_validator(snapshot, frozenset(predecessor_ids)),
        )
        return record_result(planned, receipt)

    def retire(self, request: RetireDecisionRequest) -> ItemMutationResult:  # noqa: C901
        if not request.note.strip():
            raise DecisionLifecycleConflict("retirement note must be nonempty")
        planned = PlannedRecord()
        phase = "initial"

        def reverse_item(record: LoadedRecord) -> FileReplacement:
            assert isinstance(record.metadata, Decision) and record.body is not None
            active = validated_copy(record.metadata, {"retired_at": None, "retire_note": None})
            return FileReplacement(record.path, self._formatter.item_bytes(active, record.body))

        def reverse_matches(snapshot: FederatedSnapshot, record: LoadedRecord) -> bool:
            item = reverse_item(record)
            if (
                _projected_revision(self._executor, snapshot, (item,))
                == request.expected_store_revision
            ):
                return True
            config = snapshot.selected.store
            if config is None or request.item_id in config.brief.pinned_decisions:
                return False
            for index in range(len(config.brief.pinned_decisions) + 1):
                pins = list(config.brief.pinned_decisions)
                pins.insert(index, request.item_id)
                prior = _store_with_pins(config, tuple(pins))
                if (
                    _projected_revision(
                        self._executor,
                        snapshot,
                        (
                            item,
                            FileReplacement(
                                PurePosixPath("store.toml"), self._formatter.store_bytes(prior)
                            ),
                        ),
                    )
                    == request.expected_store_revision
                ):
                    return True
            return False

        def guard(snapshot: FederatedSnapshot) -> None:
            nonlocal phase
            record = _decision(snapshot, request.item_id)
            if record is None or record.body is None:
                raise DecisionLifecycleConflict("retirement requires a local decision")
            metadata = record.metadata
            assert isinstance(metadata, Decision)
            if metadata.retired_at is None:
                if _incoming(snapshot, request.item_id):
                    raise DecisionLifecycleConflict("superseded decisions cannot be retired")
                if record.revision != request.expected_revision:
                    raise RevisionConflict("decision revision is stale")
                if snapshot.selected.store_revision != request.expected_store_revision:
                    raise RevisionConflict("decision retirement store revision is stale")
                phase = "initial"
            else:
                if metadata.retire_note != request.note:
                    raise DecisionLifecycleConflict("retirement note diverges from durable phase")
                if not reverse_matches(snapshot, record):
                    raise RevisionConflict(
                        "decision retirement phase cannot reconstruct guarded base"
                    )
                config = snapshot.selected.store
                assert config is not None
                phase = (
                    "final" if request.item_id not in config.brief.pinned_decisions else "retired"
                )
            planned.path, planned.metadata, planned.body = record.path, record.metadata, record.body

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            record = _decision(snapshot, request.item_id)
            assert (
                record is not None
                and isinstance(record.metadata, Decision)
                and record.body is not None
            )
            config = snapshot.selected.store
            assert config is not None
            if phase == "final":
                return IntendedMutation(replayed=True)
            replacements: list[FileReplacement] = []
            if phase == "initial":
                retired = validated_copy(
                    record.metadata,
                    {
                        "retired_at": UtcTimestamp.from_datetime(self._clock.now()),
                        "retire_note": request.note,
                    },
                )
                planned.metadata = retired
                replacements.append(
                    FileReplacement(record.path, self._formatter.item_bytes(retired, record.body))
                )
            pins = tuple(
                value for value in config.brief.pinned_decisions if value != request.item_id
            )
            if pins != config.brief.pinned_decisions:
                replacements.append(
                    FileReplacement(
                        PurePosixPath("store.toml"),
                        self._formatter.store_bytes(_store_with_pins(config, pins)),
                    )
                )
            return IntendedMutation(replacements=tuple(replacements))

        receipt = execute_mutation(
            self._executor,
            self._scope.recursive,
            guard,
            build,
            validator=lambda snapshot: _phase_validator(snapshot, frozenset({request.item_id})),
        )
        return record_result(planned, receipt)
