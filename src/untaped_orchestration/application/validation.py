from __future__ import annotations

from untaped_orchestration.application.results import FederatedSnapshot, LoadedRecord, StoreSnapshot
from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics
from untaped_orchestration.domain.graph import (
    DecisionNode,
    DecisionRef,
    DecisionState,
    GraphCompleteness,
    GraphState,
    TaskNode,
    decision_state,
    validate_graph,
)
from untaped_orchestration.domain.ids import DecisionId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    LinkRelation,
    StoreConfig,
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


def _task_policy_diagnostics(config: StoreConfig, task_nodes: list[TaskNode]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for task_node in task_nodes:
        if config.visibility is Visibility.PUBLIC:
            diagnostics.append(
                _diagnostic(
                    code="ORC009",
                    path=task_node.path,
                    field="kind",
                    message="public stores are decision-only and cannot contain task records",
                    hint="Move the task to a private task-capable store.",
                )
            )
        if not config.capabilities.active_tasks:
            diagnostics.append(
                _diagnostic(
                    code="ORC009",
                    path=task_node.path,
                    field="kind",
                    message="this store capability forbids task records",
                    hint="Move the task or enable active_tasks in a private store.",
                )
            )
    return diagnostics


def _pin_diagnostics(
    store: StoreSnapshot,
    config: StoreConfig,
    decision_nodes: list[DecisionNode],
    graph: GraphState,
) -> list[Diagnostic]:
    local_decisions: dict[DecisionId, list[DecisionNode]] = {}
    for decision_node in decision_nodes:
        local_decisions.setdefault(decision_node.decision.id, []).append(decision_node)
    diagnostics: list[Diagnostic] = []
    for index, pin in enumerate(config.brief.pinned_decisions):
        valid = len(local_decisions.get(pin, ())) == 1
        if valid:
            try:
                valid = decision_state(DecisionRef(config.id, pin), graph) is DecisionState.ACTIVE
            except ValueError:
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


def _policy_diagnostics(snapshot: FederatedSnapshot, graph: GraphState) -> list[Diagnostic]:
    tasks_by_store: dict[str, list[TaskNode]] = {}
    decisions_by_store: dict[str, list[DecisionNode]] = {}
    for task_node in graph.tasks:
        tasks_by_store.setdefault(task_node.store_id.root, []).append(task_node)
    for decision_node in graph.decisions:
        decisions_by_store.setdefault(decision_node.store_id.root, []).append(decision_node)

    diagnostics: list[Diagnostic] = []
    for store in snapshot.stores:
        config = store.store
        if config is None:
            continue
        diagnostics.extend(_task_policy_diagnostics(config, tasks_by_store.get(config.id.root, [])))
        diagnostics.extend(
            _pin_diagnostics(
                store,
                config,
                decisions_by_store.get(config.id.root, []),
                graph,
            )
        )
    return diagnostics


def _inactive_governance_diagnostics(graph: GraphState) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    decisions: dict[tuple[str, str], list[DecisionNode]] = {}
    for decision_node in graph.decisions:
        decisions.setdefault(
            (decision_node.store_id.root, decision_node.decision.id.root), []
        ).append(decision_node)
    for task_node in graph.tasks:
        for index, link in enumerate(task_node.task.links):
            if link.relation is not LinkRelation.GOVERNED_BY:
                continue
            targets = decisions.get((link.target_store_id.root, link.target.root), ())
            if len(targets) != 1:
                continue
            target = targets[0]
            try:
                state = decision_state(DecisionRef(target.store_id, target.decision.id), graph)
            except ValueError:
                continue
            if state is DecisionState.ACTIVE:
                continue
            diagnostics.append(
                _diagnostic(
                    code="ORC004",
                    severity="warning",
                    path=task_node.path,
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
