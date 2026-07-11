from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.builders import STORE_ID, TASK_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.items import (
    CreateDecision,
    CreateDecisionRequest,
    CreateTask,
    CreateTaskRequest,
    ItemStateConflict,
    MutationScope,
    RevisionConflict,
    UpdateDecision,
    UpdateDecisionRequest,
    UpdateTask,
    UpdateTaskRequest,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    IncompleteStore,
)
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.ids import DecisionId, Slug, StoreId, TaskId
from untaped_orchestration.domain.models import Revision, TaskPriority, TaskStage
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 11, tzinfo=UTC)


def _state(tmp_path: Path):
    target = tmp_path / "repository"
    target.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(target, STORE_ID, "Local", "UTC")
    )
    location = location_from_root(target / ".untaped" / "orchestration")

    def load() -> FederatedSnapshot:
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    scope = MutationScope((location,), location, load)
    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    return repository, location, scope, executor


def test_update_request_types_expose_exact_owned_fields_only() -> None:
    assert [field.name for field in fields(UpdateTaskRequest)] == [
        "item_id",
        "expected_revision",
        "title",
        "body",
        "priority",
        "tags",
        "waiting_on",
    ]
    assert [field.name for field in fields(UpdateDecisionRequest)] == [
        "item_id",
        "expected_revision",
        "title",
        "body",
        "tags",
    ]
    forbidden = {
        "parent",
        "rank",
        "stage",
        "revisit_when",
        "outcome",
        "supersedes",
        "reviewed_at",
        "review_on",
    }
    assert forbidden.isdisjoint(field.name for field in fields(UpdateTaskRequest))
    assert forbidden.isdisjoint(field.name for field in fields(UpdateDecisionRequest))


def test_task_update_replaces_only_owned_fields_and_keeps_creation_filename(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor = _state(tmp_path)
    before = repository.load_local(location, headers_only=False)
    created = CreateTask(executor, repository, Clock()).execute(
        scope,
        CreateTaskRequest(
            TaskId(TASK_ID),
            "Original slug",
            b"old body",
            (Slug("old"),),
            TaskPriority.HIGH,
            (Slug("alexis"),),
            before.store_revision,
        ),
    )

    updated = UpdateTask(executor, repository).execute(
        scope,
        UpdateTaskRequest(
            TaskId(TASK_ID),
            created.record.revision,
            title="Renamed completely",
            body=b"new\r\nbody",
            priority=TaskPriority.LOW,
            tags=(Slug("new"),),
            waiting_on=(),
        ),
    )

    task = updated.record.metadata
    assert task.title == "Renamed completely"
    assert updated.record.body == b"new\r\nbody"
    assert task.priority is TaskPriority.LOW
    assert task.tags == (Slug("new"),)
    assert task.waiting_on == ()
    assert task.stage is TaskStage.INBOX
    assert task.rank == 1000
    assert updated.record.path == created.record.path
    assert updated.receipt.views_current


def test_updates_require_one_change_and_exact_current_item_revision(tmp_path: Path) -> None:
    repository, location, scope, executor = _state(tmp_path)
    before = repository.load_local(location, headers_only=False)
    created = CreateTask(executor, repository, Clock()).execute(
        scope,
        CreateTaskRequest(
            TaskId(TASK_ID),
            "Task",
            b"",
            (),
            TaskPriority.NORMAL,
            (),
            before.store_revision,
        ),
    )
    service = UpdateTask(executor, repository)

    with pytest.raises(ValueError, match="at least one"):
        service.execute(scope, UpdateTaskRequest(TaskId(TASK_ID), created.record.revision))
    with pytest.raises(RevisionConflict):
        service.execute(
            scope,
            UpdateTaskRequest(
                TaskId(TASK_ID),
                Revision("sha256:" + "0" * 64),
                title="stale",
            ),
        )


def test_decision_update_allows_clarification_fields_and_preserves_lifecycle_fields(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor = _state(tmp_path)
    before = repository.load_local(location, headers_only=False)
    decision_id = DecisionId("dec_019f0000000070008000000000000001")
    created = CreateDecision(executor, repository, Clock()).execute(
        scope,
        CreateDecisionRequest(decision_id, "Original", b"body", (), before.store_revision),
    )

    updated = UpdateDecision(executor, repository).execute(
        scope,
        UpdateDecisionRequest(
            decision_id,
            created.record.revision,
            title="Clarified",
            body=b"clarification",
            tags=(Slug("clarified"),),
        ),
    )

    assert updated.record.metadata.title == "Clarified"
    assert updated.record.body == b"clarification"
    assert updated.record.metadata.tags == (Slug("clarified"),)
    assert updated.record.metadata.retired_at is None
    assert updated.record.metadata.reviewed_at is None
    assert updated.record.path == created.record.path


def test_task_update_refuses_archived_task(tmp_path: Path) -> None:
    repository, location, scope, executor = _state(tmp_path)
    before = repository.load_local(location, headers_only=False)
    created = CreateTask(executor, repository, Clock()).execute(
        scope,
        CreateTaskRequest(
            TaskId(TASK_ID), "Task", b"", (), TaskPriority.NORMAL, (), before.store_revision
        ),
    )
    raw = repository.read_file(location, created.record.path).content
    active = location.real_root.joinpath(*created.record.path.parts)
    archive = location.real_root / "archive" / "tasks" / created.record.path.name
    archive.parent.mkdir(parents=True)
    archive.write_bytes(
        raw.replace(b'stage = "inbox"\n', b'closed_from = "inbox"\noutcome = "declined"\n').replace(
            b"waiting_on = []\n",
            b'waiting_on = []\nclosed_at = "2026-07-11T00:00:00.000Z"\nclose_note = "done"\n',
        )
    )
    active.unlink()
    archived = repository.load_local(location, headers_only=False).records[0]

    with pytest.raises(ItemStateConflict):
        UpdateTask(executor, repository).execute(
            scope,
            UpdateTaskRequest(TaskId(TASK_ID), archived.revision, title="Forbidden"),
        )


def test_decision_update_allows_inactive_clarification(tmp_path: Path) -> None:
    repository, location, scope, executor = _state(tmp_path)
    before = repository.load_local(location, headers_only=False)
    decision_id = DecisionId("dec_019f0000000070008000000000000001")
    created = CreateDecision(executor, repository, Clock()).execute(
        scope,
        CreateDecisionRequest(decision_id, "Original", b"body", (), before.store_revision),
    )
    raw = repository.read_file(location, created.record.path).content
    location.real_root.joinpath(*created.record.path.parts).write_bytes(
        raw.replace(
            b"tags = []\n",
            b'tags = []\nretired_at = "2026-07-11T00:00:00.000Z"\nretire_note = "ended"\n',
        )
    )
    inactive = repository.load_local(location, headers_only=False).records[0]

    updated = UpdateDecision(executor, repository).execute(
        scope,
        UpdateDecisionRequest(decision_id, inactive.revision, title="Clarified after retirement"),
    )

    assert updated.record.metadata.title == "Clarified after retirement"
    assert updated.record.metadata.retire_note == "ended"


@pytest.mark.parametrize(("reason", "severity"), [("missing", "warning"), ("invalid", "error")])
def test_decision_update_uses_selected_local_policy_for_unrelated_child_failure(
    tmp_path: Path,
    reason: str,
    severity: str,
) -> None:
    repository, location, scope, executor = _state(tmp_path)
    before = repository.load_local(location, headers_only=False)
    decision_id = DecisionId("dec_019f0000000070008000000000000001")
    created = CreateDecision(executor, repository, Clock()).execute(
        scope,
        CreateDecisionRequest(decision_id, "Original", b"body", (), before.store_revision),
    )
    child_id = StoreId("sto_019f0000000070008000000000000001")

    def incomplete_load() -> FederatedSnapshot:
        current = scope.load()
        entry = IncompleteStore(
            child_id,
            reason,  # type: ignore[arg-type]
            Diagnostic(
                code="ORC005",
                severity=severity,
                path="registry.toml",
                field=f"children.{child_id.root}",
                message="unrelated child unavailable",
                hint="restore child",
            ),
        )
        return FederatedSnapshot(current.selected, current.stores, Completeness((entry,)))

    result = UpdateDecision(executor, repository).execute(
        MutationScope(scope.locations, scope.selected, incomplete_load),
        UpdateDecisionRequest(decision_id, created.record.revision, title="Clarified"),
    )

    assert result.record.metadata.title == "Clarified"


def test_decision_update_selected_local_policy_still_refuses_invalid_selected_store(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor = _state(tmp_path)
    before = repository.load_local(location, headers_only=False)
    decision_id = DecisionId("dec_019f0000000070008000000000000001")
    created = CreateDecision(executor, repository, Clock()).execute(
        scope,
        CreateDecisionRequest(decision_id, "Original", b"body", (), before.store_revision),
    )
    invalid = location.real_root / "tasks" / f"{TASK_ID}-invalid.md"
    invalid.parent.mkdir()
    invalid.write_bytes(b"not an item")

    from untaped_orchestration.application.mutations import InvalidMutationState

    with pytest.raises(InvalidMutationState):
        UpdateDecision(executor, repository).execute(
            scope,
            UpdateDecisionRequest(decision_id, created.record.revision, title="Forbidden"),
        )
