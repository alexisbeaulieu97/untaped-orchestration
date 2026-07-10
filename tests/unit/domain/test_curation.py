from __future__ import annotations

from untaped_orchestration.domain.curation import CurationKind, curation_queue
from untaped_orchestration.domain.graph import (
    DecisionNode,
    GraphCompleteness,
    GraphState,
    TaskNode,
)
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    CurationConfig,
    Decision,
    Link,
    LinkRelation,
    TaskPriority,
    TaskStage,
)
from untaped_orchestration.domain.time import CalendarDate, IanaTimezone, UtcTimestamp

STORE = StoreId("sto_019f0000000070008000000000000000")
CONFIG = CurationConfig(inbox_review_days=7, in_progress_review_days=14)


def tid(number: int) -> TaskId:
    return TaskId(f"tsk_019f000000007000800000000000{number:04x}")


def did(number: int) -> DecisionId:
    return DecisionId(f"dec_019f000000007000800000000000{number:04x}")


def task(
    number: int,
    stage: TaskStage,
    *,
    created: str = "2026-07-01T03:30:00.000Z",
    reviewed: str | None = None,
    review_on: str | None = None,
    priority: TaskPriority = TaskPriority.NORMAL,
    rank: int = 1000,
) -> ActiveTask:
    return ActiveTask(
        schema="untaped.orchestration.task/v1",
        id=tid(number),
        kind="task",
        title=f"Task {number}",
        created_at=UtcTimestamp(created),
        tags=(),
        links=(),
        evidence=(),
        stage=stage,
        priority=priority,
        rank=rank,
        started_at=UtcTimestamp(created) if stage is TaskStage.IN_PROGRESS else None,
        revisit_when="later" if stage is TaskStage.BACKLOG else None,
        reviewed_at=UtcTimestamp(reviewed) if reviewed else None,
        review_on=CalendarDate(review_on) if review_on else None,
        waiting_on=(),
    )


def decision(
    number: int,
    *,
    review_on: str | None,
    title: str | None = None,
    supersedes: DecisionId | None = None,
) -> Decision:
    links = (
        (
            Link(
                relation=LinkRelation.SUPERSEDES,
                target_store_id=STORE,
                target=supersedes,
            ),
        )
        if supersedes is not None
        else ()
    )
    return Decision(
        schema="untaped.orchestration.decision/v1",
        id=did(number),
        kind="decision",
        title=title or f"Decision {number}",
        created_at=UtcTimestamp("2026-07-01T00:00:00.000Z"),
        tags=(),
        links=links,
        evidence=(),
        review_on=CalendarDate(review_on) if review_on else None,
    )


def state(tasks: tuple[ActiveTask, ...] = (), decisions: tuple[Decision, ...] = ()) -> GraphState:
    return GraphState(
        tasks=tuple(TaskNode(STORE, f"tasks/{value.id.root}.md", value) for value in tasks),
        decisions=tuple(
            DecisionNode(STORE, f"decisions/{value.id.root}.md", value) for value in decisions
        ),
        completeness=GraphCompleteness(complete=True),
    )


def test_timezone_boundary_drives_implicit_inbox_due_date_from_local_creation_date() -> None:
    value = task(1, TaskStage.INBOX, created="2026-07-01T03:30:00.000Z")

    montreal = curation_queue(
        state((value,)),
        now=UtcTimestamp("2026-07-08T03:59:59.000Z"),
        timezone=IanaTimezone("America/Montreal"),
        config=CONFIG,
    )
    utc = curation_queue(
        state((value,)),
        now=UtcTimestamp("2026-07-08T03:59:59.000Z"),
        timezone=IanaTimezone("UTC"),
        config=CONFIG,
    )

    assert [entry.due_on.root for entry in montreal] == ["2026-07-07"]
    assert [entry.due_on.root for entry in utc] == ["2026-07-08"]


def test_reviewed_at_replaces_creation_or_start_baseline_for_implicit_stages() -> None:
    inbox = task(1, TaskStage.INBOX, reviewed="2026-07-05T12:00:00.000Z")
    progress = task(2, TaskStage.IN_PROGRESS, reviewed="2026-07-01T12:00:00.000Z")

    queue = curation_queue(
        state((inbox, progress)),
        now=UtcTimestamp("2026-07-15T23:00:00.000Z"),
        timezone=IanaTimezone("UTC"),
        config=CONFIG,
    )

    assert [(entry.item_id.root, entry.due_on.root) for entry in queue] == [
        (tid(1).root, "2026-07-12"),
        (tid(2).root, "2026-07-15"),
    ]


def test_backlog_planned_and_decisions_are_due_only_with_explicit_review_on() -> None:
    values = (
        task(1, TaskStage.BACKLOG),
        task(2, TaskStage.PLANNED),
        task(3, TaskStage.BACKLOG, review_on="2026-07-10"),
        task(4, TaskStage.PLANNED, review_on="2026-07-10"),
    )
    decisions = (decision(1, review_on=None), decision(2, review_on="2026-07-10"))

    queue = curation_queue(
        state(values, decisions),
        now=UtcTimestamp("2026-07-10T23:59:59.000Z"),
        timezone=IanaTimezone("UTC"),
        config=CONFIG,
    )

    assert [entry.item_id.root for entry in queue] == [tid(3).root, tid(4).root, did(2).root]


def test_explicit_review_on_overrides_implicit_stage_due_date() -> None:
    value = task(1, TaskStage.INBOX, review_on="2026-08-01")
    queue = curation_queue(
        state((value,)),
        now=UtcTimestamp("2026-07-31T23:59:59.000Z"),
        timezone=IanaTimezone("UTC"),
        config=CONFIG,
    )
    assert queue == ()


def test_inactive_decisions_are_not_curated_even_with_historical_review_dates() -> None:
    predecessor = decision(1, review_on="2026-07-01")
    successor = decision(2, review_on="2026-08-01", supersedes=did(1))
    retired = decision(3, review_on="2026-07-01").model_copy(
        update={
            "retired_at": UtcTimestamp("2026-07-02T00:00:00.000Z"),
            "retire_note": "retired",
        }
    )

    queue = curation_queue(
        state(decisions=(predecessor, successor, retired)),
        now=UtcTimestamp("2026-07-10T00:00:00.000Z"),
        timezone=IanaTimezone("UTC"),
        config=CONFIG,
    )

    assert queue == ()


def test_decision_curation_qualifies_derived_state_by_store() -> None:
    local = decision(1, review_on="2026-07-01")
    remote_with_same_id = local.model_copy(
        update={
            "retired_at": UtcTimestamp("2026-07-02T00:00:00.000Z"),
            "retire_note": "retired remotely",
        }
    )
    graph = GraphState(
        tasks=(),
        decisions=(
            DecisionNode(STORE, "decisions/local.md", local),
            DecisionNode(
                StoreId("sto_019f0000000070008000000000000001"),
                "decisions/remote.md",
                remote_with_same_id,
            ),
        ),
        completeness=GraphCompleteness(complete=True),
    )

    queue = curation_queue(
        graph,
        now=UtcTimestamp("2026-07-10T00:00:00.000Z"),
        timezone=IanaTimezone("UTC"),
        config=CONFIG,
    )

    assert [entry.item_id for entry in queue] == [did(1)]


def test_due_sort_is_date_task_before_decision_then_task_priority_rank_or_decision_title() -> None:
    tasks = (
        task(1, TaskStage.PLANNED, review_on="2026-07-10", priority=TaskPriority.LOW),
        task(
            2,
            TaskStage.PLANNED,
            review_on="2026-07-10",
            priority=TaskPriority.CRITICAL,
            rank=2000,
        ),
        task(
            3,
            TaskStage.PLANNED,
            review_on="2026-07-10",
            priority=TaskPriority.CRITICAL,
            rank=1000,
        ),
    )
    decisions = (
        decision(1, review_on="2026-07-10", title="Zulu"),
        decision(2, review_on="2026-07-10", title="Alpha"),
    )

    queue = curation_queue(
        state(tasks, decisions),
        now=UtcTimestamp("2026-07-10T23:59:59.000Z"),
        timezone=IanaTimezone("UTC"),
        config=CONFIG,
    )

    assert [(entry.kind, entry.item_id.root) for entry in queue] == [
        (CurationKind.TASK, tid(3).root),
        (CurationKind.TASK, tid(2).root),
        (CurationKind.TASK, tid(1).root),
        (CurationKind.DECISION, did(2).root),
        (CurationKind.DECISION, did(1).root),
    ]
