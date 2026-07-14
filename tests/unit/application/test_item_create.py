from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.builders import DECISION_ID, STORE_ID, TASK_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.items import (
    CreateConflict,
    CreateDecision,
    CreateDecisionRequest,
    CreateTask,
    CreateTaskRequest,
    MutationExecutionScope,
    MutationScope,
    RevisionConflict,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.results import Completeness, FederatedSnapshot
from untaped_orchestration.domain.diagnostics import DiagnosticError
from untaped_orchestration.domain.ids import DecisionId, Slug, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Revision,
    TaskOutcome,
    TaskPriority,
    TaskStage,
)
from untaped_orchestration.domain.time import UtcTimestamp
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return self.value


def _services(tmp_path: Path):
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

    execution = MutationExecutionScope((location,), location, load)
    scope = MutationScope(lambda: execution, lambda: execution)
    executor = MutationExecutor(
        repository,
        repository,
        locks,
        views,
        projector=repository,
    )
    clock = FixedClock(datetime(2026, 7, 11, 12, 34, 56, 789123, tzinfo=UTC))
    return (
        repository,
        location,
        scope,
        CreateTask(executor, repository, clock),
        CreateDecision(executor, repository, clock),
        clock,
    )


def _task_request(store_revision: Revision, **changes: object) -> CreateTaskRequest:
    values: dict[str, object] = {
        "item_id": TaskId(TASK_ID),
        "title": "Crème / launch!!!",
        "body": b"opaque\r\nbody",
        "tags": (Slug("launch"), Slug("alpha")),
        "priority": TaskPriority.NORMAL,
        "waiting_on": (Slug("alexis"),),
        "expected_store_revision": store_revision,
    }
    values.update(changes)
    return CreateTaskRequest(**values)  # type: ignore[arg-type]


def _decision_request(store_revision: Revision, **changes: object) -> CreateDecisionRequest:
    values: dict[str, object] = {
        "item_id": DecisionId(DECISION_ID),
        "title": "Keep caller-owned identity",
        "body": b"decision body",
        "tags": (Slug("identity"),),
        "expected_store_revision": store_revision,
    }
    values.update(changes)
    return CreateDecisionRequest(**values)  # type: ignore[arg-type]


def test_create_request_types_are_frozen_typed_and_have_no_hidden_identity_source() -> None:
    assert [field.name for field in fields(CreateTaskRequest)] == [
        "item_id",
        "title",
        "body",
        "tags",
        "priority",
        "waiting_on",
        "expected_store_revision",
    ]
    assert [field.name for field in fields(CreateDecisionRequest)] == [
        "item_id",
        "title",
        "body",
        "tags",
        "expected_store_revision",
    ]
    assert "id_generator" not in CreateTask.__init__.__annotations__
    assert "id_generator" not in CreateDecision.__init__.__annotations__


@pytest.mark.parametrize("kind", ("task", "decision"))
def test_create_schema_validation_is_typed_orc002(tmp_path: Path, kind: str) -> None:
    repository, location, scope, tasks, decisions, _ = _services(tmp_path)
    revision = repository.load_local(location, headers_only=False).store_revision

    with pytest.raises(DiagnosticError) as captured:
        if kind == "task":
            tasks.execute(scope, _task_request(revision, title=""))
        else:
            decisions.execute(scope, _decision_request(revision, title=""))

    assert captured.value.diagnostics[0].code == "ORC002"
    assert captured.value.diagnostics[0].field == "title"


def test_task_create_uses_caller_id_defaults_and_reports_generated_values(tmp_path: Path) -> None:
    repository, location, scope, tasks, _, clock = _services(tmp_path)
    before = repository.load_local(location, headers_only=False)

    result = tasks.execute(scope, _task_request(before.store_revision))

    assert result.record.metadata.id == TaskId(TASK_ID)
    assert result.record.metadata.created_at == UtcTimestamp("2026-07-11T12:34:56.789Z")
    assert result.record.metadata.stage is TaskStage.INBOX
    assert result.record.metadata.priority is TaskPriority.NORMAL
    assert result.record.metadata.rank == 1000
    assert result.record.path.as_posix() == f"tasks/{TASK_ID}-creme-launch.md"
    assert result.record.body == b"opaque\r\nbody"
    assert result.receipt.item_revisions
    assert result.receipt.store_revision != before.store_revision
    assert result.receipt.views_current
    assert clock.calls == 1


def test_task_create_appends_last_sparse_rank_and_decision_has_exact_generated_time(
    tmp_path: Path,
) -> None:
    repository, location, scope, tasks, decisions, _ = _services(tmp_path)
    first = repository.load_local(location, headers_only=False)
    tasks.execute(scope, _task_request(first.store_revision))
    second = repository.load_local(location, headers_only=False)
    other = tasks.execute(
        scope,
        _task_request(
            second.store_revision,
            item_id=TaskId("tsk_019f0000000070008000000000000011"),
            title="Second",
            body=b"",
            tags=(),
            waiting_on=(),
        ),
    )
    third = repository.load_local(location, headers_only=False)
    decision = decisions.execute(scope, _decision_request(third.store_revision))

    assert other.record.metadata.rank == 2000
    assert decision.record.metadata.created_at == UtcTimestamp("2026-07-11T12:34:56.789Z")
    assert (
        decision.record.path.as_posix() == f"decisions/{DECISION_ID}-keep-caller-owned-identity.md"
    )


def test_task_create_reuses_domain_rebalance_at_signed_int64_last_rank(
    tmp_path: Path,
) -> None:
    repository, location, scope, tasks, _, _ = _services(tmp_path)
    initial = repository.load_local(location, headers_only=False)
    first = tasks.execute(scope, _task_request(initial.store_revision))
    at_boundary = ActiveTask.model_validate(
        {**first.record.metadata.model_dump(by_alias=True), "rank": 2**63 - 1}
    )
    location.real_root.joinpath(*first.record.path.parts).write_bytes(
        repository.item_bytes(at_boundary, first.record.body or b"")
    )
    boundary = repository.load_local(location, headers_only=False)

    created = tasks.execute(
        scope,
        _task_request(
            boundary.store_revision,
            item_id=TaskId("tsk_019f0000000070008000000000000011"),
            title="After boundary",
            body=b"",
            tags=(),
            waiting_on=(),
        ),
    )

    current = repository.load_local(location, headers_only=False)
    ranks = {
        record.metadata.id: record.metadata.rank
        for record in current.records
        if isinstance(record.metadata, ActiveTask)
    }
    assert ranks == {TaskId(TASK_ID): 1000, created.record.metadata.id: 2000}
    assert created.record.path.name.endswith("-after-boundary.md")
    assert created.record.metadata.created_at == UtcTimestamp("2026-07-11T12:34:56.789Z")


def test_exact_existing_id_replay_precedes_stale_store_guard_and_returns_existing_generated_data(
    tmp_path: Path,
) -> None:
    repository, location, scope, tasks, _, clock = _services(tmp_path)
    original = repository.load_local(location, headers_only=False)
    first = tasks.execute(scope, _task_request(original.store_revision))

    replay = tasks.execute(scope, _task_request(original.store_revision))

    assert replay.receipt.replayed
    assert not replay.receipt.canonical_applied
    assert replay.record == first.record
    assert replay.record.metadata.created_at == first.record.metadata.created_at
    assert replay.record.metadata.rank == first.record.metadata.rank
    assert replay.record.path == first.record.path
    assert replay.receipt.store_revision == first.receipt.store_revision
    assert clock.calls == 1


def test_create_refuses_stale_absent_id_and_existing_mismatch(tmp_path: Path) -> None:
    repository, location, scope, tasks, _, _ = _services(tmp_path)
    original = repository.load_local(location, headers_only=False)
    tasks.execute(scope, _task_request(original.store_revision))

    with pytest.raises(CreateConflict):
        tasks.execute(
            scope,
            _task_request(original.store_revision, title="Different caller input"),
        )

    with pytest.raises(RevisionConflict):
        tasks.execute(
            scope,
            _task_request(
                original.store_revision,
                item_id=TaskId("tsk_019f0000000070008000000000000011"),
                title="Absent but stale",
            ),
        )


def test_decision_create_exact_replay_and_mismatch_follow_the_same_contract(tmp_path: Path) -> None:
    repository, location, scope, _, decisions, clock = _services(tmp_path)
    original = repository.load_local(location, headers_only=False)
    first = decisions.execute(scope, _decision_request(original.store_revision))
    replay = decisions.execute(scope, _decision_request(original.store_revision))

    assert replay.receipt.replayed
    assert replay.record == first.record
    assert clock.calls == 1
    with pytest.raises(CreateConflict):
        decisions.execute(
            scope,
            _decision_request(original.store_revision, body=b"different"),
        )


def test_create_refuses_archived_task_and_inactive_decision_id_reuse(tmp_path: Path) -> None:
    repository, location, scope, tasks, decisions, _ = _services(tmp_path)
    original = repository.load_local(location, headers_only=False)
    task_request = _task_request(original.store_revision)
    task = tasks.execute(scope, task_request)
    task_path = location.real_root.joinpath(*task.record.path.parts)
    archive_path = location.real_root / "archive" / "tasks" / task.record.path.name
    archive_path.parent.mkdir(parents=True)
    archived_values = task.record.metadata.model_dump(by_alias=True)
    archived_values.pop("stage")
    archived_values.update(
        closed_from=TaskStage.INBOX,
        outcome=TaskOutcome.DECLINED,
        closed_at=UtcTimestamp("2026-07-11T00:00:00.000Z"),
        close_note="done",
    )
    archived = ArchivedTask.model_validate(archived_values)
    archive_path.write_bytes(repository.item_bytes(archived, task.record.body or b""))
    task_path.unlink()
    with pytest.raises(CreateConflict, match="active task"):
        tasks.execute(scope, task_request)

    current = repository.load_local(location, headers_only=False)
    decision_request = _decision_request(current.store_revision)
    decision = decisions.execute(scope, decision_request)
    retired = decision.record.metadata.model_copy(
        update={
            "retired_at": UtcTimestamp("2026-07-11T00:00:00.000Z"),
            "retire_note": "ended",
        }
    )
    location.real_root.joinpath(*decision.record.path.parts).write_bytes(
        repository.item_bytes(retired, decision.record.body or b"")
    )
    with pytest.raises(CreateConflict, match="active decision"):
        decisions.execute(scope, decision_request)
