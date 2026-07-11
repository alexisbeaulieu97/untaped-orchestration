from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.unit.application.test_decision_lifecycle import (
    create_decision,
    pin,
    supersede_request,
)
from tests.unit.application.test_task_transition import Clock, state
from untaped_orchestration.application.decisions import (
    DecisionLifecycleConflict,
    DecisionService,
    RetireDecisionRequest,
)
from untaped_orchestration.application.items import RevisionConflict
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.ports import FileDeletion, FileReplacement
from untaped_orchestration.application.results import Completeness, FederatedSnapshot
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.infrastructure.filesystem import AtomicFilesystem
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
        raise OSError("render failed")


def test_lifecycle_files_are_written_before_pin_replacement(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    predecessor = create_decision(repository, location, scope, executor, 1)
    retiring = create_decision(repository, location, scope, executor, 2)
    pin(repository, location, predecessor.record.metadata.id, retiring.record.metadata.id)

    writer = RecordingWriter(repository)
    executor = MutationExecutor(
        repository, writer, FileLockManager(), MarkdownViewRenderer(), projector=repository
    )
    lifecycle = DecisionService(executor, repository, Clock(), scope)
    current = repository.load_local(location, headers_only=False)
    successor = lifecycle.supersede(supersede_request((predecessor,), current.store_revision))
    canonical = [event for event in writer.events if not event[1].startswith("views/")]
    assert canonical == [
        ("replace", successor.record.path.as_posix()),
        ("replace", "store.toml"),
    ]

    writer.events.clear()
    current = repository.load_local(location, headers_only=False)
    retiring = next(r for r in current.records if r.metadata.id == retiring.record.metadata.id)
    lifecycle.retire(
        RetireDecisionRequest(
            retiring.metadata.id, "ended", retiring.revision, current.store_revision
        )
    )
    canonical = [event for event in writer.events if not event[1].startswith("views/")]
    assert canonical == [
        ("replace", retiring.path.as_posix()),
        ("replace", "store.toml"),
    ]


@pytest.mark.parametrize(
    ("family", "stop_after"), [("supersede", 1), ("supersede", 2), ("retire", 1), ("retire", 2)]
)
def test_every_canonical_phase_recovers_and_final_ack_loss_replays(
    tmp_path: Path, family: str, stop_after: int
) -> None:
    count = 0
    armed = False

    def hook(event: str) -> None:
        nonlocal count
        if armed and event == "before-ack":
            count += 1
            if count == stop_after:
                raise OSError("lost acknowledgement")

    repository, location, scope, executor, _ = state(tmp_path)
    # Recreate the repository with the fault-injecting atomic writer.
    repository = FilesystemStoreRepository(atomic=AtomicFilesystem(event_hook=hook))

    def load():
        from untaped_orchestration.application.results import Completeness, FederatedSnapshot

        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    from untaped_orchestration.application.items import MutationExecutionScope, MutationScope

    execution = MutationExecutionScope((location,), location, load)
    scope = MutationScope(execution, execution)
    executor = MutationExecutor(
        repository, repository, FileLockManager(), MarkdownViewRenderer(), projector=repository
    )
    value = create_decision(repository, location, scope, executor, 1)
    pin(repository, location, value.record.metadata.id)
    current = repository.load_local(location, headers_only=False)
    lifecycle = DecisionService(executor, repository, Clock(), scope)
    request = (
        supersede_request((value,), current.store_revision)
        if family == "supersede"
        else RetireDecisionRequest(
            value.record.metadata.id, "ended", value.record.revision, current.store_revision
        )
    )
    armed = True
    with pytest.raises(OSError, match="lost acknowledgement"):
        getattr(lifecycle, family)(request)
    armed = False
    if stop_after == 2 and family == "supersede":
        with pytest.raises(RevisionConflict, match="guarded base"):
            lifecycle.supersede(request)
        final = repository.load_local(location, headers_only=False)
        recovered = lifecycle.supersede(
            replace(request, expected_store_revision=final.store_revision)
        )
        assert not recovered.receipt.applied
        assert not recovered.receipt.replayed
        assert not recovered.receipt.views_current
    else:
        recovered = getattr(lifecycle, family)(request)
        if stop_after == 2:
            assert family == "retire"
            assert recovered.receipt.replayed
        else:
            assert recovered.receipt.canonical_applied


def test_successor_only_recovery_accepts_reversed_predecessor_order(tmp_path: Path) -> None:
    armed = False

    def hook(event: str) -> None:
        if armed and event == "before-ack":
            raise OSError("stop after successor")

    repository, location, scope, _, _ = state(tmp_path)
    repository = FilesystemStoreRepository(atomic=AtomicFilesystem(event_hook=hook))

    def load():
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    from untaped_orchestration.application.items import MutationExecutionScope, MutationScope

    execution = MutationExecutionScope((location,), location, load)
    scope = MutationScope(execution, execution)
    executor = MutationExecutor(
        repository,
        repository,
        FileLockManager(),
        MarkdownViewRenderer(),
        projector=repository,
    )
    first = create_decision(repository, location, scope, executor, 1)
    second = create_decision(repository, location, scope, executor, 2)
    pin(repository, location, first.record.metadata.id, second.record.metadata.id)
    current = repository.load_local(location, headers_only=False)
    request = supersede_request((first, second), current.store_revision)
    lifecycle = DecisionService(executor, repository, Clock(), scope)
    armed = True
    with pytest.raises(OSError, match="stop after successor"):
        lifecycle.supersede(request)
    armed = False
    reversed_request = replace(request, predecessors=tuple(reversed(request.predecessors)))
    recovered = lifecycle.supersede(reversed_request)
    assert recovered.receipt.canonical_applied
    assert recovered.record.metadata.links == tuple(
        sorted(recovered.record.metadata.links, key=lambda link: link.target.root)
    )


@pytest.mark.parametrize("family", ["supersede", "retire"])
def test_intermediate_phase_reports_inactive_pin_and_refuses_unrelated_divergence(
    tmp_path: Path, family: str
) -> None:
    armed = False

    def hook(event: str) -> None:
        if armed and event == "before-ack":
            raise OSError("stop after lifecycle phase")

    repository, location, scope, _, _ = state(tmp_path)
    repository = FilesystemStoreRepository(atomic=AtomicFilesystem(event_hook=hook))

    def load():
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    from untaped_orchestration.application.items import MutationExecutionScope, MutationScope

    execution = MutationExecutionScope((location,), location, load)
    scope = MutationScope(execution, execution)
    executor = MutationExecutor(
        repository,
        repository,
        FileLockManager(),
        MarkdownViewRenderer(),
        projector=repository,
    )
    value = create_decision(repository, location, scope, executor, 1)
    unrelated = create_decision(repository, location, scope, executor, 2)
    pin(repository, location, value.record.metadata.id)
    current = repository.load_local(location, headers_only=False)
    request = (
        supersede_request((value,), current.store_revision)
        if family == "supersede"
        else RetireDecisionRequest(
            value.record.metadata.id, "ended", value.record.revision, current.store_revision
        )
    )
    lifecycle = DecisionService(executor, repository, Clock(), scope)
    armed = True
    with pytest.raises(OSError):
        getattr(lifecycle, family)(request)
    armed = False
    interrupted = load()
    diagnostics = validate_snapshot(interrupted, require_children=True)
    assert any(
        value.code == "ORC006" and value.field.startswith("brief.pinned_decisions")
        for value in diagnostics
    )

    record = next(
        value
        for value in interrupted.selected.records
        if value.metadata.id == unrelated.record.metadata.id
    )
    path = location.real_root.joinpath(*record.path.parts)
    path.write_bytes(
        repository.item_bytes(
            record.metadata.model_copy(update={"title": "unrelated divergence"}),
            record.body or b"",
        )
    )
    with pytest.raises((RevisionConflict, DecisionLifecycleConflict)):
        getattr(lifecycle, family)(request)


@pytest.mark.parametrize("family", ["supersede", "retire"])
def test_view_failure_receipt_keeps_truthful_canonical_paths(tmp_path: Path, family: str) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    value = create_decision(repository, location, scope, executor, 1)
    pin(repository, location, value.record.metadata.id)
    current = repository.load_local(location, headers_only=False)
    executor = MutationExecutor(
        repository, repository, FileLockManager(), FailingViews(), projector=repository
    )
    lifecycle = DecisionService(executor, repository, Clock(), scope)
    result = (
        lifecycle.supersede(supersede_request((value,), current.store_revision))
        if family == "supersede"
        else lifecycle.retire(
            RetireDecisionRequest(
                value.record.metadata.id, "ended", value.record.revision, current.store_revision
            )
        )
    )
    assert result.receipt.canonical_applied
    assert not result.receipt.views_current
    assert set(result.receipt.changed_paths) < set(result.receipt.intended_paths)
    assert all(path.parts[0] != "views" for path in result.receipt.changed_paths)
