from __future__ import annotations

from pathlib import Path

import pytest

from tests.builders import STORE_ID
from tests.unit.application.test_task_transition import Clock
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.items import (
    CreateTask,
    CreateTaskRequest,
    MutationExecutionScope,
    MutationScope,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.results import Completeness, FederatedSnapshot
from untaped_orchestration.application.tasks import (
    CloseTaskRequest,
    MoveTaskRequest,
    TaskService,
    TransitionTaskRequest,
)
from untaped_orchestration.domain.ids import TaskId
from untaped_orchestration.domain.models import ActiveTask, TaskOutcome, TaskPriority, TaskStage
from untaped_orchestration.domain.ordering import PlacementAnchor, PlacementAnchorKind
from untaped_orchestration.infrastructure.filesystem import AtomicFilesystem, location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def fixture(tmp_path: Path, hook=None):
    target = tmp_path / "repository"
    target.mkdir()
    normal = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    InitializeStore(normal, normal, locks, views).execute(
        InitRequest(target, STORE_ID, "Local", "UTC")
    )
    location = location_from_root(target / ".untaped" / "orchestration")
    repository = (
        FilesystemStoreRepository(atomic=AtomicFilesystem(event_hook=hook)) if hook else normal
    )

    def load():
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    execution = MutationExecutionScope((location,), location, load)
    scope = MutationScope(execution, execution)
    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    return repository, location, scope, executor, TaskService(executor, repository, Clock(), scope)


def create(repository, location, scope, executor, suffix, rank=None):
    current = repository.load_local(location, headers_only=False)
    result = CreateTask(executor, repository, Clock()).execute(
        scope,
        CreateTaskRequest(
            TaskId(f"tsk_019f00000000700080000000000000{suffix:02d}"),
            f"Task {suffix}",
            b"",
            (),
            TaskPriority.NORMAL,
            (),
            current.store_revision,
        ),
    )
    if rank is not None:
        path = location.real_root.joinpath(*result.record.path.parts)
        metadata = result.record.metadata.model_copy(update={"rank": rank})
        path.write_bytes(repository.item_bytes(metadata, b""))
        current = repository.load_local(location, headers_only=False)
        result = next(r for r in current.records if r.metadata.id == result.record.metadata.id)
        from untaped_orchestration.application.items import ItemMutationResult

        result = ItemMutationResult(result, None)  # type: ignore[arg-type]
    return result


def test_transition_interruption_after_primary_fsync_leaves_exact_replayable_final_state(
    tmp_path: Path,
) -> None:
    armed = False

    def hook(event: str):
        nonlocal armed
        if armed and event == "before-ack":
            armed = False
            raise OSError("lost acknowledgement")

    repository, location, scope, executor, service = fixture(tmp_path, hook)
    task = create(repository, location, scope, executor, 1)
    current = repository.load_local(location, headers_only=False)
    request = TransitionTaskRequest(
        task.record.metadata.id,
        TaskStage.PLANNED,
        None,
        task.record.revision,
        current.store_revision,
        PlacementAnchor(PlacementAnchorKind.LAST),
    )
    armed = True
    with pytest.raises(OSError):
        service.transition(request)
    assert service.transition(request).receipt.replayed


def test_close_fault_prefixes_never_lose_task_and_archive_precedes_delete(tmp_path: Path) -> None:
    events = []
    repository, location, scope, executor, service = fixture(tmp_path, events.append)
    task = create(repository, location, scope, executor, 1)
    current = repository.load_local(location, headers_only=False)
    service.close(
        CloseTaskRequest(
            task.record.metadata.id,
            TaskOutcome.DECLINED,
            "done",
            task.record.revision,
            current.store_revision,
        )
    )
    assert not location.real_root.joinpath(*task.record.path.parts).exists()
    archive = tuple((location.real_root / "archive/tasks").glob("*.md"))
    assert len(archive) == 1
    assert events.count("before-ack") >= 2


@pytest.mark.parametrize("stop_after", [1, 2, 3])
def test_every_rebalance_and_primary_boundary_keeps_unique_ranks_and_safe_scope(
    tmp_path: Path,
    stop_after: int,
) -> None:
    count = 0
    armed = False

    def hook(event: str):
        nonlocal count
        if armed and event == "before-ack":
            count += 1
            if count == stop_after:
                raise OSError("stop after rebalance replacement")

    repository, location, scope, executor, service = fixture(tmp_path, hook)
    first = create(repository, location, scope, executor, 1)
    second = create(repository, location, scope, executor, 2)
    for result, rank in ((first, 1), (second, 2)):
        path = location.real_root.joinpath(*result.record.path.parts)
        path.write_bytes(
            repository.item_bytes(result.record.metadata.model_copy(update={"rank": rank}), b"")
        )
    current = repository.load_local(location, headers_only=False)
    primary = next(r for r in current.records if r.metadata.id == second.record.metadata.id)
    armed = True
    with pytest.raises(OSError):
        service.move(
            MoveTaskRequest(
                primary.metadata.id,
                None,
                None,
                primary.revision,
                current.store_revision,
                PlacementAnchor(PlacementAnchorKind.FIRST),
            )
        )
    after = repository.load_local(location, headers_only=False)
    tasks = [r.metadata for r in after.records if isinstance(r.metadata, ActiveTask)]
    assert len({task.rank for task in tasks}) == len(tasks)
    assert next(task for task in tasks if task.id == primary.metadata.id).parent is None


@pytest.mark.parametrize(
    ("outcome", "stop_after"),
    [
        (TaskOutcome.DECLINED, 1),
        (TaskOutcome.DECLINED, 2),
        (TaskOutcome.SUPERSEDED, 1),
        (TaskOutcome.SUPERSEDED, 2),
        (TaskOutcome.SUPERSEDED, 3),
    ],
)
def test_every_close_canonical_boundary_is_safe_and_same_request_recovers(
    tmp_path: Path,
    outcome: TaskOutcome,
    stop_after: int,
) -> None:
    count = 0
    armed = False

    def hook(event: str) -> None:
        nonlocal count
        if armed and event == "before-ack":
            count += 1
            if count == stop_after:
                raise OSError(f"stop at canonical boundary {stop_after}")

    repository, location, scope, executor, service = fixture(tmp_path, hook)
    predecessor = create(repository, location, scope, executor, 1)
    successor = (
        create(repository, location, scope, executor, 2)
        if outcome is TaskOutcome.SUPERSEDED
        else None
    )
    current = repository.load_local(location, headers_only=False)
    request = CloseTaskRequest(
        predecessor.record.metadata.id,
        outcome,
        "durable close",
        predecessor.record.revision,
        current.store_revision,
        successor.record.metadata.id if successor else None,
        successor.record.revision if successor else None,
    )
    armed = True
    with pytest.raises(OSError, match="canonical boundary"):
        service.close(request)
    interrupted = repository.load_local(location, headers_only=False)
    assert any(
        record.metadata.id == predecessor.record.metadata.id for record in interrupted.records
    )

    recovered = service.close(request)

    assert recovered.record.metadata.outcome is outcome
    final = repository.load_local(location, headers_only=False)
    matching = [
        record for record in final.records if record.metadata.id == predecessor.record.metadata.id
    ]
    assert len(matching) == 1
    assert not isinstance(matching[0].metadata, ActiveTask)
