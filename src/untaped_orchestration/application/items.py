from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import overload

from pydantic import ValidationError

from untaped_orchestration.application.mutations import IntendedMutation, MutationExecutor
from untaped_orchestration.application.ports import CanonicalFormatter, Clock, FileReplacement
from untaped_orchestration.application.results import (
    FederatedSnapshot,
    LoadedRecord,
    MutationReceipt,
    StoreLocation,
)
from untaped_orchestration.domain.canonical import CanonicalItem
from untaped_orchestration.domain.evidence import Evidence, EvidenceReference, EvidenceRelation
from untaped_orchestration.domain.ids import DecisionId, Slug, StoreId, TaskId, item_filename
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    ItemKind,
    Link,
    LinkRelation,
    Revision,
    TaskPriority,
    TaskStage,
)
from untaped_orchestration.domain.time import UtcTimestamp


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
class MutationScope:
    locations: tuple[StoreLocation, ...]
    selected: StoreLocation
    load: Callable[[], FederatedSnapshot]


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
class _PlannedRecord:
    path: PurePosixPath | None = None
    metadata: CanonicalItem | None = None
    body: bytes | None = None


def _selected_store_id(snapshot: FederatedSnapshot) -> StoreId:
    config = snapshot.selected.store
    if config is None:
        raise ItemStateConflict("selected store configuration is unavailable")
    return config.id


def _selected_record(
    snapshot: FederatedSnapshot,
    item_id: TaskId | DecisionId,
) -> LoadedRecord | None:
    matches = tuple(record for record in snapshot.selected.records if record.metadata.id == item_id)
    if len(matches) > 1:
        raise ItemStateConflict("item identity is ambiguous in the selected store")
    return matches[0] if matches else None


def _record_result(planned: _PlannedRecord, receipt: MutationReceipt) -> ItemMutationResult:
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


def _replacement(
    formatter: CanonicalFormatter,
    path: PurePosixPath,
    metadata: CanonicalItem,
    body: bytes,
) -> IntendedMutation:
    return IntendedMutation(
        replacements=(FileReplacement(path, formatter.item_bytes(metadata, body)),)
    )


@overload
def _validated_copy(metadata: ActiveTask, updates: dict[str, object]) -> ActiveTask: ...


@overload
def _validated_copy(metadata: ArchivedTask, updates: dict[str, object]) -> ArchivedTask: ...


@overload
def _validated_copy(metadata: Decision, updates: dict[str, object]) -> Decision: ...


@overload
def _validated_copy(metadata: CanonicalItem, updates: dict[str, object]) -> CanonicalItem: ...


def _validated_copy(
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


def _execute(
    executor: MutationExecutor,
    scope: MutationScope,
    guard: Callable[[FederatedSnapshot], None],
    build: Callable[[FederatedSnapshot], IntendedMutation],
) -> MutationReceipt:
    return executor.execute(
        locations=scope.locations,
        selected=scope.selected,
        load=scope.load,
        guard=guard,
        build=build,
    )


def _decision_inactive(snapshot: FederatedSnapshot, item_id: DecisionId) -> bool:
    record = _selected_record(snapshot, item_id)
    if record is None or not isinstance(record.metadata, Decision):
        return False
    if record.metadata.retired_at is not None:
        return True
    selected_id = _selected_store_id(snapshot)
    return any(
        link.relation is LinkRelation.SUPERSEDES
        and link.target_store_id == selected_id
        and link.target == item_id
        for store in snapshot.stores
        for candidate in store.records
        if isinstance(candidate.metadata, Decision)
        for link in candidate.metadata.links
    )


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
        planned = _PlannedRecord()
        replay = False

        def guard(snapshot: FederatedSnapshot) -> None:
            nonlocal replay
            existing = _selected_record(snapshot, request.item_id)
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
            ranks = [
                record.metadata.rank
                for record in snapshot.selected.records
                if isinstance(record.metadata, ActiveTask)
                and record.metadata.stage is TaskStage.INBOX
                and record.metadata.parent is None
            ]
            rank = (max(ranks) + 1000) if ranks else 1000
            metadata = ActiveTask(
                schema="untaped.orchestration.task/v1",
                kind=ItemKind.TASK,
                id=request.item_id,
                title=request.title,
                created_at=UtcTimestamp.from_datetime(self._clock.now()),
                tags=request.tags,
                stage=TaskStage.INBOX,
                priority=request.priority,
                rank=rank,
                waiting_on=request.waiting_on,
            )
            path = PurePosixPath("tasks") / item_filename(request.item_id, request.title)
            planned.path = path
            planned.metadata = metadata
            planned.body = request.body
            return _replacement(self._formatter, path, metadata, request.body)

        receipt = _execute(self._executor, scope, guard, build)
        return _record_result(planned, receipt)


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
        planned = _PlannedRecord()
        replay = False

        def guard(snapshot: FederatedSnapshot) -> None:
            nonlocal replay
            existing = _selected_record(snapshot, request.item_id)
            if existing is not None:
                metadata = existing.metadata
                if (
                    not isinstance(metadata, Decision)
                    or existing.body is None
                    or _decision_inactive(snapshot, request.item_id)
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
            return _replacement(self._formatter, path, metadata, request.body)

        receipt = _execute(self._executor, scope, guard, build)
        return _record_result(planned, receipt)


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
        planned = _PlannedRecord()

        def guard(snapshot: FederatedSnapshot) -> None:
            record = _selected_record(snapshot, request.item_id)
            if record is None:
                raise ItemStateConflict("task does not exist in the selected store")
            if not isinstance(record.metadata, ActiveTask) or record.body is None:
                raise ItemStateConflict("task update requires an active task")
            if record.revision != request.expected_revision:
                raise RevisionConflict("task revision is stale")

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            record = _selected_record(snapshot, request.item_id)
            assert record is not None and isinstance(record.metadata, ActiveTask)
            assert record.body is not None
            updates: dict[str, object] = {}
            for name in ("title", "priority", "tags", "waiting_on"):
                value = getattr(request, name)
                if value is not None:
                    updates[name] = value
            metadata = _validated_copy(record.metadata, updates)
            body = record.body if request.body is None else request.body
            planned.path = record.path
            planned.metadata = metadata
            planned.body = body
            return _replacement(self._formatter, record.path, metadata, body)

        receipt = _execute(self._executor, scope, guard, build)
        return _record_result(planned, receipt)


class UpdateDecision:
    def __init__(self, executor: MutationExecutor, formatter: CanonicalFormatter) -> None:
        self._executor = executor
        self._formatter = formatter

    def execute(self, scope: MutationScope, request: UpdateDecisionRequest) -> ItemMutationResult:
        if request.title is None and request.body is None and request.tags is None:
            raise ValueError("decision update requires at least one changed field")
        planned = _PlannedRecord()

        def guard(snapshot: FederatedSnapshot) -> None:
            record = _selected_record(snapshot, request.item_id)
            if record is None or not isinstance(record.metadata, Decision) or record.body is None:
                raise ItemStateConflict("decision update requires a decision")
            if record.revision != request.expected_revision:
                raise RevisionConflict("decision revision is stale")

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            record = _selected_record(snapshot, request.item_id)
            assert record is not None and isinstance(record.metadata, Decision)
            assert record.body is not None
            updates: dict[str, object] = {}
            if request.title is not None:
                updates["title"] = request.title
            if request.tags is not None:
                updates["tags"] = request.tags
            metadata = _validated_copy(record.metadata, updates)
            body = record.body if request.body is None else request.body
            planned.path = record.path
            planned.metadata = metadata
            planned.body = body
            return _replacement(self._formatter, record.path, metadata, body)

        receipt = _execute(self._executor, scope, guard, build)
        return _record_result(planned, receipt)


def _target_record(snapshot: FederatedSnapshot, request: LinkRequest) -> LoadedRecord:
    stores = tuple(
        store
        for store in snapshot.stores
        if store.store is not None and store.store.id == request.target_store_id
    )
    if len(stores) != 1:
        raise RelationConflict("relation target store is missing or ambiguous")
    matches = tuple(
        record for record in stores[0].records if record.metadata.id == request.target_id
    )
    if len(matches) != 1:
        raise RelationConflict("relation target item is missing or ambiguous")
    return matches[0]


def _validate_generic_link(snapshot: FederatedSnapshot, request: LinkRequest) -> None:
    source = _selected_record(snapshot, request.source_id)
    if source is None or source.body is None:
        raise ItemStateConflict("link source does not exist")
    if isinstance(source.metadata, ArchivedTask):
        raise ItemStateConflict("archived task links are immutable")
    if isinstance(source.metadata, Decision):
        if _decision_inactive(snapshot, source.metadata.id):
            raise ItemStateConflict("inactive decision links are immutable")
        raise RelationConflict("generic links require an active task source")
    if not isinstance(source.metadata, ActiveTask):
        raise RelationConflict("generic links require an active task source")
    if source.revision != request.expected_revision:
        raise RevisionConflict("link source revision is stale")
    selected_id = _selected_store_id(snapshot)
    if request.relation is LinkRelation.DEPENDS_ON and request.target_store_id != selected_id:
        raise RelationConflict("depends-on is a same-store relation")
    target = _target_record(snapshot, request)
    if request.relation in {LinkRelation.DEPENDS_ON, LinkRelation.FOLLOW_UP_TO}:
        if not isinstance(request.target_id, TaskId) or not isinstance(
            target.metadata, (ActiveTask, ArchivedTask)
        ):
            raise RelationConflict(f"{request.relation.value} requires a task target")
    elif not isinstance(request.target_id, DecisionId) or not isinstance(target.metadata, Decision):
        raise RelationConflict("governed-by requires a decision target")


def _changed_links(source: ActiveTask, request: LinkRequest, *, add: bool) -> tuple[Link, ...]:
    try:
        link = Link(
            relation=request.relation,
            target_store_id=request.target_store_id,
            target=request.target_id,
        )
    except ValidationError as error:
        raise RelationConflict("relation target kind is invalid") from error
    links = list(source.links)
    if add:
        if link in links:
            raise RelationConflict("link already exists")
        links.append(link)
    else:
        if link not in links:
            raise RelationConflict("link does not exist")
        links.remove(link)
    return tuple(links)


class ChangeLink:
    _GENERIC = frozenset(
        {LinkRelation.DEPENDS_ON, LinkRelation.GOVERNED_BY, LinkRelation.FOLLOW_UP_TO}
    )

    def __init__(self, executor: MutationExecutor, formatter: CanonicalFormatter) -> None:
        self._executor = executor
        self._formatter = formatter

    def add(self, scope: MutationScope, request: LinkRequest) -> ItemMutationResult:
        return self._execute(scope, request, add=True)

    def remove(self, scope: MutationScope, request: LinkRequest) -> ItemMutationResult:
        return self._execute(scope, request, add=False)

    def _execute(
        self,
        scope: MutationScope,
        request: LinkRequest,
        *,
        add: bool,
    ) -> ItemMutationResult:
        if request.relation not in self._GENERIC:
            raise RelationConflict("generic link commands cannot mutate supersedes")
        planned = _PlannedRecord()

        def guard(snapshot: FederatedSnapshot) -> None:
            _validate_generic_link(snapshot, request)

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            source = _selected_record(snapshot, request.source_id)
            assert source is not None and isinstance(source.metadata, ActiveTask)
            assert source.body is not None
            links = _changed_links(source.metadata, request, add=add)
            metadata = _validated_copy(source.metadata, {"links": links})
            planned.path = source.path
            planned.metadata = metadata
            planned.body = source.body
            return _replacement(self._formatter, source.path, metadata, source.body)

        receipt = _execute(self._executor, scope, guard, build)
        return _record_result(planned, receipt)


class ChangeEvidence:
    def __init__(self, executor: MutationExecutor, formatter: CanonicalFormatter) -> None:
        self._executor = executor
        self._formatter = formatter

    def add(self, scope: MutationScope, request: EvidenceRequest) -> ItemMutationResult:
        return self._execute(scope, request, add=True)

    def remove(self, scope: MutationScope, request: EvidenceRequest) -> ItemMutationResult:
        return self._execute(scope, request, add=False)

    def _execute(
        self,
        scope: MutationScope,
        request: EvidenceRequest,
        *,
        add: bool,
    ) -> ItemMutationResult:
        planned = _PlannedRecord()

        def guard(snapshot: FederatedSnapshot) -> None:
            record = _selected_record(snapshot, request.item_id)
            if record is None or record.body is None:
                raise ItemStateConflict("evidence owner does not exist")
            if record.revision != request.expected_revision:
                raise RevisionConflict("evidence owner revision is stale")
            if not add and isinstance(record.metadata, ArchivedTask):
                raise ItemStateConflict("archived task evidence is append-only")
            if (
                not add
                and isinstance(record.metadata, Decision)
                and _decision_inactive(snapshot, record.metadata.id)
            ):
                raise ItemStateConflict("inactive decision evidence is append-only")

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            record = _selected_record(snapshot, request.item_id)
            assert record is not None and record.body is not None
            evidence = Evidence(relation=request.relation, reference=request.reference)
            values = list(record.metadata.evidence)
            if add:
                if evidence in values:
                    raise ItemStateConflict("evidence already exists")
                values.append(evidence)
            else:
                if evidence not in values:
                    raise ItemStateConflict("evidence does not exist")
                values.remove(evidence)
            metadata = _validated_copy(record.metadata, {"evidence": tuple(values)})
            planned.path = record.path
            planned.metadata = metadata
            planned.body = record.body
            return _replacement(self._formatter, record.path, metadata, record.body)

        receipt = _execute(self._executor, scope, guard, build)
        return _record_result(planned, receipt)
