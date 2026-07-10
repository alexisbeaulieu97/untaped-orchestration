from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from untaped_orchestration.domain.ids import TaskId
from untaped_orchestration.domain.models import ActiveTask, TaskPriority, TaskStage

MAX_RANK = 2**63 - 1
RANK_STEP = 1000


@dataclass(frozen=True, slots=True)
class RankReplacement:
    task_id: TaskId
    old_rank: int
    new_rank: int


@dataclass(frozen=True, slots=True)
class PlacementPlan:
    rebalance: tuple[RankReplacement, ...]
    primary: ActiveTask


@dataclass(frozen=True, slots=True)
class RankScope:
    parent: TaskId | None
    stage: TaskStage


class PlacementAnchorKind(StrEnum):
    FIRST = "first"
    LAST = "last"
    BEFORE = "before"
    AFTER = "after"


@dataclass(frozen=True, slots=True)
class PlacementAnchor:
    kind: PlacementAnchorKind
    task_id: TaskId | None = None

    def __post_init__(self) -> None:
        relative = self.kind in {PlacementAnchorKind.BEFORE, PlacementAnchorKind.AFTER}
        if relative != (self.task_id is not None):
            raise ValueError("before/after anchors require one task ID; first/last forbid it")


def _in_scope(task: ActiveTask, scope: RankScope) -> bool:
    return task.parent == scope.parent and task.stage is scope.stage


def _ordered(values: list[ActiveTask]) -> list[ActiveTask]:
    return sorted(values, key=lambda value: (value.rank, value.id.root))


def _insertion_index(
    members: list[ActiveTask], primary: ActiveTask, anchor: PlacementAnchor
) -> int:
    if anchor.task_id == primary.id:
        raise ValueError("a primary task cannot be placed relative to itself")
    if anchor.kind is PlacementAnchorKind.FIRST:
        return 0
    if anchor.kind is PlacementAnchorKind.LAST:
        return len(members)
    assert anchor.task_id is not None
    try:
        index = next(index for index, value in enumerate(members) if value.id == anchor.task_id)
    except StopIteration as error:
        raise ValueError("placement anchor must belong to the target scope") from error
    return index if anchor.kind is PlacementAnchorKind.BEFORE else index + 1


def _candidate_rank(members: list[ActiveTask], index: int) -> int | None:
    previous = members[index - 1].rank if index else None
    following = members[index].rank if index < len(members) else None
    if previous is None and following is None:
        return RANK_STEP
    if previous is None:
        assert following is not None
        candidate = following // 2
        return candidate if candidate > 0 else None
    if following is None:
        candidate = previous + RANK_STEP
        return candidate if candidate <= MAX_RANK else None
    candidate = previous + (following - previous) // 2
    return candidate if previous < candidate < following else None


def _neutral_replacements(members: list[ActiveTask]) -> tuple[RankReplacement, ...]:
    if len(members) > MAX_RANK // RANK_STEP:
        raise OverflowError("rank scope is too large for signed 64-bit sparse ranks")
    decreases: list[RankReplacement] = []
    increases: list[RankReplacement] = []
    for index, task in enumerate(members, start=1):
        new_rank = index * RANK_STEP
        if new_rank < task.rank:
            decreases.append(RankReplacement(task.id, task.rank, new_rank))
        elif new_rank > task.rank:
            increases.append(RankReplacement(task.id, task.rank, new_rank))
    return (*decreases, *reversed(increases))


def plan_placement(
    tasks: Sequence[ActiveTask],
    primary: ActiveTask,
    target: RankScope,
    anchor: PlacementAnchor,
) -> PlacementPlan:
    ids = [value.id.root for value in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("placement input task IDs must be unique")
    if primary.id.root not in ids:
        raise ValueError("primary task must be present in placement input")
    target_members = _ordered([value for value in tasks if _in_scope(value, target)])
    ranks = [value.rank for value in target_members]
    if len(ranks) != len(set(ranks)):
        raise ValueError("target scope ranks must be unique")
    without_primary = [value for value in target_members if value.id != primary.id]
    index = _insertion_index(without_primary, primary, anchor)
    rank = _candidate_rank(without_primary, index)
    if rank is not None:
        return PlacementPlan(
            (),
            primary.model_copy(
                update={"parent": target.parent, "stage": target.stage, "rank": rank}
            ),
        )

    replacements = _neutral_replacements(target_members)
    replacement_ranks = {value.task_id: value.new_rank for value in replacements}
    neutral_members = [
        value.model_copy(update={"rank": replacement_ranks.get(value.id, value.rank)})
        for value in target_members
    ]
    neutral_primary = primary.model_copy(
        update={"rank": replacement_ranks.get(primary.id, primary.rank)}
    )
    neutral_without_primary = [value for value in neutral_members if value.id != primary.id]
    index = _insertion_index(neutral_without_primary, neutral_primary, anchor)
    rank = _candidate_rank(neutral_without_primary, index)
    if rank is None:
        raise OverflowError("neutral sparse rank scope cannot represent requested placement")
    return PlacementPlan(
        replacements,
        neutral_primary.model_copy(
            update={"parent": target.parent, "stage": target.stage, "rank": rank}
        ),
    )


_PRIORITY_ORDER = {
    TaskPriority.CRITICAL: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.NORMAL: 2,
    TaskPriority.LOW: 3,
}


def sort_tasks(tasks: Sequence[ActiveTask]) -> tuple[ActiveTask, ...]:
    by_id = {value.id: value for value in tasks}
    if len(by_id) != len(tasks):
        raise ValueError("task identities must be unique")

    def ancestor_ranks(task: ActiveTask) -> tuple[int, ...]:
        result: list[int] = []
        seen = {task.id}
        parent = task.parent
        while parent is not None:
            if parent in seen:
                raise ValueError("cannot globally order a containment cycle")
            seen.add(parent)
            parent_task = by_id.get(parent)
            if parent_task is None:
                raise ValueError("cannot globally order a task with a missing parent")
            result.append(parent_task.rank)
            parent = parent_task.parent
        return tuple(reversed(result))

    return tuple(
        sorted(
            tasks,
            key=lambda value: (
                _PRIORITY_ORDER[value.priority],
                ancestor_ranks(value),
                value.rank,
                value.id.root,
            ),
        )
    )
