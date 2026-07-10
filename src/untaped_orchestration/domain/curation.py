from __future__ import annotations

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


_PRIORITY_ORDER = {
    TaskPriority.CRITICAL: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.NORMAL: 2,
    TaskPriority.LOW: 3,
}


def _task_due_on(
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
    timezone: IanaTimezone,
    config: CurationConfig,
) -> tuple[CurationEntry, ...]:
    today = local_calendar_date(now, timezone).as_date()
    sortable: list[tuple[tuple[object, ...], CurationEntry]] = []
    for task_node in graph.tasks:
        if not isinstance(task_node.task, ActiveTask):
            continue
        due_on = _task_due_on(task_node.task, timezone=timezone, config=config)
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
