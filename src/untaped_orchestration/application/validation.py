from __future__ import annotations

from untaped_orchestration.application.results import FederatedSnapshot, LoadedRecord, StoreSnapshot
from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics
from untaped_orchestration.domain.graph import (
    DecisionNode,
    DecisionState,
    GraphCompleteness,
    GraphState,
    TaskNode,
    decision_state,
    validate_graph,
)
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    LinkRelation,
    Visibility,
)


def _record_path(store: StoreSnapshot, record: LoadedRecord) -> str:
    return store.location.root.joinpath(*record.path.parts).as_posix()


def _diagnostic(
    *,
    code: str,
    severity: str = "error",
    path: str,
    field: str,
    message: str,
    hint: str,
) -> Diagnostic:
    return Diagnostic.model_validate(
        {
            "code": code,
            "severity": severity,
            "path": path,
            "field": field,
            "message": message,
            "hint": hint,
        }
    )


def _graph_state(snapshot: FederatedSnapshot) -> GraphState:
    tasks: list[TaskNode] = []
    decisions: list[DecisionNode] = []
    known_store_ids = []
    for store in snapshot.stores:
        if store.store is None:
            continue
        known_store_ids.append(store.store.id)
        for record in store.records:
            path = _record_path(store, record)
            if isinstance(record.metadata, (ActiveTask, ArchivedTask)):
                tasks.append(TaskNode(store.store.id, path, record.metadata))
            elif isinstance(record.metadata, Decision):
                decisions.append(DecisionNode(store.store.id, path, record.metadata))
    return GraphState(
        tasks=tuple(tasks),
        decisions=tuple(decisions),
        completeness=GraphCompleteness(
            complete=snapshot.completeness.complete,
            missing_store_ids=tuple(
                entry.expected_store_id for entry in snapshot.completeness.entries
            ),
            known_store_ids=tuple(known_store_ids),
        ),
    )


def _policy_diagnostics(snapshot: FederatedSnapshot, graph: GraphState) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    tasks_by_store: dict[str, list[TaskNode]] = {}
    decisions_by_store: dict[str, list[DecisionNode]] = {}
    for task_node in graph.tasks:
        tasks_by_store.setdefault(task_node.store_id.root, []).append(task_node)
    for decision_node in graph.decisions:
        decisions_by_store.setdefault(decision_node.store_id.root, []).append(decision_node)

    for store in snapshot.stores:
        config = store.store
        if config is None:
            continue
        active = [
            task_node
            for task_node in tasks_by_store.get(config.id.root, ())
            if isinstance(task_node.task, ActiveTask)
        ]
        for task_node in active:
            if config.visibility is Visibility.PUBLIC:
                diagnostics.append(
                    _diagnostic(
                        code="ORC009",
                        path=task_node.path,
                        field="kind",
                        message="public stores are decision-only and cannot contain active tasks",
                        hint="Move the task to a private task-capable store.",
                    )
                )
            if not config.capabilities.active_tasks:
                diagnostics.append(
                    _diagnostic(
                        code="ORC009",
                        path=task_node.path,
                        field="kind",
                        message="this store capability forbids active tasks",
                        hint="Move the task or enable active_tasks in a private store.",
                    )
                )

        local_decisions = {
            decision_node.decision.id: decision_node
            for decision_node in decisions_by_store.get(config.id.root, ())
        }
        for index, pin in enumerate(config.brief.pinned_decisions):
            pinned_node = local_decisions.get(pin)
            valid = pinned_node is not None
            if pinned_node is not None:
                try:
                    valid = decision_state(pin, graph, store_id=config.id) is DecisionState.ACTIVE
                except KeyError:
                    valid = False
            if not valid:
                diagnostics.append(
                    _diagnostic(
                        code="ORC006",
                        path=(store.location.root / "store.toml").as_posix(),
                        field=f"brief.pinned_decisions.{index}",
                        message="pinned decision must resolve to an active local decision",
                        hint="Replace or remove the missing or inactive decision pin.",
                    )
                )
    return diagnostics


def _inactive_governance_diagnostics(graph: GraphState) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    decisions = {(node.store_id.root, node.decision.id.root): node for node in graph.decisions}
    for node in graph.tasks:
        for index, link in enumerate(node.task.links):
            if link.relation is not LinkRelation.GOVERNED_BY:
                continue
            target = decisions.get((link.target_store_id.root, link.target.root))
            if target is None:
                continue
            try:
                state = decision_state(
                    target.decision.id,
                    graph,
                    store_id=target.store_id,
                )
            except KeyError:
                continue
            if state is DecisionState.ACTIVE:
                continue
            diagnostics.append(
                _diagnostic(
                    code="ORC004",
                    severity="warning",
                    path=node.path,
                    field=f"links.{index}",
                    message=f"governed-by points to an inactive decision ({state.value})",
                    hint="Point the task at the current ruling when one exists.",
                )
            )
    return diagnostics


def validate_snapshot(
    snapshot: FederatedSnapshot,
    *,
    require_children: bool,
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    for store in snapshot.stores:
        diagnostics.extend(store.load_diagnostics)
    for value in snapshot.completeness.diagnostics:
        if require_children and value.severity == "warning":
            diagnostics.append(value.model_copy(update={"severity": "error"}))
        else:
            diagnostics.append(value)
    graph = _graph_state(snapshot)
    diagnostics.extend(validate_graph(graph))
    diagnostics.extend(_policy_diagnostics(snapshot, graph))
    diagnostics.extend(_inactive_governance_diagnostics(graph))
    return sort_diagnostics(diagnostics)
