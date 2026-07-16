from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from filelock import FileLock

from tests.builders import STORE_ID, TASK_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.federation import FederationService
from untaped_orchestration.application.items import (
    ChangeEvidence,
    ChangeLink,
    CreateDecision,
    CreateDecisionRequest,
    CreateTask,
    CreateTaskRequest,
    EvidenceRequest,
    LinkRequest,
    MutationExecutionScope,
    MutationScope,
    RelationConflict,
    UpdateDecision,
    UpdateDecisionRequest,
    UpdateTask,
    UpdateTaskRequest,
)
from untaped_orchestration.application.mutations import InvalidMutationState, MutationExecutor
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    StoreLockTimeout,
)
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.domain.evidence import EvidenceReference, EvidenceRelation
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    Link,
    LinkRelation,
    Registry,
    RegistryChild,
    TaskPriority,
)
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
    execution = MutationExecutionScope((location,), location, load)
    return repository, location, MutationScope(lambda: execution, lambda: execution), executor


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
    fault_execution = MutationExecutionScope(
        (location,),
        location,
        lambda: _load(fault_repository, location),
    )
    fault_scope = MutationScope(lambda: fault_execution, lambda: fault_execution)
    with pytest.raises(OSError, match="acknowledgement lost"):
        CreateTask(fault_executor, fault_repository, Clock()).execute(fault_scope, request)

    replay = CreateTask(executor, repository, Clock()).execute(scope, request)
    assert replay.receipt.replayed
    assert not replay.receipt.canonical_applied
    assert replay.record.metadata.created_at.root == "2026-07-11T01:02:03.004Z"
    assert replay.record.metadata.rank == 1000


def test_decision_create_acknowledgement_loss_replays_exact_durable_item(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor = _fixture(tmp_path)
    before = repository.load_local(location, headers_only=False)
    request = CreateDecisionRequest(
        DecisionId("dec_019f0000000070008000000000000001"),
        "Decision",
        b"body",
        (),
        before.store_revision,
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
    fault_execution = MutationExecutionScope(
        (location,),
        location,
        lambda: _load(fault_repository, location),
    )
    fault_scope = MutationScope(lambda: fault_execution, lambda: fault_execution)
    with pytest.raises(OSError, match="acknowledgement lost"):
        CreateDecision(fault_executor, fault_repository, Clock()).execute(fault_scope, request)

    replay = CreateDecision(executor, repository, Clock()).execute(scope, request)
    assert replay.receipt.replayed
    assert not replay.receipt.canonical_applied
    assert replay.record.metadata.created_at.root == "2026-07-11T01:02:03.004Z"
    assert replay.record.path.name.endswith("-decision.md")


@pytest.mark.parametrize("fail_at", [1, 2])
def test_boundary_create_fault_prefix_is_valid_and_retryable(
    tmp_path: Path,
    fail_at: int,
) -> None:
    repository, location, scope, executor = _fixture(tmp_path)
    initial = repository.load_local(location, headers_only=False)
    first = CreateTask(executor, repository, Clock()).execute(
        scope,
        CreateTaskRequest(
            TaskId(TASK_ID), "First", b"", (), TaskPriority.NORMAL, (), initial.store_revision
        ),
    )
    boundary = ActiveTask.model_validate(
        {**first.record.metadata.model_dump(by_alias=True), "rank": 2**63 - 1}
    )
    location.real_root.joinpath(*first.record.path.parts).write_bytes(
        repository.item_bytes(boundary, first.record.body or b"")
    )
    before = repository.load_local(location, headers_only=False)
    request = CreateTaskRequest(
        TaskId("tsk_019f0000000070008000000000000011"),
        "After boundary",
        b"body",
        (),
        TaskPriority.NORMAL,
        (),
        before.store_revision,
    )
    acknowledgements = 0

    def fail_on_prefix(event: str) -> None:
        nonlocal acknowledgements
        if event != "before-ack":
            return
        acknowledgements += 1
        if acknowledgements == fail_at:
            raise OSError(f"fault after replacement {fail_at}")

    fault_repository = FilesystemStoreRepository(atomic=AtomicFilesystem(event_hook=fail_on_prefix))
    fault_executor = MutationExecutor(
        fault_repository,
        fault_repository,
        FileLockManager(),
        MarkdownViewRenderer(),
        projector=fault_repository,
    )
    fault_execution = MutationExecutionScope(
        (location,),
        location,
        lambda: _load(fault_repository, location),
    )
    fault_scope = MutationScope(lambda: fault_execution, lambda: fault_execution)
    with pytest.raises(OSError, match=f"replacement {fail_at}"):
        CreateTask(fault_executor, fault_repository, Clock()).execute(fault_scope, request)

    interrupted = _load(repository, location)
    assert not [
        value
        for value in validate_snapshot(interrupted, require_children=True)
        if value.severity == "error"
    ]
    existing = next(
        (
            record
            for record in interrupted.selected.records
            if record.metadata.id == request.item_id
        ),
        None,
    )
    retry_request = (
        request
        if existing is not None
        else CreateTaskRequest(
            request.item_id,
            request.title,
            request.body,
            request.tags,
            request.priority,
            request.waiting_on,
            interrupted.selected.store_revision,
        )
    )
    result = CreateTask(executor, repository, Clock()).execute(scope, retry_request)

    assert result.record.metadata.rank == 2000
    assert result.receipt.replayed is (fail_at == 2)


def _load(repository: FilesystemStoreRepository, location) -> FederatedSnapshot:
    selected = repository.load_local(location, headers_only=False)
    return FederatedSnapshot(selected, (selected,), Completeness())


def _local_scope(
    repository: FilesystemStoreRepository,
    location,
) -> MutationScope:
    execution = MutationExecutionScope(
        (location,),
        location,
        lambda: _load(repository, location),
    )
    return MutationScope(lambda: execution, lambda: execution)


def _register_child(
    repository: FilesystemStoreRepository,
    parent,
    child_id: StoreId,
    path: str,
) -> None:
    registry = Registry(
        schema="untaped.orchestration.registry/v1",
        store_id=StoreId(STORE_ID),
        children=(RegistryChild(id=child_id, path=path),),
    )
    parent.real_root.joinpath("registry.toml").write_bytes(repository.registry_bytes(registry))


def _resolved_scope(
    repository: FilesystemStoreRepository,
    resolved: FederatedSnapshot,
) -> MutationScope:
    locations = tuple(store.location for store in resolved.stores)

    def load() -> FederatedSnapshot:
        stores = tuple(
            repository.load_local(location, headers_only=False) for location in locations
        )
        selected = next(
            store
            for store in stores
            if store.location.real_root == resolved.selected.location.real_root
        )
        return FederatedSnapshot(selected, stores, resolved.completeness)

    recursive = MutationExecutionScope(locations, resolved.selected.location, load)
    selected_local = MutationExecutionScope(
        (resolved.selected.location,),
        resolved.selected.location,
        lambda: _load(repository, resolved.selected.location),
    )
    return MutationScope(lambda: recursive, lambda: selected_local)


def _files(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".lock"
    }


def test_registered_child_links_use_real_federation_and_never_write_target(
    tmp_path: Path,
) -> None:
    parent_repo = tmp_path / "parent"
    child_repo = tmp_path / "child"
    parent_repo.mkdir()
    child_repo.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    child_id = StoreId("sto_019f0000000070008000000000000001")
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(parent_repo, STORE_ID, "Parent", "UTC")
    )
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(child_repo, child_id.root, "Child", "UTC")
    )
    parent = location_from_root(parent_repo / ".untaped" / "orchestration")
    child = location_from_root(child_repo / ".untaped" / "orchestration")
    executor = MutationExecutor(
        repository,
        repository,
        locks,
        views,
        projector=repository,
        lock_timeout=0.01,
    )
    parent_local = _local_scope(repository, parent)
    child_local = _local_scope(repository, child)
    parent_before = repository.load_local(parent, headers_only=False)
    task = CreateTask(executor, repository, Clock()).execute(
        parent_local,
        CreateTaskRequest(
            TaskId(TASK_ID),
            "Parent task",
            b"",
            (),
            TaskPriority.NORMAL,
            (),
            parent_before.store_revision,
        ),
    )
    parent_after_task = repository.load_local(parent, headers_only=False)
    local_decision = CreateDecision(executor, repository, Clock()).execute(
        parent_local,
        CreateDecisionRequest(
            DecisionId("dec_019f0000000070008000000000000001"),
            "Local decision",
            b"",
            (),
            parent_after_task.store_revision,
        ),
    )
    child_before = repository.load_local(child, headers_only=False)
    decision = CreateDecision(executor, repository, Clock()).execute(
        child_local,
        CreateDecisionRequest(
            DecisionId("dec_019f0000000070008000000000000002"),
            "Child decision",
            b"",
            (),
            child_before.store_revision,
        ),
    )
    child_after_decision = repository.load_local(child, headers_only=False)
    child_task = CreateTask(executor, repository, Clock()).execute(
        child_local,
        CreateTaskRequest(
            TaskId("tsk_019f0000000070008000000000000012"),
            "Child task",
            b"",
            (),
            TaskPriority.NORMAL,
            (),
            child_after_decision.store_revision,
        ),
    )
    _register_child(
        repository,
        parent,
        child_id,
        "../../../child/.untaped/orchestration",
    )
    resolved = FederationService(repository, locks).load(parent, local=False, headers_only=False)
    assert resolved.completeness.complete
    scope = _resolved_scope(repository, resolved)
    target_before = _files(child.real_root)
    links = ChangeLink(executor, repository)

    with pytest.raises(RelationConflict, match="same-store"):
        links.add(
            scope,
            LinkRequest(
                task.record.metadata.id,
                LinkRelation.DEPENDS_ON,
                child_id,
                child_task.record.metadata.id,
                task.record.revision,
            ),
        )
    governed = links.add(
        scope,
        LinkRequest(
            task.record.metadata.id,
            LinkRelation.GOVERNED_BY,
            child_id,
            decision.record.metadata.id,
            task.record.revision,
        ),
    )
    followed = links.add(
        scope,
        LinkRequest(
            task.record.metadata.id,
            LinkRelation.FOLLOW_UP_TO,
            child_id,
            child_task.record.metadata.id,
            governed.record.revision,
        ),
    )

    assert [link.relation for link in followed.record.metadata.links] == [
        LinkRelation.FOLLOW_UP_TO,
        LinkRelation.GOVERNED_BY,
    ]
    assert _files(child.real_root) == target_before

    child_lock = FileLock(child.real_root / ".lock")
    child_lock.acquire()
    try:
        updated = UpdateDecision(executor, repository).execute(
            scope,
            UpdateDecisionRequest(
                local_decision.record.metadata.id,
                local_decision.record.revision,
                title="Clarified while child locked",
            ),
        )
        evidence = ChangeEvidence(executor, repository)
        evidence_request = EvidenceRequest(
            local_decision.record.metadata.id,
            EvidenceRelation.TRACKED_BY,
            EvidenceReference("url:https://example.com/local"),
            updated.record.revision,
        )
        added = evidence.add(scope, evidence_request)
        removed = evidence.remove(
            scope,
            EvidenceRequest(
                evidence_request.item_id,
                evidence_request.relation,
                evidence_request.reference,
                added.record.revision,
            ),
        )
        assert removed.record.metadata.evidence == ()
        with pytest.raises(StoreLockTimeout) as captured:
            links.remove(
                scope,
                LinkRequest(
                    task.record.metadata.id,
                    LinkRelation.FOLLOW_UP_TO,
                    child_id,
                    child_task.record.metadata.id,
                    followed.record.revision,
                ),
            )
        assert captured.value.location == child
    finally:
        child_lock.release()

    parent_current = repository.load_local(parent, headers_only=False)
    linked_task = next(
        record for record in parent_current.records if record.metadata.id == task.record.metadata.id
    )
    assert isinstance(linked_task.metadata, ActiveTask) and linked_task.body is not None
    locally_broken_values = linked_task.metadata.model_dump(by_alias=True)
    locally_broken_values.update(
        {
            "links": (
                *linked_task.metadata.links,
                Link(
                    relation=LinkRelation.DEPENDS_ON,
                    target_store_id=StoreId(STORE_ID),
                    target=TaskId("tsk_019f0000000070008000000000000013"),
                ),
            )
        }
    )
    locally_broken = ActiveTask.model_validate(locally_broken_values)
    parent.real_root.joinpath(*linked_task.path.parts).write_bytes(
        repository.item_bytes(locally_broken, linked_task.body)
    )
    with pytest.raises(InvalidMutationState):
        UpdateDecision(executor, repository).execute(
            scope,
            UpdateDecisionRequest(
                local_decision.record.metadata.id,
                removed.record.revision,
                title="Must refuse local graph defect",
            ),
        )


@pytest.mark.parametrize("child_state", ["missing", "invalid", "wrong-id"])
def test_registered_child_completeness_keeps_cross_store_links_fail_closed(
    tmp_path: Path,
    child_state: str,
) -> None:
    parent_repo = tmp_path / "parent"
    parent_repo.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    child_id = StoreId("sto_019f0000000070008000000000000001")
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(parent_repo, STORE_ID, "Parent", "UTC")
    )
    parent = location_from_root(parent_repo / ".untaped" / "orchestration")
    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    local_scope = _local_scope(repository, parent)
    before = repository.load_local(parent, headers_only=False)
    task = CreateTask(executor, repository, Clock()).execute(
        local_scope,
        CreateTaskRequest(
            TaskId(TASK_ID), "Task", b"", (), TaskPriority.NORMAL, (), before.store_revision
        ),
    )
    child_root = tmp_path / "child"
    child_bytes_before: dict[Path, bytes] | None = None
    if child_state != "missing":
        child_root.mkdir()
        actual_id = (
            "sto_019f0000000070008000000000000002" if child_state == "wrong-id" else child_id.root
        )
        InitializeStore(repository, repository, locks, views).execute(
            InitRequest(child_root, actual_id, "Child", "UTC")
        )
        child_location = location_from_root(child_root / ".untaped" / "orchestration")
        if child_state == "invalid":
            child_location.real_root.joinpath("store.toml").write_bytes(b"invalid = [")
        child_bytes_before = _files(child_location.real_root)
    _register_child(
        repository,
        parent,
        child_id,
        "../../../child/.untaped/orchestration",
    )
    resolved = FederationService(repository, locks).load(parent, local=False, headers_only=False)
    assert not resolved.completeness.complete

    with pytest.raises(InvalidMutationState):
        ChangeLink(executor, repository).add(
            _resolved_scope(repository, resolved),
            LinkRequest(
                task.record.metadata.id,
                LinkRelation.GOVERNED_BY,
                child_id,
                DecisionId("dec_019f0000000070008000000000000002"),
                task.record.revision,
            ),
        )
    if child_bytes_before is not None:
        assert _files(child_location.real_root) == child_bytes_before


@pytest.mark.parametrize("child_state", ["missing", "invalid"])
def test_registered_child_failure_allows_local_decision_update_and_evidence_only(
    tmp_path: Path,
    child_state: str,
) -> None:
    parent_repo = tmp_path / "parent"
    parent_repo.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    child_id = StoreId("sto_019f0000000070008000000000000001")
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(parent_repo, STORE_ID, "Parent", "UTC", decisions_only=True)
    )
    parent = location_from_root(parent_repo / ".untaped" / "orchestration")
    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    local_scope = _local_scope(repository, parent)
    before = repository.load_local(parent, headers_only=False)
    decision = CreateDecision(executor, repository, Clock()).execute(
        local_scope,
        CreateDecisionRequest(
            DecisionId("dec_019f0000000070008000000000000001"),
            "Decision",
            b"",
            (),
            before.store_revision,
        ),
    )
    child_location = None
    child_bytes_before = None
    if child_state == "invalid":
        child_repo = tmp_path / "child"
        child_repo.mkdir()
        InitializeStore(repository, repository, locks, views).execute(
            InitRequest(child_repo, child_id.root, "Child", "UTC", decisions_only=True)
        )
        child_location = location_from_root(child_repo / ".untaped" / "orchestration")
        child_location.real_root.joinpath("store.toml").write_bytes(b"invalid = [")
        child_bytes_before = _files(child_location.real_root)
    _register_child(
        repository,
        parent,
        child_id,
        "../../../child/.untaped/orchestration",
    )
    resolved = FederationService(repository, locks).load(parent, local=False, headers_only=False)
    assert not resolved.completeness.complete
    scope = _resolved_scope(repository, resolved)
    updated = UpdateDecision(executor, repository).execute(
        scope,
        UpdateDecisionRequest(
            decision.record.metadata.id,
            decision.record.revision,
            title="Clarified",
        ),
    )
    evidence = ChangeEvidence(executor, repository)
    request = EvidenceRequest(
        decision.record.metadata.id,
        EvidenceRelation.TRACKED_BY,
        EvidenceReference("url:https://example.com/context"),
        updated.record.revision,
    )
    added = evidence.add(scope, request)
    removed = evidence.remove(
        scope,
        EvidenceRequest(
            request.item_id,
            request.relation,
            request.reference,
            added.record.revision,
        ),
    )

    assert removed.record.metadata.title == "Clarified"
    assert removed.record.metadata.evidence == ()
    if child_location is not None and child_bytes_before is not None:
        assert _files(child_location.real_root) == child_bytes_before


def test_registered_child_rejects_missing_and_ambiguous_target_ids(tmp_path: Path) -> None:
    parent_repo = tmp_path / "parent"
    child_repo = tmp_path / "child"
    parent_repo.mkdir()
    child_repo.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    child_id = StoreId("sto_019f0000000070008000000000000001")
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(parent_repo, STORE_ID, "Parent", "UTC")
    )
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(child_repo, child_id.root, "Child", "UTC")
    )
    parent = location_from_root(parent_repo / ".untaped" / "orchestration")
    child = location_from_root(child_repo / ".untaped" / "orchestration")
    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    parent_scope = _local_scope(repository, parent)
    parent_before = repository.load_local(parent, headers_only=False)
    task = CreateTask(executor, repository, Clock()).execute(
        parent_scope,
        CreateTaskRequest(
            TaskId(TASK_ID), "Task", b"", (), TaskPriority.NORMAL, (), parent_before.store_revision
        ),
    )
    _register_child(
        repository,
        parent,
        child_id,
        "../../../child/.untaped/orchestration",
    )
    complete = FederationService(repository, locks).load(parent, local=False, headers_only=False)
    missing_id = DecisionId("dec_019f0000000070008000000000000002")
    with pytest.raises(RelationConflict, match="target item"):
        ChangeLink(executor, repository).add(
            _resolved_scope(repository, complete),
            LinkRequest(
                task.record.metadata.id,
                LinkRelation.GOVERNED_BY,
                child_id,
                missing_id,
                task.record.revision,
            ),
        )

    child_local = _local_scope(repository, child)
    child_before = repository.load_local(child, headers_only=False)
    decision = CreateDecision(executor, repository, Clock()).execute(
        child_local,
        CreateDecisionRequest(missing_id, "Decision", b"", (), child_before.store_revision),
    )
    duplicate = child.real_root / "decisions" / f"{missing_id.root}-duplicate.md"
    duplicate.write_bytes(repository.read_file(child, decision.record.path).content)
    ambiguous = FederationService(repository, locks).load(parent, local=False, headers_only=False)
    child_before_attempt = _files(child.real_root)
    with pytest.raises(InvalidMutationState):
        ChangeLink(executor, repository).add(
            _resolved_scope(repository, ambiguous),
            LinkRequest(
                task.record.metadata.id,
                LinkRelation.GOVERNED_BY,
                child_id,
                missing_id,
                task.record.revision,
            ),
        )
    assert _files(child.real_root) == child_before_attempt
