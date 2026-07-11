from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from tests.unit.application.test_task_transition import Clock, create, state
from untaped_orchestration.application.curation import (
    AcknowledgeRequest,
    CurateNextRequest,
    CurationService,
    SnoozeRequest,
)
from untaped_orchestration.application.items import (
    CreateDecision,
    CreateDecisionRequest,
    MutationExecutionScope,
    MutationScope,
    RevisionConflict,
    UpdateTask,
    UpdateTaskRequest,
)
from untaped_orchestration.application.mutations import InvalidMutationState
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    IncompleteStore,
)
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.ids import DecisionId, StoreId
from untaped_orchestration.domain.models import Decision
from untaped_orchestration.domain.time import CalendarDate


def test_curation_requests_are_kind_agnostic_and_frozen() -> None:
    assert [field.name for field in fields(AcknowledgeRequest)] == ["item_id", "expected_revision"]
    assert [field.name for field in fields(SnoozeRequest)] == [
        "item_id",
        "until",
        "expected_revision",
    ]
    assert [field.name for field in fields(CurateNextRequest)] == ["local"]


def test_generic_acknowledge_and_snooze_route_tasks_and_decisions_without_caller_kind(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    task = create(repository, location, scope, executor)
    current = repository.load_local(location, headers_only=False)
    decision = CreateDecision(executor, repository, Clock()).execute(
        scope,
        CreateDecisionRequest(
            DecisionId("dec_019f0000000070008000000000000001"),
            "Decision",
            b"ruling",
            (),
            current.store_revision,
        ),
    )
    service = CurationService(executor, repository, Clock(), scope)
    snoozed_task = service.snooze(
        SnoozeRequest(task.record.metadata.id, CalendarDate("2026-07-20"), task.record.revision)
    )
    snoozed_decision = service.snooze(
        SnoozeRequest(
            decision.record.metadata.id,
            CalendarDate("2026-07-21"),
            decision.record.revision,
        )
    )
    acknowledged_task = service.acknowledge(
        AcknowledgeRequest(task.record.metadata.id, snoozed_task.record.revision)
    )
    acknowledged_decision = service.acknowledge(
        AcknowledgeRequest(decision.record.metadata.id, snoozed_decision.record.revision)
    )
    assert acknowledged_task.record.metadata.review_on is None
    assert acknowledged_decision.record.metadata.review_on is None
    assert acknowledged_task.record.metadata.reviewed_at is not None
    assert isinstance(acknowledged_decision.record.metadata, Decision)


def test_task_review_is_exact_alias_of_kind_aware_acknowledge(tmp_path: Path) -> None:
    repository, location, scope, executor, tasks = state(tmp_path)
    task = create(repository, location, scope, executor)
    result = tasks.review(AcknowledgeRequest(task.record.metadata.id, task.record.revision))
    assert result.record.metadata.reviewed_at is not None
    assert result.record.metadata.review_on is None


def test_curate_next_uses_each_store_context_and_local_scope(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    task = create(repository, location, scope, executor)
    service = CurationService(executor, repository, Clock(), scope)
    task = service.snooze(
        SnoozeRequest(task.record.metadata.id, CalendarDate("2026-07-11"), task.record.revision)
    )
    local = service.next(CurateNextRequest(local=True))
    recursive = service.next(CurateNextRequest(local=False))
    assert [entry.item_id for entry in local] == [task.record.metadata.id]
    assert recursive == local


def test_inactive_decision_curation_is_refused(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    current = repository.load_local(location, headers_only=False)
    decision = CreateDecision(executor, repository, Clock()).execute(
        scope,
        CreateDecisionRequest(
            DecisionId("dec_019f0000000070008000000000000001"),
            "Decision",
            b"",
            (),
            current.store_revision,
        ),
    )
    path = location.real_root.joinpath(*decision.record.path.parts)
    retired = decision.record.metadata.model_copy(
        update={"retired_at": decision.record.metadata.created_at, "retire_note": "done"}
    )
    path.write_bytes(repository.item_bytes(retired, b""))
    current = repository.load_local(location, headers_only=False)
    retired_record = next(r for r in current.records if isinstance(r.metadata, Decision))
    service = CurationService(executor, repository, Clock(), scope)
    with pytest.raises(ValueError, match="inactive"):
        service.acknowledge(AcknowledgeRequest(retired_record.metadata.id, retired_record.revision))


def test_decision_curation_uses_selected_local_scope_without_recursive_resolution(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    current = repository.load_local(location, headers_only=False)
    decision = CreateDecision(executor, repository, Clock()).execute(
        scope,
        CreateDecisionRequest(
            DecisionId("dec_019f0000000070008000000000000001"),
            "Decision",
            b"",
            (),
            current.store_revision,
        ),
    )

    def recursive_must_not_load():
        raise AssertionError("decision curation must stay selected-local")

    guarded_scope = MutationScope(
        MutationExecutionScope(
            scope.recursive.locations,
            scope.recursive.selected,
            recursive_must_not_load,
        ),
        scope.selected_local,
    )
    service = CurationService(executor, repository, Clock(), guarded_scope)
    acknowledged = service.acknowledge(
        AcknowledgeRequest(decision.record.metadata.id, decision.record.revision)
    )
    assert acknowledged.record.metadata.reviewed_at is not None


@pytest.mark.parametrize("reason", ["missing", "invalid", "timeout"])
def test_recursive_curate_next_fails_closed_but_local_ignores_unrelated_child_failure(
    tmp_path: Path,
    reason: str,
) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    task = create(repository, location, scope, executor)
    normal = CurationService(executor, repository, Clock(), scope)
    task = normal.snooze(
        SnoozeRequest(task.record.metadata.id, CalendarDate("2026-07-11"), task.record.revision)
    )
    child_id = StoreId("sto_019f0000000070008000000000000099")

    def incomplete_load() -> FederatedSnapshot:
        selected = repository.load_local(location, headers_only=False)
        incomplete = IncompleteStore(
            child_id,
            reason,  # type: ignore[arg-type]
            Diagnostic(
                code="ORC005",
                severity="error",
                path="registry.toml",
                field="children",
                message=f"child {reason}",
                hint="restore the child",
            ),
        )
        return FederatedSnapshot(selected, (selected,), Completeness((incomplete,)))

    recursive = MutationExecutionScope((location,), location, incomplete_load)
    service = CurationService(
        executor,
        repository,
        Clock(),
        MutationScope(recursive, scope.selected_local),
    )
    with pytest.raises(InvalidMutationState):
        service.next(CurateNextRequest())
    local = service.next(CurateNextRequest(local=True))
    assert [entry.item_id for entry in local] == [task.record.metadata.id]


def test_acknowledge_and_same_date_snooze_never_replay_stale_revision(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    task = create(repository, location, scope, executor)
    service = CurationService(executor, repository, Clock(), scope)
    snooze_request = SnoozeRequest(
        task.record.metadata.id,
        CalendarDate("2026-07-20"),
        task.record.revision,
    )
    snoozed = service.snooze(snooze_request)
    with pytest.raises(RevisionConflict):
        service.snooze(snooze_request)

    acknowledge_request = AcknowledgeRequest(
        snoozed.record.metadata.id,
        snoozed.record.revision,
    )
    acknowledged = service.acknowledge(acknowledge_request)
    assert acknowledged.record.metadata.reviewed_at is not None
    with pytest.raises(RevisionConflict):
        service.acknowledge(acknowledge_request)


def test_curation_stale_revision_conflicts_after_unrelated_item_divergence(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    task = create(repository, location, scope, executor)
    UpdateTask(executor, repository).execute(
        scope,
        UpdateTaskRequest(task.record.metadata.id, task.record.revision, title="diverged"),
    )
    service = CurationService(executor, repository, Clock(), scope)
    with pytest.raises(RevisionConflict):
        service.snooze(
            SnoozeRequest(
                task.record.metadata.id,
                CalendarDate("2026-07-20"),
                task.record.revision,
            )
        )
