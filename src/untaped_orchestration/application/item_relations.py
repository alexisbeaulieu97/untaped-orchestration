from __future__ import annotations

from pydantic import ValidationError

from untaped_orchestration.application.item_support import (
    EvidenceRequest,
    ItemMutationResult,
    ItemStateConflict,
    LinkRequest,
    MutationScope,
    PlannedRecord,
    RelationConflict,
    decision_inactive,
    execute_mutation,
    guard_revision,
    record_result,
    replacement,
    selected_record,
    selected_store_id,
    validated_copy,
)
from untaped_orchestration.application.mutations import (
    IntendedMutation,
    MutationExecutor,
    validate_selected_local,
)
from untaped_orchestration.application.ports import CanonicalFormatter
from untaped_orchestration.application.results import FederatedSnapshot, LoadedRecord
from untaped_orchestration.domain.evidence import Evidence
from untaped_orchestration.domain.ids import DecisionId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    Link,
    LinkRelation,
)


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
    source = selected_record(snapshot, request.source_id)
    if source is None or source.body is None:
        raise ItemStateConflict("link source does not exist")
    if isinstance(source.metadata, ArchivedTask):
        raise ItemStateConflict("archived task links are immutable")
    if isinstance(source.metadata, Decision):
        if decision_inactive(snapshot, source.metadata.id):
            raise ItemStateConflict("inactive decision links are immutable")
        raise RelationConflict("generic links require an active task source")
    if not isinstance(source.metadata, ActiveTask):
        raise RelationConflict("generic links require an active task source")
    guard_revision(
        source.revision,
        request.expected_revision,
        force_current=request.force_current,
        message="link source revision is stale",
    )
    selected_id = selected_store_id(snapshot)
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
        return self.execute_mutation(scope, request, add=True)

    def remove(self, scope: MutationScope, request: LinkRequest) -> ItemMutationResult:
        return self.execute_mutation(scope, request, add=False)

    def execute_mutation(
        self,
        scope: MutationScope,
        request: LinkRequest,
        *,
        add: bool,
    ) -> ItemMutationResult:
        if request.relation not in self._GENERIC:
            raise RelationConflict("generic link commands cannot mutate supersedes")
        planned = PlannedRecord()

        def guard(snapshot: FederatedSnapshot) -> None:
            _validate_generic_link(snapshot, request)

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            source = selected_record(snapshot, request.source_id)
            assert source is not None and isinstance(source.metadata, ActiveTask)
            assert source.body is not None
            links = _changed_links(source.metadata, request, add=add)
            metadata = validated_copy(source.metadata, {"links": links})
            planned.path = source.path
            planned.metadata = metadata
            planned.body = source.body
            return replacement(self._formatter, source.path, metadata, source.body)

        receipt = execute_mutation(self._executor, scope.recursive, guard, build)
        return record_result(planned, receipt)


class ChangeEvidence:
    def __init__(self, executor: MutationExecutor, formatter: CanonicalFormatter) -> None:
        self._executor = executor
        self._formatter = formatter

    def add(self, scope: MutationScope, request: EvidenceRequest) -> ItemMutationResult:
        return self.execute_mutation(scope, request, add=True)

    def remove(self, scope: MutationScope, request: EvidenceRequest) -> ItemMutationResult:
        return self.execute_mutation(scope, request, add=False)

    def execute_mutation(
        self,
        scope: MutationScope,
        request: EvidenceRequest,
        *,
        add: bool,
    ) -> ItemMutationResult:
        planned = PlannedRecord()

        def guard(snapshot: FederatedSnapshot) -> None:
            record = selected_record(snapshot, request.item_id)
            if record is None or record.body is None:
                raise ItemStateConflict("evidence owner does not exist")
            guard_revision(
                record.revision,
                request.expected_revision,
                force_current=request.force_current,
                message="evidence owner revision is stale",
            )
            if not add and isinstance(record.metadata, ArchivedTask):
                raise ItemStateConflict("archived task evidence is append-only")
            if (
                not add
                and isinstance(record.metadata, Decision)
                and decision_inactive(snapshot, record.metadata.id)
            ):
                raise ItemStateConflict("inactive decision evidence is append-only")

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            record = selected_record(snapshot, request.item_id)
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
            metadata = validated_copy(record.metadata, {"evidence": tuple(values)})
            planned.path = record.path
            planned.metadata = metadata
            planned.body = record.body
            return replacement(self._formatter, record.path, metadata, record.body)

        validator = validate_selected_local if isinstance(request.item_id, DecisionId) else None
        execution = (
            scope.selected_local if isinstance(request.item_id, DecisionId) else scope.recursive
        )
        receipt = execute_mutation(
            self._executor,
            execution,
            guard,
            build,
            validator=validator,
        )
        return record_result(planned, receipt)
