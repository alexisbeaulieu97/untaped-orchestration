from __future__ import annotations

import pytest

from untaped_orchestration.domain.ids import TaskId
from untaped_orchestration.domain.models import ActiveTask, TaskPriority, TaskStage
from untaped_orchestration.domain.ordering import (
    PlacementAnchor,
    PlacementAnchorKind,
    RankScope,
    plan_placement,
    sort_tasks,
)
from untaped_orchestration.domain.time import UtcTimestamp

NOW = UtcTimestamp("2026-07-10T01:02:03.004Z")
MAX_RANK = 2**63 - 1


def tid(number: int) -> TaskId:
    return TaskId(f"tsk_019f000000007000800000000000{number:04x}")


def task(
    number: int,
    rank: int,
    *,
    stage: TaskStage = TaskStage.INBOX,
    parent: TaskId | None = None,
    priority: TaskPriority = TaskPriority.NORMAL,
) -> ActiveTask:
    return ActiveTask(
        schema="untaped.orchestration.task/v1",
        id=tid(number),
        kind="task",
        title=f"Task {number}",
        created_at=NOW,
        tags=(),
        links=(),
        evidence=(),
        stage=stage,
        priority=priority,
        rank=rank,
        parent=parent,
        revisit_when="later" if stage is TaskStage.BACKLOG else None,
        waiting_on=(),
    )


def scope(*, parent: TaskId | None = None, stage: TaskStage = TaskStage.INBOX) -> RankScope:
    return RankScope(parent=parent, stage=stage)


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        (PlacementAnchor(PlacementAnchorKind.LAST), 3000),
        (PlacementAnchor(PlacementAnchorKind.FIRST), 500),
        (PlacementAnchor(PlacementAnchorKind.BEFORE, tid(2)), 1500),
        (PlacementAnchor(PlacementAnchorKind.AFTER, tid(1)), 1500),
    ],
)
def test_sparse_placement_uses_append_half_first_and_midpoint(
    anchor: PlacementAnchor,
    expected: int,
) -> None:
    first, second, primary = task(1, 1000), task(2, 2000), task(3, 7000)

    plan = plan_placement((first, second, primary), primary, scope(), anchor)

    assert plan.rebalance == ()
    assert plan.primary.rank == expected


def test_empty_scope_starts_at_one_thousand() -> None:
    primary = task(1, 5000, stage=TaskStage.BACKLOG)
    plan = plan_placement(
        (primary,),
        primary,
        scope(stage=TaskStage.INBOX),
        PlacementAnchor(PlacementAnchorKind.LAST),
    )
    assert plan.primary.rank == 1000


@pytest.mark.parametrize(
    ("ranks", "anchor"),
    [
        ((MAX_RANK - 1, MAX_RANK), PlacementAnchor(PlacementAnchorKind.LAST)),
        ((1, 2), PlacementAnchor(PlacementAnchorKind.FIRST)),
        ((1000, 1001), PlacementAnchor(PlacementAnchorKind.BEFORE, tid(2))),
    ],
)
def test_int64_boundaries_and_exhausted_gaps_force_neutral_rebalance(
    ranks: tuple[int, int],
    anchor: PlacementAnchor,
) -> None:
    first, second = task(1, ranks[0]), task(2, ranks[1])
    primary = task(3, 9000, stage=TaskStage.PLANNED)

    plan = plan_placement((first, second, primary), primary, scope(), anchor)

    assert plan.rebalance
    assert 0 < plan.primary.rank <= MAX_RANK


def test_same_scope_rebalance_includes_primary_neutrally_before_final_move() -> None:
    first, last, primary = task(1, 1000), task(3, 1001), task(2, 1002)

    plan = plan_placement(
        (first, last, primary),
        primary,
        scope(),
        PlacementAnchor(PlacementAnchorKind.BEFORE, tid(3)),
    )

    primary_replacement = next(value for value in plan.rebalance if value.task_id == tid(2))
    assert primary_replacement.new_rank == 3000
    assert plan.primary.rank == 1500


def test_rebalance_orders_decreases_first_to_last_and_increases_last_to_first() -> None:
    decreasing = [task(1, MAX_RANK - 2), task(2, MAX_RANK - 1), task(3, MAX_RANK)]
    increasing = [task(1, 1), task(2, 2), task(3, 3)]
    outsider = task(4, 9000, stage=TaskStage.PLANNED)

    down = plan_placement(
        (*decreasing, outsider),
        outsider,
        scope(),
        PlacementAnchor(PlacementAnchorKind.LAST),
    )
    up = plan_placement(
        (*increasing, outsider),
        outsider,
        scope(),
        PlacementAnchor(PlacementAnchorKind.FIRST),
    )

    assert [value.task_id for value in down.rebalance] == [tid(1), tid(2), tid(3)]
    assert [value.task_id for value in up.rebalance] == [tid(3), tid(2), tid(1)]


@pytest.mark.parametrize(
    ("original", "anchor"),
    [
        (
            [task(1, 1), task(2, 2), task(3, 3)],
            PlacementAnchor(PlacementAnchorKind.FIRST),
        ),
        (
            [task(1, MAX_RANK - 2), task(2, MAX_RANK - 1), task(3, MAX_RANK)],
            PlacementAnchor(PlacementAnchorKind.LAST),
        ),
    ],
)
def test_every_rebalance_interruption_preserves_unique_ranks_and_relative_order(
    original: list[ActiveTask],
    anchor: PlacementAnchor,
) -> None:
    primary = task(4, 9000, stage=TaskStage.PLANNED)
    plan = plan_placement((*original, primary), primary, scope(), anchor)

    for stop in range(len(plan.rebalance) + 1):
        ranks = {value.id: value.rank for value in original}
        for replacement in plan.rebalance[:stop]:
            assert ranks[replacement.task_id] == replacement.old_rank
            ranks[replacement.task_id] = replacement.new_rank
        ordered = sorted(original, key=lambda value: ranks[value.id])
        assert [value.id for value in ordered] == [value.id for value in original]
        assert len(set(ranks.values())) == len(ranks)


def test_anchor_must_be_in_target_scope_and_primary_cannot_anchor_itself() -> None:
    primary = task(1, 1000)
    other = task(2, 1000, stage=TaskStage.PLANNED)

    with pytest.raises(ValueError, match="target scope"):
        plan_placement(
            (primary, other),
            primary,
            scope(),
            PlacementAnchor(PlacementAnchorKind.BEFORE, tid(2)),
        )
    with pytest.raises(ValueError, match="itself"):
        plan_placement(
            (primary,),
            primary,
            scope(),
            PlacementAnchor(PlacementAnchorKind.AFTER, tid(1)),
        )


def test_global_order_is_priority_ancestor_vector_own_rank_then_id() -> None:
    parent_late = task(1, 2000, priority=TaskPriority.CRITICAL)
    child_late = task(2, 1, parent=tid(1), priority=TaskPriority.HIGH)
    parent_early = task(3, 1000, priority=TaskPriority.LOW)
    child_early_b = task(5, 1000, parent=tid(3), priority=TaskPriority.HIGH)
    child_early_a = task(4, 1000, parent=tid(3), priority=TaskPriority.HIGH)
    normal = task(6, 1, priority=TaskPriority.NORMAL)

    ordered = sort_tasks(
        (normal, child_late, child_early_b, parent_early, parent_late, child_early_a)
    )

    assert [value.id for value in ordered] == [
        tid(1),
        tid(4),
        tid(5),
        tid(2),
        tid(6),
        tid(3),
    ]
