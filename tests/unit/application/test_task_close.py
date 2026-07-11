from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from tests.unit.application.test_task_transition import create, state, transition_request
from untaped_orchestration.application.items import RevisionConflict
from untaped_orchestration.application.tasks import (
    CloseTaskRequest,
    RepairDuplicateRequest,
    TaskLifecycleConflict,
)
from untaped_orchestration.domain.ids import Slug, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    TaskOutcome,
    TaskStage,
)


def close_request(task, store_revision, outcome, **changes):
    values = dict(
        item_id=task.record.metadata.id,
        outcome=outcome,
        note="finished for a reason",
        expected_revision=task.record.revision,
        expected_store_revision=store_revision,
    )
    values.update(changes)
    return CloseTaskRequest(**values)


def test_close_and_repair_requests_are_typed_and_do_not_own_unrelated_fields() -> None:
    assert [field.name for field in fields(CloseTaskRequest)] == [
        "item_id",
        "outcome",
        "note",
        "expected_revision",
        "expected_store_revision",
        "successor_id",
        "expected_successor_revision",
    ]
    assert [field.name for field in fields(RepairDuplicateRequest)] == [
        "item_id",
        "expected_active_revision",
        "expected_archive_revision",
        "apply",
    ]


@pytest.mark.parametrize("outcome", list(TaskOutcome))
def test_all_close_outcomes_create_archive_shape_and_preserve_body_fields(
    tmp_path: Path, outcome: TaskOutcome
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor, waiting=())
    if outcome is TaskOutcome.CANCELLED:
        current = repository.load_local(location, headers_only=False)
        task = service.transition(
            transition_request(task, current.store_revision, TaskStage.PLANNED)
        )
        current = repository.load_local(location, headers_only=False)
        task = service.transition(
            transition_request(task, current.store_revision, TaskStage.IN_PROGRESS)
        )
    successor = None
    changes = {}
    if outcome is TaskOutcome.SUPERSEDED:
        successor = create(repository, location, scope, executor, suffix=2)
        changes = {
            "successor_id": successor.record.metadata.id,
            "expected_successor_revision": successor.record.revision,
        }
    current = repository.load_local(location, headers_only=False)
    result = service.close(close_request(task, current.store_revision, outcome, **changes))
    assert isinstance(result.record.metadata, ArchivedTask)
    assert result.record.metadata.closed_from is task.record.metadata.stage
    assert result.record.metadata.outcome is outcome
    assert result.record.metadata.close_note == "finished for a reason"
    assert result.record.metadata.title == task.record.metadata.title
    assert result.record.body == task.record.body
    assert result.record.path.parent == Path("archive/tasks")


def test_close_preconditions_reject_waiting_delivery_and_bad_successor_contract(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    waiting = create(repository, location, scope, executor, waiting=(Slug("alexis"),))
    current = repository.load_local(location, headers_only=False)
    with pytest.raises(TaskLifecycleConflict):
        service.close(close_request(waiting, current.store_revision, TaskOutcome.DELIVERED))
    with pytest.raises(TaskLifecycleConflict):
        service.close(
            close_request(
                waiting,
                current.store_revision,
                TaskOutcome.SUPERSEDED,
                successor_id=waiting.record.metadata.id,
                expected_successor_revision=waiting.record.revision,
            )
        )
    with pytest.raises(TaskLifecycleConflict):
        service.close(
            close_request(
                waiting,
                current.store_revision,
                TaskOutcome.DECLINED,
                successor_id=TaskId("tsk_019f0000000070008000000000000099"),
            )
        )


def test_exact_final_close_replays_after_active_deletion_but_divergence_conflicts(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor)
    current = repository.load_local(location, headers_only=False)
    request = close_request(task, current.store_revision, TaskOutcome.DECLINED)
    result = service.close(request)
    replay = service.close(request)
    assert replay.receipt.replayed
    path = location.real_root.joinpath(*result.record.path.parts)
    changed = result.record.metadata.model_copy(update={"title": "divergent archive"})
    path.write_bytes(repository.item_bytes(changed, result.record.body or b""))
    with pytest.raises(TaskLifecycleConflict):
        service.close(request)


def test_duplicate_repair_deletes_only_exact_semantic_active_projection(tmp_path: Path) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor)
    current = repository.load_local(location, headers_only=False)
    archived = service.close(close_request(task, current.store_revision, TaskOutcome.DECLINED))
    active_path = location.real_root.joinpath(*task.record.path.parts)
    active_path.parent.mkdir(exist_ok=True)
    active_path.write_bytes(repository.item_bytes(task.record.metadata, task.record.body or b""))
    duplicate = repository.load_local(location, headers_only=False)
    active = next(r for r in duplicate.records if isinstance(r.metadata, ActiveTask))
    archive = next(r for r in duplicate.records if isinstance(r.metadata, ArchivedTask))
    preview = service.repair_duplicate(
        RepairDuplicateRequest(task.record.metadata.id, active.revision, archive.revision, False)
    )
    assert not preview.receipt.applied
    repaired = service.repair_duplicate(
        RepairDuplicateRequest(task.record.metadata.id, active.revision, archive.revision, True)
    )
    assert repaired.receipt.canonical_applied
    assert not active_path.exists()
    assert archived.record.path == repaired.record.path


def test_duplicate_repair_refuses_divergent_active_copy(tmp_path: Path) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor)
    current = repository.load_local(location, headers_only=False)
    archived = service.close(close_request(task, current.store_revision, TaskOutcome.DECLINED))
    divergent = task.record.metadata.model_copy(update={"title": "different"})
    active_path = location.real_root.joinpath(*task.record.path.parts)
    active_path.parent.mkdir(exist_ok=True)
    active_path.write_bytes(repository.item_bytes(divergent, task.record.body or b""))
    duplicate = repository.load_local(location, headers_only=False)
    active = next(r for r in duplicate.records if isinstance(r.metadata, ActiveTask))
    with pytest.raises(TaskLifecycleConflict):
        service.repair_duplicate(
            RepairDuplicateRequest(
                task.record.metadata.id, active.revision, archived.record.revision, True
            )
        )


@pytest.mark.parametrize(
    "outcome",
    [TaskOutcome.DELIVERED, TaskOutcome.DECLINED, TaskOutcome.CANCELLED],
)
def test_fresh_ordinary_close_always_requires_exact_store_revision(
    tmp_path: Path,
    outcome: TaskOutcome,
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor, suffix=1)
    if outcome is TaskOutcome.CANCELLED:
        current = repository.load_local(location, headers_only=False)
        task = service.transition(
            transition_request(task, current.store_revision, TaskStage.PLANNED)
        )
        current = repository.load_local(location, headers_only=False)
        task = service.transition(
            transition_request(task, current.store_revision, TaskStage.IN_PROGRESS)
        )
    guarded = repository.load_local(location, headers_only=False)
    create(repository, location, scope, executor, suffix=2)

    with pytest.raises(RevisionConflict):
        service.close(close_request(task, guarded.store_revision, outcome))


def test_final_close_replay_refuses_unrelated_store_divergence(tmp_path: Path) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor, suffix=1)
    guarded = repository.load_local(location, headers_only=False)
    request = close_request(task, guarded.store_revision, TaskOutcome.DECLINED)
    service.close(request)
    create(repository, location, scope, executor, suffix=2)

    with pytest.raises(RevisionConflict):
        service.close(request)
