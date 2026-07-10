from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics
from untaped_orchestration.domain.ids import DecisionId, Slug, StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    LinkRelation,
    TaskOutcome,
)

type TaskValue = ActiveTask | ArchivedTask
type ItemKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class TaskNode:
    store_id: StoreId
    path: str
    task: TaskValue


@dataclass(frozen=True, slots=True)
class DecisionNode:
    store_id: StoreId
    path: str
    decision: Decision


@dataclass(frozen=True, slots=True)
class GraphCompleteness:
    complete: bool
    missing_store_ids: tuple[StoreId, ...] = ()
    known_store_ids: tuple[StoreId, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphState:
    tasks: tuple[TaskNode, ...]
    decisions: tuple[DecisionNode, ...]
    completeness: GraphCompleteness


class DecisionState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class ReadinessBlockerKind(StrEnum):
    WAITING_PARTY = "waiting-party"
    DEPENDENCY_ACTIVE = "dependency-active"
    DEPENDENCY_UNSATISFIED = "dependency-unsatisfied"
    DEPENDENCY_INVALID = "dependency-invalid"
    DEPENDENCY_UNKNOWN = "dependency-unknown"
    DESCENDANT_ACTIVE = "descendant-active"
    DESCENDANT_UNDELIVERED = "descendant-undelivered"
    FEDERATION_INCOMPLETE = "federation-incomplete"


@dataclass(frozen=True, slots=True)
class ReadinessBlocker:
    kind: ReadinessBlockerKind
    related_task_id: TaskId | None = None
    waiting_party: Slug | None = None

    def __post_init__(self) -> None:
        has_task = self.related_task_id is not None
        has_party = self.waiting_party is not None
        if self.kind is ReadinessBlockerKind.WAITING_PARTY:
            if not has_party or has_task:
                raise ValueError("waiting-party blockers require exactly one waiting party")
        elif self.kind is ReadinessBlockerKind.FEDERATION_INCOMPLETE:
            if has_task or has_party:
                raise ValueError("federation blockers do not identify an item")
        elif not has_task or has_party:
            raise ValueError("task blockers require exactly one related task ID")


@dataclass(frozen=True, slots=True)
class Readiness:
    task_id: TaskId
    blockers: tuple[ReadinessBlocker, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers


def _task_key(node: TaskNode) -> ItemKey:
    return (node.store_id.root, node.task.id.root)


def _decision_key(node: DecisionNode) -> ItemKey:
    return (node.store_id.root, node.decision.id.root)


def _diagnostic(
    node: TaskNode | DecisionNode,
    *,
    code: str,
    severity: str = "error",
    field: str,
    message: str,
    hint: str,
) -> Diagnostic:
    return Diagnostic.model_validate(
        {
            "code": code,
            "severity": severity,
            "path": node.path,
            "field": field,
            "message": message,
            "hint": hint,
        }
    )


def _task_index(graph: GraphState) -> dict[ItemKey, TaskNode]:
    return {_task_key(node): node for node in graph.tasks}


def _decision_index(graph: GraphState) -> dict[ItemKey, DecisionNode]:
    return {_decision_key(node): node for node in graph.decisions}


def _incoming_supersession(
    graph: GraphState,
) -> tuple[dict[ItemKey, list[TaskNode]], dict[ItemKey, list[DecisionNode]]]:
    task_incoming: dict[ItemKey, list[TaskNode]] = {}
    decision_incoming: dict[ItemKey, list[DecisionNode]] = {}
    for task_node in graph.tasks:
        for value in task_node.task.links:
            if value.relation is LinkRelation.SUPERSEDES and isinstance(value.target, TaskId):
                task_incoming.setdefault(
                    (value.target_store_id.root, value.target.root), []
                ).append(task_node)
    for decision_node in graph.decisions:
        for value in decision_node.decision.links:
            if value.relation is LinkRelation.SUPERSEDES:
                decision_incoming.setdefault(
                    (value.target_store_id.root, value.target.root), []
                ).append(decision_node)
    return task_incoming, decision_incoming


def decision_state(
    decision_id: DecisionId,
    graph: GraphState,
    *,
    store_id: StoreId | None = None,
) -> DecisionState:
    matches = [
        node
        for node in graph.decisions
        if node.decision.id == decision_id and (store_id is None or node.store_id == store_id)
    ]
    if len(matches) != 1:
        raise KeyError(f"decision does not resolve uniquely: {decision_id.root}")
    node = matches[0]
    _, incoming = _incoming_supersession(graph)
    if incoming.get(_decision_key(node)):
        return DecisionState.SUPERSEDED
    if node.decision.retired_at is not None:
        return DecisionState.RETIRED
    return DecisionState.ACTIVE


def _descendants(node: TaskNode, graph: GraphState) -> tuple[TaskNode, ...]:
    descendants: list[TaskNode] = []
    pending = [node.task.id]
    seen = {node.task.id.root}
    while pending:
        parent = pending.pop()
        children = sorted(
            (
                candidate
                for candidate in graph.tasks
                if candidate.store_id == node.store_id and candidate.task.parent == parent
            ),
            key=lambda candidate: candidate.task.id.root,
        )
        for child in children:
            if child.task.id.root in seen:
                continue
            seen.add(child.task.id.root)
            descendants.append(child)
            pending.append(child.task.id)
    return tuple(sorted(descendants, key=lambda value: value.task.id.root))


def _blocker_sort_key(value: ReadinessBlocker) -> tuple[str, str]:
    identity = (
        value.related_task_id.root
        if value.related_task_id is not None
        else value.waiting_party.root
        if value.waiting_party is not None
        else ""
    )
    return (value.kind.value, identity)


def readiness(task_id: TaskId, graph: GraphState) -> Readiness:
    matches = [node for node in graph.tasks if node.task.id == task_id]
    if len(matches) != 1:
        raise KeyError(f"task does not resolve uniquely: {task_id.root}")
    node = matches[0]
    tasks = _task_index(graph)
    known_store_ids = {value.root for value in graph.completeness.known_store_ids}
    known_store_ids.update(value.store_id.root for value in graph.tasks)
    known_store_ids.update(value.store_id.root for value in graph.decisions)
    blockers: list[ReadinessBlocker] = []
    for party in node.task.waiting_on:
        blockers.append(ReadinessBlocker(ReadinessBlockerKind.WAITING_PARTY, waiting_party=party))
    for value in node.task.links:
        if value.relation is not LinkRelation.DEPENDS_ON or not isinstance(value.target, TaskId):
            continue
        target = tasks.get((value.target_store_id.root, value.target.root))
        if target is None:
            blockers.append(
                ReadinessBlocker(
                    (
                        ReadinessBlockerKind.DEPENDENCY_UNKNOWN
                        if not graph.completeness.complete
                        and value.target_store_id.root not in known_store_ids
                        else ReadinessBlockerKind.DEPENDENCY_INVALID
                    ),
                    related_task_id=value.target,
                )
            )
        elif isinstance(target.task, ActiveTask):
            blockers.append(
                ReadinessBlocker(
                    ReadinessBlockerKind.DEPENDENCY_ACTIVE,
                    related_task_id=target.task.id,
                )
            )
        elif target.task.outcome is not TaskOutcome.DELIVERED:
            blockers.append(
                ReadinessBlocker(
                    ReadinessBlockerKind.DEPENDENCY_UNSATISFIED,
                    related_task_id=target.task.id,
                )
            )
    for descendant in _descendants(node, graph):
        if isinstance(descendant.task, ActiveTask):
            blockers.append(
                ReadinessBlocker(
                    ReadinessBlockerKind.DESCENDANT_ACTIVE,
                    related_task_id=descendant.task.id,
                )
            )
        elif descendant.task.outcome is not TaskOutcome.DELIVERED:
            blockers.append(
                ReadinessBlocker(
                    ReadinessBlockerKind.DESCENDANT_UNDELIVERED,
                    related_task_id=descendant.task.id,
                )
            )
    if not graph.completeness.complete:
        blockers.append(ReadinessBlocker(ReadinessBlockerKind.FEDERATION_INCOMPLETE))
    unique = {(value.kind, value.related_task_id, value.waiting_party): value for value in blockers}
    return Readiness(task_id, tuple(sorted(unique.values(), key=_blocker_sort_key)))


def _cycle_nodes(edges: dict[ItemKey, set[ItemKey]]) -> set[ItemKey]:
    visiting: set[ItemKey] = set()
    visited: set[ItemKey] = set()
    cyclic: set[ItemKey] = set()

    def visit(key: ItemKey, trail: list[ItemKey]) -> None:
        if key in visiting:
            start = trail.index(key)
            cyclic.update(trail[start:])
            return
        if key in visited:
            return
        visiting.add(key)
        trail.append(key)
        for target in sorted(edges.get(key, ())):
            visit(target, trail)
        trail.pop()
        visiting.remove(key)
        visited.add(key)

    for key in sorted(edges):
        visit(key, [])
    return cyclic


def _cycle_diagnostic(
    nodes: set[ItemKey],
    index: Mapping[ItemKey, TaskNode | DecisionNode],
    *,
    field: str,
    message: str,
) -> Diagnostic | None:
    candidates = sorted((index[key] for key in nodes if key in index), key=lambda node: node.path)
    if not candidates:
        return None
    return _diagnostic(
        candidates[0],
        code="ORC004",
        field=field,
        message=message,
        hint="Remove or redirect a relation to break the cycle.",
    )


def _duplicate_diagnostics(
    graph: GraphState,
    tasks: dict[ItemKey, TaskNode],
    decisions: dict[ItemKey, DecisionNode],
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if len(tasks) != len(graph.tasks):
        for task_node in graph.tasks:
            if sum(_task_key(value) == _task_key(task_node) for value in graph.tasks) > 1:
                diagnostics.append(
                    _diagnostic(
                        task_node,
                        code="ORC003",
                        field="id",
                        message="task identity is duplicated within its store",
                        hint="Keep exactly one canonical item for each immutable ID.",
                    )
                )
    if len(decisions) != len(graph.decisions):
        for decision_node in graph.decisions:
            if (
                sum(
                    _decision_key(value) == _decision_key(decision_node)
                    for value in graph.decisions
                )
                > 1
            ):
                diagnostics.append(
                    _diagnostic(
                        decision_node,
                        code="ORC003",
                        field="id",
                        message="decision identity is duplicated within its store",
                        hint="Keep exactly one canonical item for each immutable ID.",
                    )
                )

    return diagnostics


@dataclass(slots=True)
class _Edges:
    containment: dict[ItemKey, set[ItemKey]]
    dependencies: dict[ItemKey, set[ItemKey]]
    task_supersession: dict[ItemKey, set[ItemKey]]
    decision_supersession: dict[ItemKey, set[ItemKey]]
    precedence: dict[ItemKey, set[ItemKey]]


def _empty_edges() -> _Edges:
    return _Edges({}, {}, {}, {}, {})


def _missing_target_diagnostic(
    node: TaskNode,
    *,
    field: str,
    target_store_id: StoreId,
    known_stores: set[str],
    missing_stores: set[str],
    complete: bool,
) -> Diagnostic | None:
    target_store_unknown = target_store_id.root not in known_stores
    if not complete and (target_store_unknown or target_store_id.root in missing_stores):
        return None
    return _diagnostic(
        node,
        code="ORC004",
        field=field,
        message=(
            "relation target store is missing from complete federation"
            if target_store_unknown
            else "relation target item does not exist"
        ),
        hint="Restore the target or remove the stale relation.",
    )


def _task_relation_diagnostics(
    graph: GraphState,
    tasks: dict[ItemKey, TaskNode],
    decisions: dict[ItemKey, DecisionNode],
    edges: _Edges,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    known_stores = {value.root for value in graph.completeness.known_store_ids}
    known_stores.update(task_node.store_id.root for task_node in graph.tasks)
    known_stores.update(decision_node.store_id.root for decision_node in graph.decisions)
    missing_stores = {value.root for value in graph.completeness.missing_store_ids}
    for task_node in graph.tasks:
        source = _task_key(task_node)
        edges.containment.setdefault(source, set())
        edges.dependencies.setdefault(source, set())
        edges.task_supersession.setdefault(source, set())
        edges.precedence.setdefault(source, set())
        if task_node.task.parent is not None:
            parent_key = (task_node.store_id.root, task_node.task.parent.root)
            edges.containment[source].add(parent_key)
            edges.precedence[source].add(parent_key)
            parent = tasks.get(parent_key)
            if isinstance(task_node.task, ActiveTask) and (
                parent is None or not isinstance(parent.task, ActiveTask)
            ):
                diagnostics.append(
                    _diagnostic(
                        task_node,
                        code="ORC004",
                        field="parent",
                        message="an active task parent must resolve to an active same-store task",
                        hint="Move the task below an active local parent or clear its parent.",
                    )
                )
        for position, link in enumerate(task_node.task.links):
            field = f"links.{position}"
            target_key = (link.target_store_id.root, link.target.root)
            if (
                link.relation in {LinkRelation.DEPENDS_ON, LinkRelation.SUPERSEDES}
                and link.target_store_id != task_node.store_id
            ):
                diagnostics.append(
                    _diagnostic(
                        task_node,
                        code="ORC004",
                        field=field,
                        message=f"{link.relation.value} is a same-store relation",
                        hint="Point the structural relation at an item in the source store.",
                    )
                )
                continue
            expected: Mapping[ItemKey, TaskNode | DecisionNode]
            expected = decisions if link.relation is LinkRelation.GOVERNED_BY else tasks
            target = expected.get(target_key)
            if target is None:
                diagnostic = _missing_target_diagnostic(
                    task_node,
                    field=field,
                    target_store_id=link.target_store_id,
                    known_stores=known_stores,
                    missing_stores=missing_stores,
                    complete=graph.completeness.complete,
                )
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
                continue
            if link.relation is LinkRelation.DEPENDS_ON:
                edges.dependencies[source].add(target_key)
                edges.precedence.setdefault(target_key, set()).add(source)
            elif link.relation is LinkRelation.SUPERSEDES:
                edges.task_supersession[source].add(target_key)
    return diagnostics


def _decision_relation_diagnostics(
    graph: GraphState,
    decisions: dict[ItemKey, DecisionNode],
    edges: _Edges,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for decision_node in graph.decisions:
        source = _decision_key(decision_node)
        edges.decision_supersession.setdefault(source, set())
        for position, link in enumerate(decision_node.decision.links):
            field = f"links.{position}"
            target_key = (link.target_store_id.root, link.target.root)
            if link.target_store_id != decision_node.store_id:
                diagnostics.append(
                    _diagnostic(
                        decision_node,
                        code="ORC004",
                        field=field,
                        message="supersedes is a same-store relation",
                        hint="Point the structural relation at a decision in the source store.",
                    )
                )
                continue
            if target_key not in decisions:
                diagnostics.append(
                    _diagnostic(
                        decision_node,
                        code="ORC004",
                        field=field,
                        message="relation target item does not exist",
                        hint="Restore the predecessor or remove the stale relation.",
                    )
                )
                continue
            edges.decision_supersession[source].add(target_key)
    return diagnostics


def _cardinality_diagnostics(graph: GraphState) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    task_incoming, decision_incoming = _incoming_supersession(graph)
    for task_successors in task_incoming.values():
        if len(task_successors) > 1:
            diagnostics.extend(
                _diagnostic(
                    successor,
                    code="ORC004",
                    field="links",
                    message="each predecessor has at most one successor",
                    hint="Keep one lifecycle-owned successor relation per predecessor.",
                )
                for successor in task_successors
            )
    for decision_successors in decision_incoming.values():
        if len(decision_successors) > 1:
            diagnostics.extend(
                _diagnostic(
                    successor,
                    code="ORC004",
                    field="links",
                    message="each predecessor has at most one successor",
                    hint="Keep one lifecycle-owned successor relation per predecessor.",
                )
                for successor in decision_successors
            )
    return diagnostics


def _cycle_diagnostics(
    edges: _Edges,
    tasks: dict[ItemKey, TaskNode],
    decisions: dict[ItemKey, DecisionNode],
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    specs: tuple[
        tuple[dict[ItemKey, set[ItemKey]], str, str, Mapping[ItemKey, TaskNode | DecisionNode]],
        ...,
    ] = (
        (edges.containment, "parent", "containment cycle detected", tasks),
        (edges.dependencies, "links", "dependency cycle detected", tasks),
        (edges.task_supersession, "links", "task supersession cycle detected", tasks),
        (
            edges.decision_supersession,
            "links",
            "decision supersession cycle detected",
            decisions,
        ),
        (edges.precedence, "links", "completion-precedence cycle detected", tasks),
    )
    for relation_edges, field, message, index in specs:
        diagnostic = _cycle_diagnostic(
            _cycle_nodes(relation_edges),
            index,
            field=field,
            message=message,
        )
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return diagnostics


def _decision_lifecycle_diagnostics(graph: GraphState) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    _, decision_incoming = _incoming_supersession(graph)
    for decision_node in graph.decisions:
        incoming = decision_incoming.get(_decision_key(decision_node), ())
        if incoming and decision_node.decision.retired_at is not None:
            diagnostics.append(
                _diagnostic(
                    decision_node,
                    code="ORC006",
                    field="retired_at",
                    message="a retired decision cannot also be superseded",
                    hint="Restore the decision to exactly one terminal lifecycle state.",
                )
            )
    return diagnostics


def _delivered_diagnostics(
    node: TaskNode,
    graph: GraphState,
    active_descendants: list[TaskNode],
    undelivered: list[TaskNode],
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    result = readiness(node.task.id, graph)
    dependency_blockers = {
        ReadinessBlockerKind.DEPENDENCY_ACTIVE,
        ReadinessBlockerKind.DEPENDENCY_UNSATISFIED,
        ReadinessBlockerKind.DEPENDENCY_INVALID,
        ReadinessBlockerKind.DEPENDENCY_UNKNOWN,
    }
    if any(blocker.kind in dependency_blockers for blocker in result.blockers):
        diagnostics.append(
            _diagnostic(
                node,
                code="ORC006",
                field="outcome",
                message="delivered task has unsatisfied dependencies",
                hint="Deliver every prerequisite before closing the dependent task.",
            )
        )
    if active_descendants or undelivered:
        diagnostics.append(
            _diagnostic(
                node,
                code="ORC006",
                field="outcome",
                message="delivered task requires every descendant to be delivered",
                hint="Finish and deliver every descendant first.",
            )
        )
    if not graph.completeness.complete:
        diagnostics.append(
            _diagnostic(
                node,
                code="ORC006",
                field="outcome",
                message="delivered closure requires complete federation",
                hint="Restore required child stores and revalidate the closure.",
            )
        )
    return diagnostics


def _archive_lifecycle_diagnostics(graph: GraphState) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    task_incoming, _ = _incoming_supersession(graph)
    for task_node in graph.tasks:
        if not isinstance(task_node.task, ArchivedTask):
            continue
        descendants = _descendants(task_node, graph)
        active_descendants = [value for value in descendants if isinstance(value.task, ActiveTask)]
        undelivered = [
            value
            for value in descendants
            if isinstance(value.task, ArchivedTask)
            and value.task.outcome is not TaskOutcome.DELIVERED
        ]
        if task_node.task.outcome is TaskOutcome.DELIVERED:
            diagnostics.extend(
                _delivered_diagnostics(task_node, graph, active_descendants, undelivered)
            )
        elif active_descendants:
            diagnostics.append(
                _diagnostic(
                    task_node,
                    code="ORC006",
                    field="outcome",
                    message=f"{task_node.task.outcome.value} task has active descendants",
                    hint="Archive every descendant before using this close outcome.",
                )
            )
        if task_node.task.outcome is TaskOutcome.SUPERSEDED and not task_incoming.get(
            _task_key(task_node)
        ):
            diagnostics.append(
                _diagnostic(
                    task_node,
                    code="ORC006",
                    field="outcome",
                    message="superseded task has no successor",
                    hint="Use the guarded superseded-close flow with an active successor.",
                )
            )
    return diagnostics


def validate_graph(graph: GraphState) -> tuple[Diagnostic, ...]:
    tasks = _task_index(graph)
    decisions = _decision_index(graph)
    edges = _empty_edges()
    diagnostics = _duplicate_diagnostics(graph, tasks, decisions)
    diagnostics.extend(_task_relation_diagnostics(graph, tasks, decisions, edges))
    diagnostics.extend(_decision_relation_diagnostics(graph, decisions, edges))
    diagnostics.extend(_cardinality_diagnostics(graph))
    diagnostics.extend(_cycle_diagnostics(edges, tasks, decisions))
    diagnostics.extend(_decision_lifecycle_diagnostics(graph))
    diagnostics.extend(_archive_lifecycle_diagnostics(graph))

    return sort_diagnostics(diagnostics)
