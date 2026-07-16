from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from untaped_orchestration.domain.graph import (
    DecisionRef,
    DecisionState,
    GraphState,
    decision_state,
)
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import ActiveTask, CurationConfig, TaskPriority, TaskStage
from untaped_orchestration.domain.time import (
    CalendarDate,
    IanaTimezone,
    UtcTimestamp,
    local_calendar_date,
)


class CurationKind(StrEnum):
    TASK = "task"
    DECISION = "decision"


@dataclass(frozen=True, slots=True)
class CurationEntry:
    store_id: StoreId
    kind: CurationKind
    item_id: TaskId | DecisionId
    due_on: CalendarDate


@dataclass(frozen=True, slots=True)
class StoreCurationContext:
    store_id: StoreId
    timezone: IanaTimezone
    config: CurationConfig


_PRIORITY_ORDER = {
    TaskPriority.CRITICAL: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.NORMAL: 2,
    TaskPriority.LOW: 3,
}


def task_due_on(
    task: ActiveTask,
    *,
    timezone: IanaTimezone,
    config: CurationConfig,
) -> CalendarDate | None:
    if task.review_on is not None:
        return task.review_on
    if task.stage is TaskStage.INBOX:
        baseline = task.reviewed_at or task.created_at
        days = config.inbox_review_days
    elif task.stage is TaskStage.IN_PROGRESS:
        assert task.started_at is not None
        baseline = task.reviewed_at or task.started_at
        days = config.in_progress_review_days
    else:
        return None
    local = local_calendar_date(baseline, timezone).as_date()
    return CalendarDate.from_date(local + timedelta(days=days))


def curation_queue(
    graph: GraphState,
    *,
    now: UtcTimestamp,
    contexts: Sequence[StoreCurationContext],
) -> tuple[CurationEntry, ...]:
    grouped: dict[StoreId, list[StoreCurationContext]] = {}
    for context in contexts:
        grouped.setdefault(context.store_id, []).append(context)
    duplicates = sorted(store_id.root for store_id, values in grouped.items() if len(values) > 1)
    if duplicates:
        raise ValueError(f"duplicate curation contexts: {', '.join(duplicates)}")
    required_stores = {node.store_id for node in graph.tasks if isinstance(node.task, ActiveTask)}
    required_stores.update(node.store_id for node in graph.decisions)
    missing = sorted(store_id.root for store_id in required_stores if store_id not in grouped)
    if missing:
        raise ValueError(f"missing curation contexts: {', '.join(missing)}")
    by_store = {store_id: values[0] for store_id, values in grouped.items()}
    sortable: list[tuple[tuple[object, ...], CurationEntry]] = []
    for task_node in graph.tasks:
        if not isinstance(task_node.task, ActiveTask):
            continue
        context = by_store[task_node.store_id]
        today = local_calendar_date(now, context.timezone).as_date()
        due_on = task_due_on(
            task_node.task,
            timezone=context.timezone,
            config=context.config,
        )
        if due_on is None or due_on.as_date() > today:
            continue
        entry = CurationEntry(task_node.store_id, CurationKind.TASK, task_node.task.id, due_on)
        sortable.append(
            (
                (
                    due_on.root,
                    0,
                    _PRIORITY_ORDER[task_node.task.priority],
                    task_node.task.rank,
                    task_node.task.id.root,
                    task_node.store_id.root,
                ),
                entry,
            )
        )
    for decision_node in graph.decisions:
        if (
            decision_state(DecisionRef(decision_node.store_id, decision_node.decision.id), graph)
            is not DecisionState.ACTIVE
        ):
            continue
        context = by_store[decision_node.store_id]
        today = local_calendar_date(now, context.timezone).as_date()
        due_on = decision_node.decision.review_on
        if due_on is None or due_on.as_date() > today:
            continue
        entry = CurationEntry(
            decision_node.store_id,
            CurationKind.DECISION,
            decision_node.decision.id,
            due_on,
        )
        sortable.append(
            (
                (
                    due_on.root,
                    1,
                    decision_node.decision.title,
                    decision_node.decision.id.root,
                    decision_node.store_id.root,
                ),
                entry,
            )
        )
    return tuple(entry for _, entry in sorted(sortable, key=lambda value: value[0]))
