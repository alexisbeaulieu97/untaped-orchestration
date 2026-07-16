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
type TaskIndex = dict[ItemKey, tuple[TaskNode, ...]]
type DecisionIndex = dict[ItemKey, tuple[DecisionNode, ...]]


@dataclass(frozen=True, slots=True)
class TaskRef:
    store_id: StoreId
    task_id: TaskId


@dataclass(frozen=True, slots=True)
class DecisionRef:
    store_id: StoreId
    decision_id: DecisionId


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


_BLOCKER_ORDER = {
    ReadinessBlockerKind.WAITING_PARTY: 0,
    ReadinessBlockerKind.DEPENDENCY_INVALID: 1,
    ReadinessBlockerKind.DEPENDENCY_UNKNOWN: 2,
    ReadinessBlockerKind.DEPENDENCY_ACTIVE: 3,
    ReadinessBlockerKind.DEPENDENCY_UNSATISFIED: 4,
    ReadinessBlockerKind.DESCENDANT_ACTIVE: 5,
    ReadinessBlockerKind.DESCENDANT_UNDELIVERED: 6,
    ReadinessBlockerKind.FEDERATION_INCOMPLETE: 7,
}


@dataclass(frozen=True, slots=True)
class ReadinessBlocker:
    kind: ReadinessBlockerKind
    related_task: TaskRef | None = None
    waiting_party: Slug | None = None
    missing_store_ids: tuple[StoreId, ...] = ()

    def __post_init__(self) -> None:
        has_task = self.related_task is not None
        has_party = self.waiting_party is not None
        has_missing_stores = bool(self.missing_store_ids)
        if self.kind is ReadinessBlockerKind.WAITING_PARTY:
            if not has_party or has_task or has_missing_stores:
                raise ValueError("waiting-party blockers require exactly one waiting party")
        elif self.kind is ReadinessBlockerKind.FEDERATION_INCOMPLETE:
            if has_task or has_party:
                raise ValueError("federation blockers do not identify an item")
            if not has_missing_stores:
                raise ValueError("federation blockers require missing store IDs")
            roots = [value.root for value in self.missing_store_ids]
            if roots != sorted(set(roots)):
                raise ValueError("missing store IDs must be sorted unique values")
        elif not has_task or has_party:
            raise ValueError("task blockers require exactly one related task reference")
        elif has_missing_stores:
            raise ValueError("only federation blockers carry missing store IDs")


@dataclass(frozen=True, slots=True)
class Readiness:
    task: TaskRef
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


def _task_index(graph: GraphState) -> TaskIndex:
    grouped: dict[ItemKey, list[TaskNode]] = {}
    for node in graph.tasks:
        grouped.setdefault(_task_key(node), []).append(node)
    return {key: tuple(sorted(nodes, key=lambda node: node.path)) for key, nodes in grouped.items()}


def _decision_index(graph: GraphState) -> DecisionIndex:
    grouped: dict[ItemKey, list[DecisionNode]] = {}
    for node in graph.decisions:
        grouped.setdefault(_decision_key(node), []).append(node)
    return {key: tuple(sorted(nodes, key=lambda node: node.path)) for key, nodes in grouped.items()}


def _incoming_supersession(
    graph: GraphState,
) -> tuple[dict[ItemKey, list[TaskNode]], dict[ItemKey, list[DecisionNode]]]:
    tasks = _task_index(graph)
    decisions = _decision_index(graph)
    task_incoming: dict[ItemKey, list[TaskNode]] = {}
    decision_incoming: dict[ItemKey, list[DecisionNode]] = {}
    for task_node in graph.tasks:
        if len(tasks[_task_key(task_node)]) != 1:
            continue
        for value in task_node.task.links:
            target_key = (value.target_store_id.root, value.target.root)
            if (
                value.relation is LinkRelation.SUPERSEDES
                and isinstance(value.target, TaskId)
                and value.target_store_id == task_node.store_id
                and len(tasks.get(target_key, ())) == 1
            ):
                task_incoming.setdefault(target_key, []).append(task_node)
    for decision_node in graph.decisions:
        if len(decisions[_decision_key(decision_node)]) != 1:
            continue
        for value in decision_node.decision.links:
            target_key = (value.target_store_id.root, value.target.root)
            if (
                value.relation is LinkRelation.SUPERSEDES
                and value.target_store_id == decision_node.store_id
                and len(decisions.get(target_key, ())) == 1
            ):
                decision_incoming.setdefault(target_key, []).append(decision_node)
    for task_successors in task_incoming.values():
        task_successors.sort(key=lambda node: node.path)
    for decision_successors in decision_incoming.values():
        decision_successors.sort(key=lambda node: node.path)
    return task_incoming, decision_incoming


def decision_state(decision: DecisionRef, graph: GraphState) -> DecisionState:
    matches = [
        node
        for node in graph.decisions
        if node.decision.id == decision.decision_id and node.store_id == decision.store_id
    ]
    if len(matches) != 1:
        raise ValueError(
            "decision does not resolve uniquely: "
            f"{decision.store_id.root}/{decision.decision_id.root}"
        )
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
    seen = {(node.task.id.root, node.path)}
    while pending:
        parent = pending.pop()
        children = sorted(
            (
                candidate
                for candidate in graph.tasks
                if candidate.store_id == node.store_id and candidate.task.parent == parent
            ),
            key=lambda candidate: (candidate.task.id.root, candidate.path),
        )
        for child in children:
            identity = (child.task.id.root, child.path)
            if identity in seen:
                continue
            seen.add(identity)
            descendants.append(child)
            pending.append(child.task.id)
    return tuple(sorted(descendants, key=lambda value: (value.task.id.root, value.path)))


def _blocker_sort_key(value: ReadinessBlocker) -> tuple[int, str]:
    identity = (
        f"{value.related_task.store_id.root}/{value.related_task.task_id.root}"
        if value.related_task is not None
        else value.waiting_party.root
        if value.waiting_party is not None
        else ""
    )
    return (_BLOCKER_ORDER[value.kind], identity)


def _readiness_for_node(node: TaskNode, graph: GraphState) -> Readiness:
    task = TaskRef(node.store_id, node.task.id)
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
        targets = tasks.get((value.target_store_id.root, value.target.root), ())
        if len(targets) != 1:
            blockers.append(
                ReadinessBlocker(
                    (
                        ReadinessBlockerKind.DEPENDENCY_UNKNOWN
                        if not graph.completeness.complete
                        and value.target_store_id.root not in known_store_ids
                        else ReadinessBlockerKind.DEPENDENCY_INVALID
                    ),
                    related_task=TaskRef(value.target_store_id, value.target),
                )
            )
        elif isinstance(targets[0].task, ActiveTask):
            target = targets[0]
            blockers.append(
                ReadinessBlocker(
                    ReadinessBlockerKind.DEPENDENCY_ACTIVE,
                    related_task=TaskRef(target.store_id, target.task.id),
                )
            )
        elif targets[0].task.outcome is not TaskOutcome.DELIVERED:
            target = targets[0]
            blockers.append(
                ReadinessBlocker(
                    ReadinessBlockerKind.DEPENDENCY_UNSATISFIED,
                    related_task=TaskRef(target.store_id, target.task.id),
                )
            )
    for descendant in _descendants(node, graph):
        if isinstance(descendant.task, ActiveTask):
            blockers.append(
                ReadinessBlocker(
                    ReadinessBlockerKind.DESCENDANT_ACTIVE,
                    related_task=TaskRef(descendant.store_id, descendant.task.id),
                )
            )
        elif descendant.task.outcome is not TaskOutcome.DELIVERED:
            blockers.append(
                ReadinessBlocker(
                    ReadinessBlockerKind.DESCENDANT_UNDELIVERED,
                    related_task=TaskRef(descendant.store_id, descendant.task.id),
                )
            )
    if not graph.completeness.complete:
        missing_store_ids = tuple(
            sorted(set(graph.completeness.missing_store_ids), key=lambda value: value.root)
        )
        blockers.append(
            ReadinessBlocker(
                ReadinessBlockerKind.FEDERATION_INCOMPLETE,
                missing_store_ids=missing_store_ids,
            )
        )
    unique = {(value.kind, value.related_task, value.waiting_party): value for value in blockers}
    return Readiness(task, tuple(sorted(unique.values(), key=_blocker_sort_key)))


def readiness(task: TaskRef, graph: GraphState) -> Readiness:
    matches = [
        node
        for node in graph.tasks
        if node.store_id == task.store_id and node.task.id == task.task_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"task does not resolve uniquely: {task.store_id.root}/{task.task_id.root}"
        )
    return _readiness_for_node(matches[0], graph)


def _finish_order(edges: dict[ItemKey, set[ItemKey]], nodes: set[ItemKey]) -> list[ItemKey]:
    visited: set[ItemKey] = set()
    finished: list[ItemKey] = []
    for start in sorted(nodes):
        if start in visited:
            continue
        pending: list[tuple[ItemKey, bool]] = [(start, False)]
        while pending:
            key, expanded = pending.pop()
            if expanded:
                finished.append(key)
                continue
            if key in visited:
                continue
            visited.add(key)
            pending.append((key, True))
            pending.extend(
                (target, False)
                for target in reversed(sorted(edges.get(key, ())))
                if target not in visited
            )
    return finished


def _reverse_edges(
    edges: dict[ItemKey, set[ItemKey]], nodes: set[ItemKey]
) -> dict[ItemKey, set[ItemKey]]:
    reverse_edges: dict[ItemKey, set[ItemKey]] = {key: set() for key in nodes}
    for source, targets in edges.items():
        for target in targets:
            reverse_edges[target].add(source)
    return reverse_edges


def _strong_components(
    finished: list[ItemKey], reverse_edges: dict[ItemKey, set[ItemKey]]
) -> list[frozenset[ItemKey]]:
    components: list[frozenset[ItemKey]] = []
    assigned: set[ItemKey] = set()
    for start in reversed(finished):
        if start in assigned:
            continue
        component: set[ItemKey] = set()
        pending_nodes = [start]
        assigned.add(start)
        while pending_nodes:
            key = pending_nodes.pop()
            component.add(key)
            for source in reversed(sorted(reverse_edges[key])):
                if source not in assigned:
                    assigned.add(source)
                    pending_nodes.append(source)
        components.append(frozenset(component))
    return components


def _cyclic_components(edges: dict[ItemKey, set[ItemKey]]) -> tuple[frozenset[ItemKey], ...]:
    nodes = set(edges)
    nodes.update(target for targets in edges.values() for target in targets)
    finished = _finish_order(edges, nodes)
    components = _strong_components(finished, _reverse_edges(edges, nodes))
    cyclic = (
        frozenset(component)
        for component in components
        if len(component) > 1 or (member := next(iter(component))) in edges.get(member, ())
    )
    return tuple(sorted(cyclic, key=lambda value: tuple(sorted(value))))


def _cycle_diagnostic(
    nodes: frozenset[ItemKey],
    index: Mapping[ItemKey, tuple[TaskNode | DecisionNode, ...]],
    *,
    field: str,
    message: str,
) -> Diagnostic | None:
    candidates = sorted(
        (node for key in nodes for node in index.get(key, ())),
        key=lambda node: node.path,
    )
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
    tasks: TaskIndex,
    decisions: DecisionIndex,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for task_nodes in tasks.values():
        if len(task_nodes) > 1:
            diagnostics.extend(
                _diagnostic(
                    task_node,
                    code="ORC003",
                    field="id",
                    message="task identity is duplicated within its store",
                    hint="Keep exactly one canonical item for each immutable ID.",
                )
                for task_node in task_nodes
            )
    for decision_nodes in decisions.values():
        if len(decision_nodes) > 1:
            diagnostics.extend(
                _diagnostic(
                    decision_node,
                    code="ORC003",
                    field="id",
                    message="decision identity is duplicated within its store",
                    hint="Keep exactly one canonical item for each immutable ID.",
                )
                for decision_node in decision_nodes
            )

    return diagnostics


def _rank_diagnostics(graph: GraphState) -> list[Diagnostic]:
    scopes: dict[tuple[str, str, str], dict[int, list[TaskNode]]] = {}
    for node in graph.tasks:
        if not isinstance(node.task, ActiveTask):
            continue
        scope = (
            node.store_id.root,
            node.task.parent.root if node.task.parent is not None else "",
            node.task.stage.value,
        )
        scopes.setdefault(scope, {}).setdefault(node.task.rank, []).append(node)

    diagnostics: list[Diagnostic] = []
    for scope in sorted(scopes):
        for rank in sorted(scopes[scope]):
            nodes = scopes[scope][rank]
            if len(nodes) > 1:
                diagnostics.extend(
                    _diagnostic(
                        node,
                        code="ORC004",
                        field="rank",
                        message="duplicate rank within task scope",
                        hint="Rebalance the exact parent and stage scope.",
                    )
                    for node in sorted(nodes, key=lambda value: value.path)
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
    complete: bool,
) -> Diagnostic | None:
    target_store_unknown = target_store_id.root not in known_stores
    if not complete and target_store_unknown:
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


def _task_parent_diagnostics(
    task_node: TaskNode,
    tasks: TaskIndex,
    edges: _Edges,
    *,
    source_unique: bool,
) -> list[Diagnostic]:
    parent_id = task_node.task.parent
    if parent_id is None:
        return []
    source = _task_key(task_node)
    parent_key = (task_node.store_id.root, parent_id.root)
    parents = tasks.get(parent_key, ())
    if source_unique and len(parents) == 1:
        edges.containment[source].add(parent_key)
        edges.precedence[source].add(parent_key)
    diagnostics: list[Diagnostic] = []
    if len(parents) > 1:
        diagnostics.append(
            _diagnostic(
                task_node,
                code="ORC004",
                field="parent",
                message="parent target is ambiguous within its store",
                hint="Keep one canonical parent item for the referenced ID.",
            )
        )
    if isinstance(task_node.task, ActiveTask) and (
        len(parents) != 1 or not isinstance(parents[0].task, ActiveTask)
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
    return diagnostics


def _task_link_targets(
    relation: LinkRelation,
    target_key: ItemKey,
    tasks: TaskIndex,
    decisions: DecisionIndex,
) -> tuple[TaskNode | DecisionNode, ...]:
    if relation is LinkRelation.GOVERNED_BY:
        return decisions.get(target_key, ())
    return tasks.get(target_key, ())


def _task_link_diagnostics(
    task_node: TaskNode,
    tasks: TaskIndex,
    decisions: DecisionIndex,
    edges: _Edges,
    *,
    source_unique: bool,
    known_stores: set[str],
    complete: bool,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    source = _task_key(task_node)
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
        targets = _task_link_targets(link.relation, target_key, tasks, decisions)
        if not targets:
            diagnostic = _missing_target_diagnostic(
                task_node,
                field=field,
                target_store_id=link.target_store_id,
                known_stores=known_stores,
                complete=complete,
            )
            if diagnostic is not None:
                diagnostics.append(diagnostic)
            continue
        if len(targets) > 1:
            diagnostics.append(
                _diagnostic(
                    task_node,
                    code="ORC004",
                    field=field,
                    message="relation target is ambiguous within its store",
                    hint="Keep one canonical target item for the referenced ID.",
                )
            )
            continue
        if link.relation is LinkRelation.DEPENDS_ON and source_unique:
            edges.dependencies[source].add(target_key)
            edges.precedence.setdefault(target_key, set()).add(source)
        elif link.relation is LinkRelation.SUPERSEDES:
            target = targets[0]
            if not isinstance(target, TaskNode):
                raise AssertionError("task relation resolved to a non-task node")
            if not isinstance(target.task, ArchivedTask) or (
                target.task.outcome is not TaskOutcome.SUPERSEDED
            ):
                diagnostics.append(
                    _diagnostic(
                        task_node,
                        code="ORC006",
                        field=field,
                        message=(
                            "task supersedes predecessor must be archived with outcome superseded"
                        ),
                        hint="Archive the predecessor through the guarded superseded flow.",
                    )
                )
            if source_unique:
                edges.task_supersession[source].add(target_key)
    return diagnostics


def _task_relation_diagnostics(
    graph: GraphState,
    tasks: TaskIndex,
    decisions: DecisionIndex,
    edges: _Edges,
) -> list[Diagnostic]:
    known_stores = {value.root for value in graph.completeness.known_store_ids}
    known_stores.update(task_node.store_id.root for task_node in graph.tasks)
    known_stores.update(decision_node.store_id.root for decision_node in graph.decisions)
    diagnostics: list[Diagnostic] = []
    for task_node in graph.tasks:
        source = _task_key(task_node)
        source_unique = len(tasks[source]) == 1
        if source_unique:
            edges.containment.setdefault(source, set())
            edges.dependencies.setdefault(source, set())
            edges.task_supersession.setdefault(source, set())
            edges.precedence.setdefault(source, set())
        diagnostics.extend(
            _task_parent_diagnostics(
                task_node,
                tasks,
                edges,
                source_unique=source_unique,
            )
        )
        diagnostics.extend(
            _task_link_diagnostics(
                task_node,
                tasks,
                decisions,
                edges,
                source_unique=source_unique,
                known_stores=known_stores,
                complete=graph.completeness.complete,
            )
        )
    return diagnostics


def _decision_relation_diagnostics(
    graph: GraphState,
    decisions: DecisionIndex,
    edges: _Edges,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for decision_node in graph.decisions:
        source = _decision_key(decision_node)
        source_unique = len(decisions[source]) == 1
        if source_unique:
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
            targets = decisions.get(target_key, ())
            if not targets:
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
            if len(targets) > 1:
                diagnostics.append(
                    _diagnostic(
                        decision_node,
                        code="ORC004",
                        field=field,
                        message="relation target is ambiguous within its store",
                        hint="Keep one canonical target item for the referenced ID.",
                    )
                )
                continue
            if source_unique:
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
    tasks: TaskIndex,
    decisions: DecisionIndex,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    specs: tuple[
        tuple[
            dict[ItemKey, set[ItemKey]],
            str,
            str,
            Mapping[ItemKey, tuple[TaskNode | DecisionNode, ...]],
        ],
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
        diagnostics.extend(
            diagnostic
            for component in _cyclic_components(relation_edges)
            if (
                diagnostic := _cycle_diagnostic(
                    component,
                    index,
                    field=field,
                    message=message,
                )
            )
            is not None
        )
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
    result = _readiness_for_node(node, graph)
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
        incoming = task_incoming.get(_task_key(task_node), ())
        if task_node.task.outcome is TaskOutcome.SUPERSEDED and len(incoming) != 1:
            diagnostics.append(
                _diagnostic(
                    task_node,
                    code="ORC006",
                    field="outcome",
                    message="superseded task requires exactly one valid successor",
                    hint="Use the guarded superseded-close flow with an active successor.",
                )
            )
    return diagnostics


def validate_graph(graph: GraphState) -> tuple[Diagnostic, ...]:
    tasks = _task_index(graph)
    decisions = _decision_index(graph)
    edges = _empty_edges()
    diagnostics = _duplicate_diagnostics(tasks, decisions)
    diagnostics.extend(_rank_diagnostics(graph))
    diagnostics.extend(_task_relation_diagnostics(graph, tasks, decisions, edges))
    diagnostics.extend(_decision_relation_diagnostics(graph, decisions, edges))
    diagnostics.extend(_cardinality_diagnostics(graph))
    diagnostics.extend(_cycle_diagnostics(edges, tasks, decisions))
    diagnostics.extend(_decision_lifecycle_diagnostics(graph))
    diagnostics.extend(_archive_lifecycle_diagnostics(graph))

    return sort_diagnostics(diagnostics)
