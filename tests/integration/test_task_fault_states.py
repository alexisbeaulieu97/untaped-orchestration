from __future__ import annotations

from pathlib import Path

import pytest

from tests.builders import STORE_ID
from tests.unit.application.test_task_transition import Clock
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.curation import CurationService, SnoozeRequest
from untaped_orchestration.application.items import (
    CreateTask,
    CreateTaskRequest,
    MutationExecutionScope,
    MutationScope,
    RevisionConflict,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    FileDeletion,
    FileReplacement,
)
from untaped_orchestration.application.tasks import (
    CloseTaskRequest,
    MoveTaskRequest,
    TaskService,
    TransitionTaskRequest,
)
from untaped_orchestration.domain.ids import TaskId
from untaped_orchestration.domain.models import ActiveTask, TaskOutcome, TaskPriority, TaskStage
from untaped_orchestration.domain.ordering import PlacementAnchor, PlacementAnchorKind
from untaped_orchestration.domain.time import CalendarDate
from untaped_orchestration.infrastructure.filesystem import AtomicFilesystem, location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


class RecordingWriter:
    def __init__(self, delegate: FilesystemStoreRepository) -> None:
        self.delegate = delegate
        self.events: list[tuple[str, str]] = []

    def replace(self, location, change: FileReplacement) -> None:
        self.events.append(("replace", change.path.as_posix()))
        self.delegate.replace(location, change)

    def delete(self, location, change: FileDeletion) -> None:
        self.events.append(("delete", change.path.as_posix()))
        self.delegate.delete(location, change)


class FailingViews:
    def managed_paths(self):
        return MarkdownViewRenderer().managed_paths()

    def expected(self, snapshot):
        del snapshot
        raise OSError("renderer failed")


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


def test_transition_interruption_after_primary_fsync_leaves_safe_but_stale_final_state(
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
    with pytest.raises(RevisionConflict, match="stale"):
        service.transition(request)


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
    by_id = {task.id: task.rank for task in tasks}
    expected = {
        1: {first.record.metadata.id: 1, second.record.metadata.id: 2000},
        2: {first.record.metadata.id: 1000, second.record.metadata.id: 2000},
        3: {first.record.metadata.id: 1000, second.record.metadata.id: 500},
    }
    assert by_id == expected[stop_after]

    current_primary = next(
        record for record in after.records if record.metadata.id == primary.metadata.id
    )
    recovered = service.move(
        MoveTaskRequest(
            current_primary.metadata.id,
            None,
            None,
            current_primary.revision,
            after.store_revision,
            PlacementAnchor(PlacementAnchorKind.FIRST),
        )
    )
    assert not recovered.receipt.replayed
    final = repository.load_local(location, headers_only=False)
    ordered = sorted(
        (record.metadata for record in final.records if isinstance(record.metadata, ActiveTask)),
        key=lambda task: task.rank,
    )
    assert [task.id for task in ordered] == [second.record.metadata.id, first.record.metadata.id]


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


@pytest.mark.parametrize("outcome", [TaskOutcome.DECLINED, TaskOutcome.SUPERSEDED])
def test_close_writes_canonical_phases_in_required_order(
    tmp_path: Path,
    outcome: TaskOutcome,
) -> None:
    repository, location, scope, executor, _ = fixture(tmp_path)
    predecessor = create(repository, location, scope, executor, 1)
    successor = (
        create(repository, location, scope, executor, 2)
        if outcome is TaskOutcome.SUPERSEDED
        else None
    )
    writer = RecordingWriter(repository)
    recording_executor = MutationExecutor(
        repository,
        writer,
        FileLockManager(),
        MarkdownViewRenderer(),
        projector=repository,
    )
    service = TaskService(recording_executor, repository, Clock(), scope)
    current = repository.load_local(location, headers_only=False)
    service.close(
        CloseTaskRequest(
            predecessor.record.metadata.id,
            outcome,
            "ordered close",
            predecessor.record.revision,
            current.store_revision,
            successor.record.metadata.id if successor else None,
            successor.record.revision if successor else None,
        )
    )
    canonical = [event for event in writer.events if not event[1].startswith("views/")]
    expected = [
        ("replace", f"archive/tasks/{predecessor.record.path.name}"),
        ("delete", predecessor.record.path.as_posix()),
    ]
    if successor is not None:
        expected.insert(0, ("replace", successor.record.path.as_posix()))
    assert canonical == expected


@pytest.mark.parametrize("family", ["transition", "move", "close", "curation"])
def test_task9_mutations_report_canonical_success_when_view_finalization_fails(
    tmp_path: Path,
    family: str,
) -> None:
    repository, location, scope, executor, _ = fixture(tmp_path)
    primary = create(repository, location, scope, executor, 1)
    parent = create(repository, location, scope, executor, 2) if family == "move" else None
    failing_executor = MutationExecutor(
        repository,
        repository,
        FileLockManager(),
        FailingViews(),
        projector=repository,
    )
    service = TaskService(failing_executor, repository, Clock(), scope)
    current = repository.load_local(location, headers_only=False)
    if family == "transition":
        result = service.transition(
            TransitionTaskRequest(
                primary.record.metadata.id,
                TaskStage.PLANNED,
                None,
                primary.record.revision,
                current.store_revision,
                PlacementAnchor(PlacementAnchorKind.LAST),
            )
        )
    elif family == "move":
        assert parent is not None
        result = service.move(
            MoveTaskRequest(
                primary.record.metadata.id,
                parent.record.metadata.id,
                None,
                primary.record.revision,
                current.store_revision,
                PlacementAnchor(PlacementAnchorKind.LAST),
            )
        )
    elif family == "close":
        result = service.close(
            CloseTaskRequest(
                primary.record.metadata.id,
                TaskOutcome.DECLINED,
                "closed",
                primary.record.revision,
                current.store_revision,
            )
        )
    else:
        result = CurationService(
            failing_executor,
            repository,
            Clock(),
            scope,
        ).snooze(
            SnoozeRequest(
                primary.record.metadata.id,
                CalendarDate("2026-07-20"),
                primary.record.revision,
            )
        )
    assert result.receipt.canonical_applied
    assert not result.receipt.views_current


@pytest.mark.parametrize(
    ("outcome", "stop_after", "diverge"),
    [
        (TaskOutcome.DECLINED, 1, "archive"),
        (TaskOutcome.SUPERSEDED, 1, "successor"),
        (TaskOutcome.SUPERSEDED, 2, "archive"),
    ],
)
def test_close_retry_refuses_divergence_at_every_accepted_intermediate_phase(
    tmp_path: Path,
    outcome: TaskOutcome,
    stop_after: int,
    diverge: str,
) -> None:
    count = 0
    armed = False

    def hook(event: str) -> None:
        nonlocal count
        if armed and event == "before-ack":
            count += 1
            if count == stop_after:
                raise OSError("interrupted close")

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
        "close",
        predecessor.record.revision,
        current.store_revision,
        successor.record.metadata.id if successor else None,
        successor.record.revision if successor else None,
    )
    armed = True
    with pytest.raises(OSError):
        service.close(request)
    interrupted = repository.load_local(location, headers_only=False)
    if diverge == "successor":
        assert successor is not None
        record = next(
            value
            for value in interrupted.records
            if value.metadata.id == successor.record.metadata.id
        )
    else:
        record = next(
            value
            for value in interrupted.records
            if value.metadata.id == predecessor.record.metadata.id
            and not isinstance(value.metadata, ActiveTask)
        )
    path = location.real_root.joinpath(*record.path.parts)
    path.write_bytes(
        repository.item_bytes(
            record.metadata.model_copy(update={"title": "diverged phase"}),
            record.body or b"",
        )
    )

    with pytest.raises(ValueError):
        service.close(request)
