from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.builders import STORE_ID, TASK_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.items import (
    ChangeEvidence,
    ChangeLink,
    CreateDecision,
    CreateDecisionRequest,
    CreateTask,
    CreateTaskRequest,
    EvidenceRequest,
    ItemStateConflict,
    LinkRequest,
    MutationExecutionScope,
    MutationScope,
    RelationConflict,
    RevisionConflict,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    IncompleteStore,
)
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.evidence import EvidenceReference, EvidenceRelation
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import LinkRelation, TaskPriority
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

    execution = MutationExecutionScope((location,), location, load)
    scope = MutationScope(execution, execution)
    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    before = repository.load_local(location, headers_only=False)
    task = CreateTask(executor, repository, Clock()).execute(
        scope,
        CreateTaskRequest(
            TaskId(TASK_ID), "Task", b"", (), TaskPriority.NORMAL, (), before.store_revision
        ),
    )
    after_task = repository.load_local(location, headers_only=False)
    decision_id = DecisionId("dec_019f0000000070008000000000000001")
    decision = CreateDecision(executor, repository, Clock()).execute(
        scope,
        CreateDecisionRequest(decision_id, "Decision", b"", (), after_task.store_revision),
    )
    return repository, location, scope, executor, task, decision


def test_link_and_evidence_requests_own_only_their_named_fields() -> None:
    assert [field.name for field in fields(LinkRequest)] == [
        "source_id",
        "relation",
        "target_store_id",
        "target_id",
        "expected_revision",
    ]
    assert [field.name for field in fields(EvidenceRequest)] == [
        "item_id",
        "relation",
        "reference",
        "expected_revision",
    ]


def test_generic_links_allow_only_dependency_governance_and_follow_up(tmp_path: Path) -> None:
    repository, _location, scope, executor, task, decision = _state(tmp_path)
    links = ChangeLink(executor, repository)
    governed = links.add(
        scope,
        LinkRequest(
            TaskId(TASK_ID),
            LinkRelation.GOVERNED_BY,
            StoreId(STORE_ID),
            decision.record.metadata.id,
            task.record.revision,
        ),
    )
    assert governed.record.metadata.links[0].relation is LinkRelation.GOVERNED_BY
    with pytest.raises(RelationConflict):
        links.add(
            scope,
            LinkRequest(
                TaskId(TASK_ID),
                LinkRelation.SUPERSEDES,
                StoreId(STORE_ID),
                TaskId("tsk_019f0000000070008000000000000011"),
                governed.record.revision,
            ),
        )
    with pytest.raises(RelationConflict, match="active task source"):
        links.add(
            scope,
            LinkRequest(
                decision.record.metadata.id,
                LinkRelation.FOLLOW_UP_TO,
                StoreId(STORE_ID),
                TaskId(TASK_ID),
                decision.record.revision,
            ),
        )


def test_evidence_add_remove_canonicalizes_and_uses_exact_revision(tmp_path: Path) -> None:
    repository, _, scope, executor, task, _ = _state(tmp_path)
    evidence = ChangeEvidence(executor, repository)
    added = evidence.add(
        scope,
        EvidenceRequest(
            TaskId(TASK_ID),
            EvidenceRelation.TRACKED_BY,
            EvidenceReference("github-pr:Owner/Repo#7"),
            task.record.revision,
        ),
    )
    assert added.record.metadata.evidence[0].reference == EvidenceReference(
        "github-pr:owner/repo#7"
    )
    with pytest.raises(RevisionConflict):
        evidence.remove(
            scope,
            EvidenceRequest(
                TaskId(TASK_ID),
                EvidenceRelation.TRACKED_BY,
                EvidenceReference("github-pr:owner/repo#7"),
                task.record.revision,
            ),
        )
    removed = evidence.remove(
        scope,
        EvidenceRequest(
            TaskId(TASK_ID),
            EvidenceRelation.TRACKED_BY,
            EvidenceReference("github-pr:owner/repo#7"),
            added.record.revision,
        ),
    )
    assert removed.record.metadata.evidence == ()


def test_missing_and_wrong_locality_link_targets_are_rejected(tmp_path: Path) -> None:
    repository, _, scope, executor, task, _ = _state(tmp_path)
    links = ChangeLink(executor, repository)
    with pytest.raises(RelationConflict, match="target item"):
        links.add(
            scope,
            LinkRequest(
                TaskId(TASK_ID),
                LinkRelation.DEPENDS_ON,
                StoreId(STORE_ID),
                TaskId("tsk_019f0000000070008000000000000011"),
                task.record.revision,
            ),
        )


def test_dependency_add_and_remove_are_guarded_and_same_store(tmp_path: Path) -> None:
    repository, location, scope, executor, task, _ = _state(tmp_path)
    current = repository.load_local(location, headers_only=False)
    prerequisite = CreateTask(executor, repository, Clock()).execute(
        scope,
        CreateTaskRequest(
            TaskId("tsk_019f0000000070008000000000000011"),
            "Prerequisite",
            b"",
            (),
            TaskPriority.NORMAL,
            (),
            current.store_revision,
        ),
    )
    request = LinkRequest(
        TaskId(TASK_ID),
        LinkRelation.DEPENDS_ON,
        StoreId(STORE_ID),
        prerequisite.record.metadata.id,
        task.record.revision,
    )
    service = ChangeLink(executor, repository)
    added = service.add(scope, request)
    assert added.record.metadata.links[0].relation is LinkRelation.DEPENDS_ON
    with pytest.raises(RevisionConflict):
        service.remove(scope, request)
    removed = service.remove(
        scope,
        LinkRequest(
            request.source_id,
            request.relation,
            request.target_store_id,
            request.target_id,
            added.record.revision,
        ),
    )
    assert removed.record.metadata.links == ()


def test_archived_evidence_is_append_only(tmp_path: Path) -> None:
    repository, location, scope, executor, task, _ = _state(tmp_path)
    raw = repository.read_file(location, task.record.path).content
    active = location.real_root.joinpath(*task.record.path.parts)
    archive_path = location.real_root / "archive" / "tasks" / task.record.path.name
    archive_path.parent.mkdir(parents=True)
    archive_path.write_bytes(
        raw.replace(b'stage = "inbox"\n', b'closed_from = "inbox"\noutcome = "declined"\n').replace(
            b"waiting_on = []\n",
            b'waiting_on = []\nclosed_at = "2026-07-11T00:00:00.000Z"\nclose_note = "done"\n',
        )
    )
    active.unlink()
    archived = next(
        record
        for record in repository.load_local(location, headers_only=False).records
        if record.metadata.id == TaskId(TASK_ID)
    )
    evidence = ChangeEvidence(executor, repository)
    request = EvidenceRequest(
        TaskId(TASK_ID),
        EvidenceRelation.VERIFIED_BY,
        EvidenceReference("url:https://example.com/proof"),
        archived.revision,
    )
    added = evidence.add(scope, request)
    assert added.record.path.parts[:2] == ("archive", "tasks")
    with pytest.raises(ItemStateConflict, match="links are immutable"):
        ChangeLink(executor, repository).add(
            scope,
            LinkRequest(
                TaskId(TASK_ID),
                LinkRelation.FOLLOW_UP_TO,
                StoreId(STORE_ID),
                TaskId(TASK_ID),
                added.record.revision,
            ),
        )
    with pytest.raises(ItemStateConflict):
        evidence.remove(
            scope,
            EvidenceRequest(
                request.item_id,
                request.relation,
                request.reference,
                added.record.revision,
            ),
        )


def test_inactive_decision_allows_evidence_add_but_refuses_remove_and_generic_links(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, _task, decision = _state(tmp_path)
    raw = repository.read_file(location, decision.record.path).content
    retired = raw.replace(
        b"tags = []\n",
        b'tags = []\nretired_at = "2026-07-11T00:00:00.000Z"\nretire_note = "ended"\n',
    )
    location.real_root.joinpath(*decision.record.path.parts).write_bytes(retired)
    inactive = next(
        record
        for record in repository.load_local(location, headers_only=False).records
        if record.metadata.id == decision.record.metadata.id
    )
    evidence = ChangeEvidence(executor, repository)
    added = evidence.add(
        scope,
        EvidenceRequest(
            decision.record.metadata.id,
            EvidenceRelation.RELEASED_AS,
            EvidenceReference("github-release:Owner/Repo@v1"),
            inactive.revision,
        ),
    )
    assert added.record.metadata.evidence
    with pytest.raises(ItemStateConflict):
        evidence.remove(
            scope,
            EvidenceRequest(
                decision.record.metadata.id,
                EvidenceRelation.RELEASED_AS,
                EvidenceReference("github-release:owner/repo@v1"),
                added.record.revision,
            ),
        )
    with pytest.raises(ItemStateConflict):
        ChangeLink(executor, repository).add(
            scope,
            LinkRequest(
                decision.record.metadata.id,
                LinkRelation.FOLLOW_UP_TO,
                StoreId(STORE_ID),
                TaskId(TASK_ID),
                added.record.revision,
            ),
        )


@pytest.mark.parametrize(("reason", "severity"), [("missing", "warning"), ("invalid", "error")])
def test_decision_evidence_add_remove_uses_selected_local_policy(
    tmp_path: Path,
    reason: str,
    severity: str,
) -> None:
    repository, _location, scope, executor, _task, decision = _state(tmp_path)
    child_id = StoreId("sto_019f0000000070008000000000000001")

    def incomplete_load() -> FederatedSnapshot:
        current = scope.recursive.load()
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

    incomplete_scope = MutationScope(
        MutationExecutionScope(
            scope.recursive.locations,
            scope.recursive.selected,
            incomplete_load,
        ),
        scope.selected_local,
    )
    service = ChangeEvidence(executor, repository)
    request = EvidenceRequest(
        decision.record.metadata.id,
        EvidenceRelation.TRACKED_BY,
        EvidenceReference("url:https://example.com/context"),
        decision.record.revision,
    )
    added = service.add(incomplete_scope, request)
    removed = service.remove(
        incomplete_scope,
        EvidenceRequest(
            request.item_id,
            request.relation,
            request.reference,
            added.record.revision,
        ),
    )

    assert removed.record.metadata.evidence == ()


def test_cross_store_governance_validates_federated_target_without_mutating_target(
    tmp_path: Path,
) -> None:
    parent_repo = tmp_path / "parent"
    child_repo = tmp_path / "child"
    parent_repo.mkdir()
    child_repo.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    child_store_id = StoreId("sto_019f0000000070008000000000000001")
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(parent_repo, STORE_ID, "Parent", "UTC")
    )
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(child_repo, child_store_id.root, "Child", "UTC", decisions_only=True)
    )
    parent = location_from_root(parent_repo / ".untaped" / "orchestration")
    child = location_from_root(child_repo / ".untaped" / "orchestration")

    def child_load() -> FederatedSnapshot:
        selected = repository.load_local(child, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    child_execution = MutationExecutionScope((child,), child, child_load)
    child_scope = MutationScope(child_execution, child_execution)
    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    child_before = repository.load_local(child, headers_only=False)
    decision = CreateDecision(executor, repository, Clock()).execute(
        child_scope,
        CreateDecisionRequest(
            DecisionId("dec_019f0000000070008000000000000002"),
            "External ruling",
            b"",
            (),
            child_before.store_revision,
        ),
    )
    parent_before = repository.load_local(parent, headers_only=False)

    def parent_load() -> FederatedSnapshot:
        selected = repository.load_local(parent, headers_only=False)
        target = repository.load_local(child, headers_only=False)
        return FederatedSnapshot(selected, (selected, target), Completeness())

    def parent_local_load() -> FederatedSnapshot:
        selected = repository.load_local(parent, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    parent_scope = MutationScope(
        MutationExecutionScope((parent, child), parent, parent_load),
        MutationExecutionScope(
            (parent,),
            parent,
            parent_local_load,
        ),
    )
    task = CreateTask(executor, repository, Clock()).execute(
        parent_scope,
        CreateTaskRequest(
            TaskId(TASK_ID),
            "Task",
            b"",
            (),
            TaskPriority.NORMAL,
            (),
            parent_before.store_revision,
        ),
    )
    target_bytes_before = {
        path.relative_to(child.real_root): path.read_bytes()
        for path in child.real_root.rglob("*")
        if path.is_file() and path.name != ".lock"
    }
    with pytest.raises(RelationConflict, match="same-store"):
        ChangeLink(executor, repository).add(
            parent_scope,
            LinkRequest(
                TaskId(TASK_ID),
                LinkRelation.DEPENDS_ON,
                child_store_id,
                decision.record.metadata.id,
                task.record.revision,
            ),
        )
    linked = ChangeLink(executor, repository).add(
        parent_scope,
        LinkRequest(
            TaskId(TASK_ID),
            LinkRelation.GOVERNED_BY,
            child_store_id,
            decision.record.metadata.id,
            task.record.revision,
        ),
    )
    target_bytes_after = {
        path.relative_to(child.real_root): path.read_bytes()
        for path in child.real_root.rglob("*")
        if path.is_file() and path.name != ".lock"
    }
    assert linked.record.metadata.links[0].target_store_id == child_store_id
    assert target_bytes_after == target_bytes_before
