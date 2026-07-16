from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from untaped_orchestration.application.curation import AcknowledgeRequest, CurationService
from untaped_orchestration.application.decisions import (
    DecisionGuard,
    DecisionService,
    RetireDecisionRequest,
    SupersedeDecisionRequest,
)
from untaped_orchestration.application.item_relations import ChangeEvidence, ChangeLink
from untaped_orchestration.application.item_support import (
    CreateDecisionRequest,
    CreateTaskRequest,
    EvidenceRequest,
    LinkRequest,
    MutationExecutionScope,
    MutationScope,
    UpdateDecisionRequest,
    UpdateTaskRequest,
)
from untaped_orchestration.application.items import (
    CreateDecision,
    CreateTask,
    UpdateDecision,
    UpdateTask,
)
from untaped_orchestration.application.results import StoreLocation
from untaped_orchestration.application.tasks import TaskService, TransitionTaskRequest
from untaped_orchestration.domain.evidence import (
    EvidenceReference,
    EvidenceRelation,
)
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import LinkRelation, Revision, TaskPriority, TaskStage
from untaped_orchestration.domain.ordering import PlacementAnchor, PlacementAnchorKind

TASK = TaskId("tsk_019f0000000070008000000000000000")
OTHER_TASK = TaskId("tsk_019f0000000070008000000000000001")
DECISION = DecisionId("dec_019f0000000070008000000000000000")
OTHER_DECISION = DecisionId("dec_019f0000000070008000000000000001")
STORE = StoreId("sto_019f0000000070008000000000000000")
REVISION = Revision("sha256:" + "a" * 64)


class Routed(Exception):
    pass


class RejectingExecutor:
    def execute(self, **kwargs):
        del kwargs
        raise Routed


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 14, tzinfo=UTC)


type Action = Callable[[RejectingExecutor, MutationScope], None]


def _create_task(executor: RejectingExecutor, scope: MutationScope) -> None:
    CreateTask(executor, object(), object()).execute(  # type: ignore[arg-type]
        scope,
        CreateTaskRequest(TASK, "Task", b"", (), TaskPriority.NORMAL, (), REVISION),
    )


def _create_decision(executor: RejectingExecutor, scope: MutationScope) -> None:
    CreateDecision(executor, object(), object()).execute(  # type: ignore[arg-type]
        scope,
        CreateDecisionRequest(DECISION, "Decision", b"", (), REVISION),
    )


def _update_task(executor: RejectingExecutor, scope: MutationScope) -> None:
    UpdateTask(executor, object()).execute(  # type: ignore[arg-type]
        scope,
        UpdateTaskRequest(TASK, REVISION, title="Task"),
    )


def _update_decision(executor: RejectingExecutor, scope: MutationScope) -> None:
    UpdateDecision(executor, object()).execute(  # type: ignore[arg-type]
        scope,
        UpdateDecisionRequest(DECISION, REVISION, title="Decision"),
    )


def _change_link(executor: RejectingExecutor, scope: MutationScope) -> None:
    ChangeLink(executor, object()).add(  # type: ignore[arg-type]
        scope,
        LinkRequest(TASK, LinkRelation.DEPENDS_ON, STORE, OTHER_TASK, REVISION),
    )


def _task_evidence(executor: RejectingExecutor, scope: MutationScope) -> None:
    ChangeEvidence(executor, object()).add(  # type: ignore[arg-type]
        scope,
        EvidenceRequest(
            TASK,
            EvidenceRelation.TRACKED_BY,
            EvidenceReference("url:https://example.com/task"),
            REVISION,
        ),
    )


def _decision_evidence(executor: RejectingExecutor, scope: MutationScope) -> None:
    ChangeEvidence(executor, object()).add(  # type: ignore[arg-type]
        scope,
        EvidenceRequest(
            DECISION,
            EvidenceRelation.TRACKED_BY,
            EvidenceReference("url:https://example.com/decision"),
            REVISION,
        ),
    )


def _task_curation(executor: RejectingExecutor, scope: MutationScope) -> None:
    CurationService(executor, object(), FixedClock(), scope).acknowledge(  # type: ignore[arg-type]
        AcknowledgeRequest(TASK, REVISION)
    )


def _decision_curation(executor: RejectingExecutor, scope: MutationScope) -> None:
    CurationService(executor, object(), FixedClock(), scope).acknowledge(  # type: ignore[arg-type]
        AcknowledgeRequest(DECISION, REVISION)
    )


def _transition_task(executor: RejectingExecutor, scope: MutationScope) -> None:
    TaskService(executor, object(), object(), scope).transition(  # type: ignore[arg-type]
        TransitionTaskRequest(
            TASK,
            TaskStage.IN_PROGRESS,
            None,
            None,
            None,
            PlacementAnchor(PlacementAnchorKind.LAST),
            force_current=True,
        )
    )


def _retire_decision(executor: RejectingExecutor, scope: MutationScope) -> None:
    DecisionService(executor, object(), object(), scope).retire(  # type: ignore[arg-type]
        RetireDecisionRequest(DECISION, "Retired", None, None, force_current=True)
    )


def _supersede_decision(executor: RejectingExecutor, scope: MutationScope) -> None:
    DecisionService(executor, object(), object(), scope).supersede(  # type: ignore[arg-type]
        SupersedeDecisionRequest(
            OTHER_DECISION,
            "Successor",
            b"",
            (),
            (DecisionGuard(DECISION, None),),
            None,
            force_current=True,
        )
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (_create_task, "recursive"),
        (_create_decision, "recursive"),
        (_update_task, "recursive"),
        (_update_decision, "selected-local"),
        (_change_link, "recursive"),
        (_task_evidence, "recursive"),
        (_decision_evidence, "selected-local"),
        (_task_curation, "recursive"),
        (_decision_curation, "selected-local"),
        (_transition_task, "recursive"),
        (_retire_decision, "recursive"),
        (_supersede_decision, "recursive"),
    ],
)
def test_mutation_consumers_invoke_exactly_one_factory(
    action: Action,
    expected: str,
) -> None:
    location = StoreLocation(Path("/work/root"), Path("/work/root"))
    calls: list[str] = []

    def factory(name: str):
        def create() -> MutationExecutionScope:
            calls.append(name)
            return MutationExecutionScope(
                (location,),
                location,
                lambda: pytest.fail("rejecting executor must not load"),
            )

        return create

    scope = MutationScope(factory("recursive"), factory("selected-local"))

    with pytest.raises(Routed):
        action(RejectingExecutor(), scope)

    assert calls == [expected]
