from __future__ import annotations

import pytest

from untaped_orchestration.domain.graph import (
    DecisionNode,
    DecisionState,
    GraphCompleteness,
    GraphState,
    ReadinessBlockerKind,
    TaskNode,
    decision_state,
    readiness,
    validate_graph,
)
from untaped_orchestration.domain.ids import DecisionId, Slug, StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    Link,
    LinkRelation,
    TaskOutcome,
    TaskPriority,
    TaskStage,
)
from untaped_orchestration.domain.time import UtcTimestamp

STORE = StoreId("sto_019f0000000070008000000000000000")
OTHER_STORE = StoreId("sto_019f0000000070008000000000000001")
NOW = UtcTimestamp("2026-07-10T01:02:03.004Z")


def tid(number: int) -> TaskId:
    return TaskId(f"tsk_019f000000007000800000000000{number:04x}")


def did(number: int) -> DecisionId:
    return DecisionId(f"dec_019f000000007000800000000000{number:04x}")


def link(relation: LinkRelation, target: TaskId | DecisionId, store: StoreId = STORE) -> Link:
    return Link(relation=relation, target_store_id=store, target=target)


def active(
    number: int,
    *,
    parent: TaskId | None = None,
    links: tuple[Link, ...] = (),
    waiting: tuple[str, ...] = (),
    rank: int | None = None,
) -> ActiveTask:
    return ActiveTask(
        schema="untaped.orchestration.task/v1",
        id=tid(number),
        kind="task",
        title=f"Task {number}",
        created_at=NOW,
        tags=(),
        links=links,
        evidence=(),
        stage=TaskStage.INBOX,
        priority=TaskPriority.NORMAL,
        rank=rank or number * 1000,
        parent=parent,
        waiting_on=tuple(Slug(value) for value in waiting),
    )


def archived(
    number: int,
    outcome: TaskOutcome,
    *,
    parent: TaskId | None = None,
    links: tuple[Link, ...] = (),
    started: bool = False,
) -> ArchivedTask:
    return ArchivedTask(
        schema="untaped.orchestration.task/v1",
        id=tid(number),
        kind="task",
        title=f"Task {number}",
        created_at=NOW,
        tags=(),
        links=links,
        evidence=(),
        priority=TaskPriority.NORMAL,
        rank=number * 1000,
        parent=parent,
        started_at=NOW if started else None,
        waiting_on=(),
        closed_from=TaskStage.PLANNED,
        outcome=outcome,
        closed_at=NOW,
        close_note="closed",
    )


def decision(
    number: int,
    *,
    links: tuple[Link, ...] = (),
    retired: bool = False,
) -> Decision:
    return Decision(
        schema="untaped.orchestration.decision/v1",
        id=did(number),
        kind="decision",
        title=f"Decision {number}",
        created_at=NOW,
        tags=(),
        links=links,
        evidence=(),
        retired_at=NOW if retired else None,
        retire_note="retired" if retired else None,
    )


def task_node(task: ActiveTask | ArchivedTask, *, store: StoreId = STORE) -> TaskNode:
    directory = "tasks" if isinstance(task, ActiveTask) else "archive/tasks"
    return TaskNode(store_id=store, path=f"{directory}/{task.id.root}.md", task=task)


def decision_node(value: Decision, *, store: StoreId = STORE) -> DecisionNode:
    return DecisionNode(store_id=store, path=f"decisions/{value.id.root}.md", decision=value)


def graph(
    *tasks: ActiveTask | ArchivedTask,
    decisions: tuple[Decision, ...] = (),
    complete: bool = True,
) -> GraphState:
    return GraphState(
        tasks=tuple(task_node(task) for task in tasks),
        decisions=tuple(decision_node(value) for value in decisions),
        completeness=GraphCompleteness(complete=complete, known_store_ids=(STORE,)),
    )


@pytest.mark.parametrize(
    "tasks",
    [
        (active(1, parent=tid(1)),),
        (active(1, parent=tid(2)), active(2, parent=tid(1))),
        (
            active(1, parent=tid(2)),
            active(2, parent=tid(3)),
            active(3, parent=tid(1)),
        ),
    ],
)
def test_containment_matrix_rejects_every_child_owned_cycle(
    tasks: tuple[ActiveTask, ...],
) -> None:
    diagnostics = validate_graph(graph(*tasks))
    assert any(value.code == "ORC004" and value.field == "parent" for value in diagnostics)


def test_active_parent_must_resolve_to_an_active_same_store_task() -> None:
    child = active(1, parent=tid(2))

    missing = validate_graph(graph(child))
    archived_parent = validate_graph(graph(child, archived(2, TaskOutcome.DELIVERED)))

    assert any(value.field == "parent" and "active" in value.message for value in missing)
    assert any(value.field == "parent" and "active" in value.message for value in archived_parent)


def test_archived_tasks_preserve_historical_parent_ids_without_active_parent_requirement() -> None:
    parent = archived(1, TaskOutcome.DECLINED)
    child = archived(2, TaskOutcome.DECLINED, parent=tid(1))

    diagnostics = validate_graph(graph(parent, child))

    assert not any(value.field == "parent" for value in diagnostics)


def test_dependency_and_combined_completion_precedence_cycles_are_rejected() -> None:
    dependency_cycle = graph(
        active(1, links=(link(LinkRelation.DEPENDS_ON, tid(2)),)),
        active(2, links=(link(LinkRelation.DEPENDS_ON, tid(1)),)),
    )
    combined_cycle = graph(
        active(1, parent=tid(2), links=(link(LinkRelation.DEPENDS_ON, tid(2)),)),
        active(2),
    )

    dependency_diagnostics = validate_graph(dependency_cycle)
    combined_diagnostics = validate_graph(combined_cycle)

    assert any("dependency cycle" in value.message for value in dependency_diagnostics)
    assert any("completion-precedence cycle" in value.message for value in combined_diagnostics)


def test_supersession_enforces_kind_locality_cardinality_and_per_kind_cycles() -> None:
    task_cycle = graph(
        active(1, links=(link(LinkRelation.SUPERSEDES, tid(2)),)),
        active(2, links=(link(LinkRelation.SUPERSEDES, tid(1)),)),
    )
    task_predecessor = archived(3, TaskOutcome.SUPERSEDED)
    task_one = active(4, links=(link(LinkRelation.SUPERSEDES, tid(3)),))
    task_two = active(5, links=(link(LinkRelation.SUPERSEDES, tid(3)),))
    predecessor = decision(1)
    one = decision(2, links=(link(LinkRelation.SUPERSEDES, did(1)),))
    two = decision(3, links=(link(LinkRelation.SUPERSEDES, did(1)),))
    decision_cycle = graph(
        decisions=(
            decision(4, links=(link(LinkRelation.SUPERSEDES, did(5)),)),
            decision(5, links=(link(LinkRelation.SUPERSEDES, did(4)),)),
        )
    )
    task_cardinality = graph(task_predecessor, task_one, task_two)
    decision_cardinality = graph(decisions=(predecessor, one, two))

    assert any("task supersession cycle" in value.message for value in validate_graph(task_cycle))
    assert any(
        "decision supersession cycle" in value.message for value in validate_graph(decision_cycle)
    )
    assert any(
        "at most one successor" in value.message for value in validate_graph(task_cardinality)
    )
    assert any(
        "at most one successor" in value.message for value in validate_graph(decision_cardinality)
    )


@pytest.mark.parametrize(
    ("outcome", "expected_kind"),
    [
        (None, ReadinessBlockerKind.DEPENDENCY_ACTIVE),
        (TaskOutcome.DECLINED, ReadinessBlockerKind.DEPENDENCY_UNSATISFIED),
        (TaskOutcome.SUPERSEDED, ReadinessBlockerKind.DEPENDENCY_UNSATISFIED),
        (TaskOutcome.CANCELLED, ReadinessBlockerKind.DEPENDENCY_UNSATISFIED),
        (TaskOutcome.DELIVERED, None),
    ],
)
def test_readiness_covers_every_dependency_outcome(
    outcome: TaskOutcome | None,
    expected_kind: ReadinessBlockerKind | None,
) -> None:
    dependent = active(1, links=(link(LinkRelation.DEPENDS_ON, tid(2)),))
    prerequisite: ActiveTask | ArchivedTask = (
        active(2)
        if outcome is None
        else archived(2, outcome, started=outcome is TaskOutcome.CANCELLED)
    )

    result = readiness(tid(1), graph(dependent, prerequisite))

    if expected_kind is not None:
        assert expected_kind in {value.kind for value in result.blockers}
    assert result.ready is (expected_kind is None)


def test_readiness_keeps_missing_local_dependency_invalid_when_federation_is_incomplete() -> None:
    dependent = active(1, links=(link(LinkRelation.DEPENDS_ON, tid(2)),))

    complete = readiness(tid(1), graph(dependent))
    incomplete = readiness(tid(1), graph(dependent, complete=False))

    assert {value.kind for value in complete.blockers} == {ReadinessBlockerKind.DEPENDENCY_INVALID}
    assert {value.kind for value in incomplete.blockers} == {
        ReadinessBlockerKind.DEPENDENCY_INVALID,
        ReadinessBlockerKind.FEDERATION_INCOMPLETE,
    }


def test_readiness_includes_waiting_parties_and_all_descendant_states() -> None:
    parent = active(1, waiting=("alexis",))
    child = active(2, parent=tid(1))
    grandchild = archived(3, TaskOutcome.DECLINED, parent=tid(2))

    result = readiness(tid(1), graph(parent, child, grandchild))

    assert {value.kind for value in result.blockers} == {
        ReadinessBlockerKind.WAITING_PARTY,
        ReadinessBlockerKind.DESCENDANT_ACTIVE,
        ReadinessBlockerKind.DESCENDANT_UNDELIVERED,
    }


def test_decision_state_derives_active_superseded_and_retired_without_persisted_state() -> None:
    active_value = decision(1)
    successor = decision(2, links=(link(LinkRelation.SUPERSEDES, did(1)),))
    retired_value = decision(3, retired=True)
    state = graph(decisions=(active_value, successor, retired_value))

    assert decision_state(did(1), state) is DecisionState.SUPERSEDED
    assert decision_state(did(2), state) is DecisionState.ACTIVE
    assert decision_state(did(3), state) is DecisionState.RETIRED


def test_decision_cannot_be_both_retired_and_superseded() -> None:
    predecessor = decision(1, retired=True)
    successor = decision(2, links=(link(LinkRelation.SUPERSEDES, did(1)),))

    diagnostics = validate_graph(graph(decisions=(predecessor, successor)))

    assert any("cannot also be superseded" in value.message for value in diagnostics)


@pytest.mark.parametrize(
    "outcome",
    [TaskOutcome.DECLINED, TaskOutcome.SUPERSEDED, TaskOutcome.CANCELLED],
)
def test_non_delivered_archive_outcomes_require_every_descendant_archived(
    outcome: TaskOutcome,
) -> None:
    parent = archived(1, outcome, started=outcome is TaskOutcome.CANCELLED)
    child = active(2, parent=tid(1))

    diagnostics = validate_graph(graph(parent, child))

    assert any(
        value.field == "outcome" and "active descendants" in value.message for value in diagnostics
    )


def test_lifecycle_matrix_reports_archive_outcome_blockers() -> None:
    dependency = active(9)
    delivered = archived(
        1,
        TaskOutcome.DELIVERED,
        links=(link(LinkRelation.DEPENDS_ON, tid(9)),),
    )
    declined = archived(2, TaskOutcome.DECLINED)
    child = active(3, parent=tid(2))
    superseded = archived(4, TaskOutcome.SUPERSEDED)

    diagnostics = validate_graph(graph(delivered, dependency, declined, child, superseded))

    messages = "\n".join(value.message for value in diagnostics)
    assert "delivered task has unsatisfied dependencies" in messages
    assert "declined task has active descendants" in messages
    assert "superseded task has no successor" in messages


def test_graph_diagnostics_are_deterministic_under_input_permutation() -> None:
    first = active(1, parent=tid(99), links=(link(LinkRelation.DEPENDS_ON, tid(98)),))
    second = active(2, parent=tid(97))

    assert validate_graph(graph(first, second)) == validate_graph(graph(second, first))
