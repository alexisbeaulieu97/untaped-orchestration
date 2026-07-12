from __future__ import annotations

from dataclasses import replace

import pytest

from tests.builders import STORE_ID
from tests.unit.application.test_decision_lifecycle import (
    create_decision,
)
from tests.unit.application.test_decision_lifecycle import (
    service as decision_service,
)
from tests.unit.application.test_relations import _state as relation_state
from tests.unit.application.test_task_transition import create, state
from untaped_orchestration.application.curation import (
    AcknowledgeRequest,
    CurationService,
    SnoozeRequest,
)
from untaped_orchestration.application.decisions import (
    DecisionGuard,
    DecisionLifecycleConflict,
    RetireDecisionRequest,
    SupersedeDecisionRequest,
)
from untaped_orchestration.application.items import (
    ChangeEvidence,
    ChangeLink,
    RevisionConflict,
    UpdateDecision,
    UpdateDecisionRequest,
    UpdateTask,
    UpdateTaskRequest,
)
from untaped_orchestration.application.tasks import (
    CloseTaskRequest,
    MoveTaskRequest,
    TaskLifecycleConflict,
    TransitionTaskRequest,
)
from untaped_orchestration.domain.evidence import EvidenceReference, EvidenceRelation
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import LinkRelation, TaskOutcome, TaskStage
from untaped_orchestration.domain.ordering import PlacementAnchor, PlacementAnchorKind
from untaped_orchestration.domain.time import CalendarDate


def test_force_current_updates_read_item_revisions_inside_executor_lock(tmp_path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    task = create(repository, location, scope, executor)
    updated = UpdateTask(executor, repository).execute(
        scope,
        UpdateTaskRequest(task.record.metadata.id, None, force_current=True, title="Forced"),
    )
    assert updated.record.metadata.title == "Forced"

    decision = create_decision(repository, location, scope, executor, 1)
    clarified = UpdateDecision(executor, repository).execute(
        scope,
        UpdateDecisionRequest(
            decision.record.metadata.id,
            None,
            force_current=True,
            title="Forced ruling",
        ),
    )
    assert clarified.record.metadata.title == "Forced ruling"

    with pytest.raises(RevisionConflict, match="force-current"):
        UpdateTask(executor, repository).execute(
            scope,
            UpdateTaskRequest(
                task.record.metadata.id,
                updated.record.revision,
                force_current=True,
                title="Mixed",
            ),
        )


def test_force_current_transition_move_and_close_preserve_non_revision_invariants(tmp_path) -> None:
    repository, location, scope, executor, tasks = state(tmp_path)
    task = create(repository, location, scope, executor)
    transitioned = tasks.transition(
        TransitionTaskRequest(
            task.record.metadata.id,
            TaskStage.PLANNED,
            None,
            None,
            None,
            PlacementAnchor(PlacementAnchorKind.LAST),
            force_current=True,
        )
    )
    successor = create(repository, location, scope, executor, suffix=2)
    anchor = create(repository, location, scope, executor, suffix=3)
    moved = tasks.move(
        MoveTaskRequest(
            successor.record.metadata.id,
            None,
            None,
            None,
            None,
            PlacementAnchor(PlacementAnchorKind.BEFORE, anchor.record.metadata.id),
            expected_anchor_revision=None,
            force_current=True,
        )
    )
    assert moved.record.metadata.rank < anchor.record.metadata.rank
    with pytest.raises(TaskLifecycleConflict, match="parent"):
        tasks.move(
            MoveTaskRequest(
                task.record.metadata.id,
                None,
                TaskId("tsk_019f0000000070008000000000000099"),
                None,
                None,
                PlacementAnchor(PlacementAnchorKind.LAST),
                force_current=True,
            )
        )
    with pytest.raises(TaskLifecycleConflict, match="successor"):
        tasks.close(
            CloseTaskRequest(
                task.record.metadata.id,
                TaskOutcome.SUPERSEDED,
                "replaced",
                None,
                None,
                force_current=True,
            )
        )
    closed = tasks.close(
        CloseTaskRequest(
            task.record.metadata.id,
            TaskOutcome.SUPERSEDED,
            "replaced",
            None,
            None,
            successor_id=successor.record.metadata.id,
            expected_successor_revision=None,
            force_current=True,
        )
    )
    assert closed.record.metadata.id == transitioned.record.metadata.id


def test_force_current_decision_multi_item_keeps_exact_predecessor_set_and_lifecycle(
    tmp_path,
) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    first = create_decision(repository, location, scope, executor, 1)
    second = create_decision(repository, location, scope, executor, 2)
    decisions = decision_service(executor, repository, scope)
    request = SupersedeDecisionRequest(
        DecisionId("dec_019f0000000070008000000000000090"),
        "Replacement",
        b"body",
        (),
        (
            DecisionGuard(first.record.metadata.id, None),
            DecisionGuard(second.record.metadata.id, None),
        ),
        None,
        force_current=True,
    )
    result = decisions.supersede(request)
    assert {link.target for link in result.record.metadata.links} == {
        first.record.metadata.id,
        second.record.metadata.id,
    }
    with pytest.raises(DecisionLifecycleConflict, match="distinct"):
        decisions.supersede(
            replace(request, predecessors=(request.predecessors[0], request.predecessors[0]))
        )
    with pytest.raises(DecisionLifecycleConflict, match="note"):
        decisions.retire(
            RetireDecisionRequest(result.record.metadata.id, "", None, None, force_current=True)
        )


def test_force_current_relations_and_curation_skip_only_owner_revision(tmp_path) -> None:
    repository, _location, scope, executor, task, decision = relation_state(tmp_path)
    linked = ChangeLink(executor, repository).add(
        scope,
        __import__("untaped_orchestration.application.items", fromlist=["LinkRequest"]).LinkRequest(
            task.record.metadata.id,
            LinkRelation.GOVERNED_BY,
            StoreId(STORE_ID),
            decision.record.metadata.id,
            None,
            force_current=True,
        ),
    )
    assert linked.record.metadata.links
    evidenced = ChangeEvidence(executor, repository).add(
        scope,
        __import__(
            "untaped_orchestration.application.items", fromlist=["EvidenceRequest"]
        ).EvidenceRequest(
            task.record.metadata.id,
            EvidenceRelation.VERIFIED_BY,
            EvidenceReference("url:https://example.com/proof"),
            None,
            force_current=True,
        ),
    )
    with pytest.raises(Exception, match="already exists"):
        ChangeEvidence(executor, repository).add(
            scope,
            __import__(
                "untaped_orchestration.application.items", fromlist=["EvidenceRequest"]
            ).EvidenceRequest(
                task.record.metadata.id,
                EvidenceRelation.VERIFIED_BY,
                EvidenceReference("url:https://example.com/proof"),
                None,
                force_current=True,
            ),
        )
    curation = CurationService(
        executor,
        repository,
        __import__("tests.unit.application.test_task_transition", fromlist=["Clock"]).Clock(),
        scope,
    )
    snoozed = curation.snooze(
        SnoozeRequest(
            task.record.metadata.id,
            CalendarDate("2026-08-01"),
            None,
            force_current=True,
        )
    )
    acknowledged = curation.acknowledge(
        AcknowledgeRequest(
            task.record.metadata.id,
            None,
            force_current=True,
        )
    )
    assert evidenced.record.metadata.evidence
    assert snoozed.record.metadata.review_on is not None
    assert acknowledged.record.metadata.review_on is None
