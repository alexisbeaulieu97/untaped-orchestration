from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from untaped_orchestration.application.query_models import ItemDetail, ItemRow
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    LoadedRecord,
    StoreSnapshot,
)
from untaped_orchestration.application.validation import _graph_state, validate_snapshot
from untaped_orchestration.domain.curation import (
    CurationEntry,
    StoreCurationContext,
    curation_queue,
    task_due_on,
)
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.graph import (
    DecisionRef,
    DecisionState,
    GraphState,
    Readiness,
    TaskRef,
    decision_state,
    readiness,
)
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import ActiveTask, ArchivedTask, Decision, Revision
from untaped_orchestration.domain.ordering import TaskOrderItem, sort_tasks
from untaped_orchestration.domain.time import CalendarDate, UtcTimestamp


@dataclass(frozen=True, slots=True)
class SafeProjection:
    snapshot: FederatedSnapshot
    graph: GraphState
    diagnostics: tuple[Diagnostic, ...]
    complete: bool
    due_by_item: dict[tuple[StoreId, TaskId | DecisionId], CalendarDate]
    due_entries: tuple[CurationEntry, ...]


def selected_stores(snapshot: FederatedSnapshot, local: bool) -> tuple[StoreSnapshot, ...]:
    return (snapshot.selected,) if local else snapshot.stores


def selected_scope(snapshot: FederatedSnapshot, local: bool) -> FederatedSnapshot:
    if not local:
        return snapshot
    return FederatedSnapshot(snapshot.selected, (snapshot.selected,), Completeness())


def project_safely(
    snapshot: FederatedSnapshot,
    *,
    local: bool,
    now: UtcTimestamp,
) -> SafeProjection:
    scoped = selected_scope(snapshot, local)
    graph = _graph_state(scoped)
    diagnostics = validate_snapshot(scoped, require_children=False)
    complete = (local or snapshot.completeness.complete) and not any(
        value.severity == "error" for value in diagnostics
    )
    contexts = tuple(
        StoreCurationContext(store.store.id, store.store.timezone, store.store.curation)
        for store in scoped.stores
        if store.store is not None
    )
    try:
        due = curation_queue(graph, now=now, contexts=contexts)
    except ValueError:
        due = ()
    by_context = {value.store_id: value for value in contexts}
    due_by_item: dict[tuple[StoreId, TaskId | DecisionId], CalendarDate] = {}
    for node in graph.tasks:
        if isinstance(node.task, ActiveTask) and node.store_id in by_context:
            context = by_context[node.store_id]
            due_on = task_due_on(
                node.task,
                timezone=context.timezone,
                config=context.config,
            )
            if due_on is not None:
                due_by_item[(node.store_id, node.task.id)] = due_on
    for decision_node in graph.decisions:
        if decision_node.decision.review_on is not None:
            due_by_item[(decision_node.store_id, decision_node.decision.id)] = (
                decision_node.decision.review_on
            )
    return SafeProjection(
        scoped,
        graph,
        diagnostics,
        complete,
        due_by_item,
        due,
    )


def active_records(projection: SafeProjection) -> Iterable[tuple[StoreSnapshot, LoadedRecord]]:
    for store in projection.snapshot.stores:
        if store.store is None:
            continue
        for record in store.records:
            if isinstance(record.metadata, (ActiveTask, Decision)):
                yield store, record


def safe_decision_state(
    graph: GraphState,
    store_id: StoreId,
    item_id: DecisionId,
) -> DecisionState | None:
    try:
        return decision_state(DecisionRef(store_id, item_id), graph)
    except ValueError:
        return None


def safe_readiness(
    graph: GraphState,
    store_id: StoreId,
    item_id: TaskId,
) -> Readiness | None:
    try:
        return readiness(TaskRef(store_id, item_id), graph)
    except ValueError:
        return None


def row_for(
    projection: SafeProjection,
    store_id: StoreId,
    record: LoadedRecord,
) -> ItemRow:
    metadata = record.metadata
    state = (
        safe_decision_state(projection.graph, store_id, metadata.id)
        if isinstance(metadata, Decision)
        else None
    )
    return ItemRow(
        item_id=metadata.id,
        kind=metadata.kind,
        title=metadata.title,
        store_id=store_id,
        path=record.path.as_posix(),
        revision=record.revision,
        stage=metadata.stage if isinstance(metadata, ActiveTask) else None,
        state=state,
        waiting_on=(
            tuple(value.root for value in metadata.waiting_on)
            if isinstance(metadata, (ActiveTask, ArchivedTask))
            else ()
        ),
        priority=(metadata.priority if isinstance(metadata, (ActiveTask, ArchivedTask)) else None),
        rank=metadata.rank if isinstance(metadata, (ActiveTask, ArchivedTask)) else None,
        due_on=projection.due_by_item.get((store_id, metadata.id)),
    )


def detail_for(
    projection: SafeProjection,
    store: StoreSnapshot,
    record: LoadedRecord,
    body: bytes,
) -> ItemDetail:
    assert store.store is not None
    row = row_for(projection, store.store.id, record)
    status = (
        safe_readiness(projection.graph, store.store.id, record.metadata.id)
        if isinstance(record.metadata, ActiveTask)
        else None
    )
    blockers = tuple(value.kind.value for value in status.blockers) if status is not None else ()
    return ItemDetail(
        row,
        record.metadata,
        body,
        store.store_revision,
        blocked=(not status.ready if status is not None else None),
        blockers=blockers,
        due_on=row.due_on,
        complete=projection.complete,
    )


def safe_task_order(items: list[TaskOrderItem]) -> tuple[TaskOrderItem, ...]:
    try:
        return sort_tasks(items)
    except ValueError:
        return tuple(
            sorted(
                items,
                key=lambda value: (
                    value.task.priority.value,
                    value.store_id.root,
                    value.task.rank,
                    value.task.id.root,
                ),
            )
        )


def store_revisions(projection: SafeProjection) -> tuple[tuple[str, Revision], ...]:
    return tuple(
        sorted(
            (store.store.id.root, store.store_revision)
            for store in projection.snapshot.stores
            if store.store is not None
        )
    )
