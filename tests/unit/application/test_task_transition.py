from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.builders import STORE_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.items import (
    CreateTask,
    CreateTaskRequest,
    MutationExecutionScope,
    MutationScope,
    RevisionConflict,
)
from untaped_orchestration.application.mutations import InvalidMutationState, MutationExecutor
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    IncompleteStore,
)
from untaped_orchestration.application.tasks import (
    CloseTaskRequest,
    TaskLifecycleConflict,
    TaskService,
    TransitionTaskRequest,
)
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.ids import Slug, StoreId, TaskId
from untaped_orchestration.domain.models import Revision, TaskOutcome, TaskPriority, TaskStage
from untaped_orchestration.domain.ordering import PlacementAnchor, PlacementAnchorKind
from untaped_orchestration.domain.time import UtcTimestamp
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 11, 12, 34, 56, 789123, tzinfo=UTC)


def state(tmp_path: Path):
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
    scope = MutationScope(execution, execution)
    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    return repository, location, scope, executor, TaskService(executor, repository, Clock(), scope)


def create(repository, location, scope, executor, *, suffix: int = 1, waiting=()):
    before = repository.load_local(location, headers_only=False)
    item_id = TaskId(f"tsk_019f00000000700080000000000000{suffix:02d}")
    return CreateTask(executor, repository, Clock()).execute(
        scope,
        CreateTaskRequest(
            item_id,
            f"Task {suffix}",
            f"body-{suffix}".encode(),
            (),
            TaskPriority.NORMAL,
            waiting,
            before.store_revision,
        ),
    )


def transition_request(result, store_revision, to_stage, **changes):
    values = dict(
        item_id=result.record.metadata.id,
        to_stage=to_stage,
        expected_parent=result.record.metadata.parent,
        expected_revision=result.record.revision,
        expected_store_revision=store_revision,
        placement=PlacementAnchor(PlacementAnchorKind.LAST),
    )
    values.update(changes)
    return TransitionTaskRequest(**values)


def test_transition_request_is_frozen_and_owns_only_lifecycle_and_placement_guards() -> None:
    assert [field.name for field in fields(TransitionTaskRequest)] == [
        "item_id",
        "to_stage",
        "expected_parent",
        "expected_revision",
        "expected_store_revision",
        "placement",
        "revisit_when",
        "expected_anchor_revision",
    ]
    request = TransitionTaskRequest(
        TaskId("tsk_019f0000000070008000000000000001"),
        TaskStage.PLANNED,
        None,
        Revision("sha256:" + "0" * 64),
        Revision("sha256:" + "1" * 64),
        PlacementAnchor(PlacementAnchorKind.LAST),
    )
    with pytest.raises(FrozenInstanceError):
        request.revisit_when = "later"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("start", "target", "revisit"),
    [
        (TaskStage.INBOX, TaskStage.BACKLOG, "after launch"),
        (TaskStage.INBOX, TaskStage.PLANNED, None),
        (TaskStage.BACKLOG, TaskStage.PLANNED, None),
        (TaskStage.PLANNED, TaskStage.BACKLOG, "after launch"),
        (TaskStage.PLANNED, TaskStage.IN_PROGRESS, None),
        (TaskStage.IN_PROGRESS, TaskStage.PLANNED, None),
        (TaskStage.BACKLOG, TaskStage.BACKLOG, "new trigger"),
    ],
)
def test_allowed_transition_matrix_and_lifecycle_fields(
    tmp_path: Path, start: TaskStage, target: TaskStage, revisit: str | None
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    result = create(repository, location, scope, executor)
    current_stage = TaskStage.INBOX
    for stage, trigger in (
        (TaskStage.PLANNED, None),
        (TaskStage.IN_PROGRESS, None),
        (TaskStage.PLANNED, None),
        (TaskStage.BACKLOG, "seed"),
    ):
        if current_stage is start:
            break
        snapshot = repository.load_local(location, headers_only=False)
        result = service.transition(
            transition_request(result, snapshot.store_revision, stage, revisit_when=trigger)
        )
        current_stage = stage
    snapshot = repository.load_local(location, headers_only=False)
    changed = service.transition(
        transition_request(result, snapshot.store_revision, target, revisit_when=revisit)
    )
    assert changed.record.metadata.stage is target
    assert changed.record.metadata.revisit_when == (
        revisit if target is TaskStage.BACKLOG else None
    )
    if target is TaskStage.IN_PROGRESS:
        assert changed.record.metadata.started_at is not None
    if start is TaskStage.IN_PROGRESS:
        assert changed.record.metadata.started_at == result.record.metadata.started_at


@pytest.mark.parametrize(
    ("target", "revisit"),
    [
        (TaskStage.IN_PROGRESS, None),
        (TaskStage.INBOX, None),
        (TaskStage.BACKLOG, None),
        (TaskStage.PLANNED, "not allowed"),
    ],
)
def test_rejected_transition_matrix(tmp_path: Path, target: TaskStage, revisit: str | None) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor)
    current = repository.load_local(location, headers_only=False)
    with pytest.raises(TaskLifecycleConflict):
        service.transition(
            transition_request(task, current.store_revision, target, revisit_when=revisit)
        )


def test_started_at_is_set_once_and_backlog_same_stage_replaces_trigger(tmp_path: Path) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor)
    current = repository.load_local(location, headers_only=False)
    planned = service.transition(
        transition_request(task, current.store_revision, TaskStage.PLANNED)
    )
    current = repository.load_local(location, headers_only=False)
    started = service.transition(
        transition_request(planned, current.store_revision, TaskStage.IN_PROGRESS)
    )
    current = repository.load_local(location, headers_only=False)
    paused = service.transition(
        transition_request(started, current.store_revision, TaskStage.PLANNED)
    )
    current = repository.load_local(location, headers_only=False)
    backlog = service.transition(
        transition_request(paused, current.store_revision, TaskStage.BACKLOG, revisit_when="one")
    )
    current = repository.load_local(location, headers_only=False)
    replaced = service.transition(
        transition_request(backlog, current.store_revision, TaskStage.BACKLOG, revisit_when="two")
    )
    assert paused.record.metadata.started_at == started.record.metadata.started_at
    assert replaced.record.metadata.started_at == started.record.metadata.started_at
    assert replaced.record.metadata.revisit_when == "two"


def test_start_refuses_waiting_party_and_stale_parent_store_or_item_guards(tmp_path: Path) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor, waiting=(Slug("alexis"),))
    current = repository.load_local(location, headers_only=False)
    planned = service.transition(
        transition_request(task, current.store_revision, TaskStage.PLANNED)
    )
    current = repository.load_local(location, headers_only=False)
    with pytest.raises(TaskLifecycleConflict, match="blocked"):
        service.transition(
            transition_request(planned, current.store_revision, TaskStage.IN_PROGRESS)
        )
    stale = Revision("sha256:" + "0" * 64)
    with pytest.raises((TaskLifecycleConflict, RevisionConflict)):
        service.transition(
            transition_request(planned, stale, TaskStage.BACKLOG, revisit_when="later")
        )
    with pytest.raises(TaskLifecycleConflict):
        service.transition(
            transition_request(
                planned,
                current.store_revision,
                TaskStage.BACKLOG,
                revisit_when="later",
                expected_parent=TaskId("tsk_019f0000000070008000000000000099"),
            )
        )


def test_stale_transition_conflicts_even_when_current_shape_is_requested_final_state(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor)
    before = repository.load_local(location, headers_only=False)
    request = transition_request(task, before.store_revision, TaskStage.PLANNED)
    service.transition(request)
    with pytest.raises(RevisionConflict):
        service.transition(request)


def test_fresh_guard_reissue_of_exact_transition_target_is_idempotent_noop(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor)
    current = repository.load_local(location, headers_only=False)
    transitioned = service.transition(
        transition_request(task, current.store_revision, TaskStage.PLANNED)
    )
    current = repository.load_local(location, headers_only=False)
    rank = transitioned.record.metadata.rank
    result = service.transition(
        transition_request(transitioned, current.store_revision, TaskStage.PLANNED)
    )
    assert not result.receipt.applied
    assert not result.receipt.replayed
    assert result.record.metadata.rank == rank


@pytest.mark.parametrize(
    "divergence",
    ["title", "body", "tags", "waiting_on", "started_at", "rank"],
)
def test_stale_transition_never_hides_unrelated_or_lifecycle_divergence(
    tmp_path: Path,
    divergence: str,
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor)
    before = repository.load_local(location, headers_only=False)
    request = transition_request(task, before.store_revision, TaskStage.PLANNED)
    transitioned = service.transition(request)
    path = location.real_root.joinpath(*transitioned.record.path.parts)
    metadata = transitioned.record.metadata
    body = transitioned.record.body or b""
    if divergence == "title":
        metadata = metadata.model_copy(update={"title": "diverged"})
    elif divergence == "body":
        body = b"diverged body"
    elif divergence == "tags":
        metadata = metadata.model_copy(update={"tags": (Slug("diverged"),)})
    elif divergence == "waiting_on":
        metadata = metadata.model_copy(update={"waiting_on": (Slug("team"),)})
    elif divergence == "started_at":
        metadata = metadata.model_copy(
            update={"started_at": UtcTimestamp("2026-07-12T00:00:00.000Z")}
        )
    else:
        metadata = metadata.model_copy(update={"rank": metadata.rank + 1})
    path.write_bytes(repository.item_bytes(metadata, body))

    with pytest.raises(RevisionConflict):
        service.transition(request)


def test_start_fails_closed_when_recursive_federation_is_incomplete(tmp_path: Path) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    task = create(repository, location, scope, executor)
    current = repository.load_local(location, headers_only=False)
    planned = service.transition(
        transition_request(task, current.store_revision, TaskStage.PLANNED)
    )
    missing_id = StoreId("sto_019f0000000070008000000000000099")

    def incomplete_load() -> FederatedSnapshot:
        selected = repository.load_local(location, headers_only=False)
        missing = IncompleteStore(
            missing_id,
            "missing",
            Diagnostic(
                code="ORC005",
                severity="error",
                path="registry.toml",
                field="children",
                message="required child is missing",
                hint="restore the child",
            ),
        )
        return FederatedSnapshot(selected, (selected,), Completeness((missing,)))

    incomplete_execution = MutationExecutionScope((location,), location, incomplete_load)
    incomplete_scope = MutationScope(incomplete_execution, scope.selected_local)
    incomplete_service = TaskService(executor, repository, Clock(), incomplete_scope)
    current = repository.load_local(location, headers_only=False)
    with pytest.raises(InvalidMutationState):
        incomplete_service.transition(
            transition_request(planned, current.store_revision, TaskStage.IN_PROGRESS)
        )
    with pytest.raises(InvalidMutationState):
        incomplete_service.close(
            CloseTaskRequest(
                planned.record.metadata.id,
                TaskOutcome.DELIVERED,
                "delivery",
                planned.record.revision,
                current.store_revision,
            )
        )
