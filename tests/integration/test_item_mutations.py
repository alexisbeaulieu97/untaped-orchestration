from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import STORE_ID, TASK_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.items import (
    ChangeEvidence,
    ChangeLink,
    CreateTask,
    CreateTaskRequest,
    EvidenceRequest,
    LinkRequest,
    MutationScope,
    UpdateTask,
    UpdateTaskRequest,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.results import Completeness, FederatedSnapshot
from untaped_orchestration.domain.evidence import EvidenceReference, EvidenceRelation
from untaped_orchestration.domain.ids import StoreId, TaskId
from untaped_orchestration.domain.models import LinkRelation, TaskPriority
from untaped_orchestration.infrastructure.filesystem import AtomicFilesystem, location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 11, 1, 2, 3, 4000, tzinfo=UTC)


class FailingViews:
    def managed_paths(self) -> tuple[PurePosixPath, ...]:
        return (
            PurePosixPath("views/roadmap.md"),
            PurePosixPath("views/backlog.md"),
            PurePosixPath("views/inbox.md"),
            PurePosixPath("views/decisions.md"),
        )

    def expected(self, snapshot):
        del snapshot
        raise OSError("renderer failed")


def _fixture(tmp_path: Path, *, views=None, repository=None):
    target = tmp_path / "repository"
    target.mkdir()
    repository = repository or FilesystemStoreRepository()
    locks = FileLockManager()
    normal_views = MarkdownViewRenderer()
    InitializeStore(repository, repository, locks, normal_views).execute(
        InitRequest(target, STORE_ID, "Local", "UTC")
    )
    location = location_from_root(target / ".untaped" / "orchestration")

    def load() -> FederatedSnapshot:
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    executor = MutationExecutor(
        repository,
        repository,
        locks,
        views or normal_views,
        projector=repository,
    )
    return repository, location, MutationScope((location,), location, load), executor


@pytest.mark.parametrize("family", ["create", "update", "link", "evidence"])
def test_every_item_mutation_family_uses_shared_finalizer_and_preserves_renderer_failure_receipt(
    tmp_path: Path,
    family: str,
) -> None:
    repository, location, scope, normal = _fixture(tmp_path)
    before = repository.load_local(location, headers_only=False)
    created = CreateTask(normal, repository, Clock()).execute(
        scope,
        CreateTaskRequest(
            TaskId(TASK_ID), "Task", b"", (), TaskPriority.NORMAL, (), before.store_revision
        ),
    )
    failing = MutationExecutor(
        repository,
        repository,
        FileLockManager(),
        FailingViews(),
        projector=repository,
    )
    if family == "create":
        current = repository.load_local(location, headers_only=False)
        result = CreateTask(failing, repository, Clock()).execute(
            scope,
            CreateTaskRequest(
                TaskId("tsk_019f0000000070008000000000000011"),
                "Second",
                b"",
                (),
                TaskPriority.NORMAL,
                (),
                current.store_revision,
            ),
        )
    elif family == "update":
        result = UpdateTask(failing, repository).execute(
            scope,
            UpdateTaskRequest(TaskId(TASK_ID), created.record.revision, title="Updated"),
        )
    elif family == "link":
        result = ChangeLink(failing, repository).add(
            scope,
            LinkRequest(
                TaskId(TASK_ID),
                LinkRelation.FOLLOW_UP_TO,
                StoreId(STORE_ID),
                TaskId(TASK_ID),
                created.record.revision,
            ),
        )
    else:
        result = ChangeEvidence(failing, repository).add(
            scope,
            EvidenceRequest(
                TaskId(TASK_ID),
                EvidenceRelation.TRACKED_BY,
                EvidenceReference("url:https://example.com/issue"),
                created.record.revision,
            ),
        )

    assert result.receipt.canonical_applied
    assert not result.receipt.views_current
    assert result.record.revision != created.record.revision or family == "create"


def test_create_acknowledgement_loss_after_final_fsync_replays_exact_durable_item(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor = _fixture(tmp_path)
    before = repository.load_local(location, headers_only=False)
    request = CreateTaskRequest(
        TaskId(TASK_ID), "Task", b"body", (), TaskPriority.NORMAL, (), before.store_revision
    )
    armed = True

    def fail_after_fsync(event: str) -> None:
        nonlocal armed
        if armed and event == "before-ack":
            armed = False
            raise OSError("acknowledgement lost")

    fault_repository = FilesystemStoreRepository(
        atomic=AtomicFilesystem(event_hook=fail_after_fsync)
    )
    fault_executor = MutationExecutor(
        fault_repository,
        fault_repository,
        FileLockManager(),
        MarkdownViewRenderer(),
        projector=fault_repository,
    )
    fault_scope = MutationScope(
        scope.locations, scope.selected, lambda: _load(fault_repository, location)
    )
    with pytest.raises(OSError, match="acknowledgement lost"):
        CreateTask(fault_executor, fault_repository, Clock()).execute(fault_scope, request)

    replay = CreateTask(executor, repository, Clock()).execute(scope, request)
    assert replay.receipt.replayed
    assert not replay.receipt.canonical_applied
    assert replay.record.metadata.created_at.root == "2026-07-11T01:02:03.004Z"
    assert replay.record.metadata.rank == 1000


def _load(repository: FilesystemStoreRepository, location) -> FederatedSnapshot:
    selected = repository.load_local(location, headers_only=False)
    return FederatedSnapshot(selected, (selected,), Completeness())
