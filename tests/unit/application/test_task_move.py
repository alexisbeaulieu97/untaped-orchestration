from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from tests.unit.application.test_task_transition import create, state
from untaped_orchestration.application.items import RevisionConflict
from untaped_orchestration.application.tasks import (
    MoveTaskRequest,
    TaskLifecycleConflict,
)
from untaped_orchestration.domain.models import Revision
from untaped_orchestration.domain.ordering import PlacementAnchor, PlacementAnchorKind


def request(task, store_revision, *, parent=None, anchor=None, anchor_revision=None):
    return MoveTaskRequest(
        task.record.metadata.id,
        parent,
        task.record.metadata.parent,
        task.record.revision,
        store_revision,
        anchor or PlacementAnchor(PlacementAnchorKind.LAST),
        anchor_revision,
    )


def test_move_request_has_explicit_current_and_target_parent_plus_guards() -> None:
    assert [field.name for field in fields(MoveTaskRequest)] == [
        "item_id",
        "parent",
        "expected_parent",
        "expected_revision",
        "expected_store_revision",
        "placement",
        "expected_anchor_revision",
    ]


@pytest.mark.parametrize("kind", list(PlacementAnchorKind))
def test_move_supports_first_last_before_after_and_defaults_are_explicit(
    tmp_path: Path, kind: PlacementAnchorKind
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    first = create(repository, location, scope, executor, suffix=1)
    second = create(repository, location, scope, executor, suffix=2)
    parent = create(repository, location, scope, executor, suffix=3)
    current = repository.load_local(location, headers_only=False)
    anchor = first if kind in {PlacementAnchorKind.BEFORE, PlacementAnchorKind.AFTER} else None
    target_parent = None if anchor else parent.record.metadata.id
    moved = service.move(
        request(
            second,
            current.store_revision,
            parent=target_parent,
            anchor=PlacementAnchor(kind, anchor.record.metadata.id if anchor else None),
            anchor_revision=anchor.record.revision if anchor else None,
        )
    )
    assert moved.record.metadata.parent == target_parent


def test_relative_anchor_requires_exact_revision_and_target_scope(tmp_path: Path) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    first = create(repository, location, scope, executor, suffix=1)
    second = create(repository, location, scope, executor, suffix=2)
    current = repository.load_local(location, headers_only=False)
    with pytest.raises(TaskLifecycleConflict):
        service.move(
            request(
                second,
                current.store_revision,
                anchor=PlacementAnchor(PlacementAnchorKind.BEFORE, first.record.metadata.id),
                anchor_revision=Revision("sha256:" + "0" * 64),
            )
        )


def test_move_rejects_parent_cycle_and_stale_explicit_none_assertion(tmp_path: Path) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    parent = create(repository, location, scope, executor, suffix=1)
    child = create(repository, location, scope, executor, suffix=2)
    current = repository.load_local(location, headers_only=False)
    child = service.move(request(child, current.store_revision, parent=parent.record.metadata.id))
    current = repository.load_local(location, headers_only=False)
    with pytest.raises(TaskLifecycleConflict):
        service.move(request(parent, current.store_revision, parent=child.record.metadata.id))
    with pytest.raises(TaskLifecycleConflict):
        service.move(
            MoveTaskRequest(
                child.record.metadata.id,
                None,
                None,
                child.record.revision,
                current.store_revision,
                PlacementAnchor(PlacementAnchorKind.LAST),
            )
        )


def test_stale_move_conflicts_even_when_current_shape_is_the_requested_final_state(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    child = create(repository, location, scope, executor, suffix=1)
    parent = create(repository, location, scope, executor, suffix=2)
    current = repository.load_local(location, headers_only=False)
    move = request(child, current.store_revision, parent=parent.record.metadata.id)
    service.move(move)
    with pytest.raises(RevisionConflict):
        service.move(move)


def test_fresh_guard_reissue_of_exact_move_target_is_idempotent_noop(tmp_path: Path) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    child = create(repository, location, scope, executor, suffix=1)
    parent = create(repository, location, scope, executor, suffix=2)
    current = repository.load_local(location, headers_only=False)
    moved = service.move(request(child, current.store_revision, parent=parent.record.metadata.id))
    current = repository.load_local(location, headers_only=False)
    rank = moved.record.metadata.rank
    result = service.move(request(moved, current.store_revision, parent=parent.record.metadata.id))
    assert not result.receipt.applied
    assert not result.receipt.replayed
    assert result.record.metadata.rank == rank


def test_relative_move_requires_fresh_anchor_revision_even_for_exact_current_placement(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, service = state(tmp_path)
    anchor = create(repository, location, scope, executor, suffix=1)
    primary = create(repository, location, scope, executor, suffix=2)
    current = repository.load_local(location, headers_only=False)
    moved = service.move(
        request(
            primary,
            current.store_revision,
            anchor=PlacementAnchor(PlacementAnchorKind.BEFORE, anchor.record.metadata.id),
            anchor_revision=anchor.record.revision,
        )
    )
    anchor_path = location.real_root.joinpath(*anchor.record.path.parts)
    anchor_path.write_bytes(
        repository.item_bytes(
            anchor.record.metadata.model_copy(update={"title": "changed anchor"}),
            anchor.record.body or b"",
        )
    )
    current = repository.load_local(location, headers_only=False)
    current_primary = next(
        record for record in current.records if record.metadata.id == moved.record.metadata.id
    )
    with pytest.raises(TaskLifecycleConflict, match="anchor"):
        service.move(
            MoveTaskRequest(
                current_primary.metadata.id,
                current_primary.metadata.parent,
                current_primary.metadata.parent,
                current_primary.revision,
                current.store_revision,
                PlacementAnchor(PlacementAnchorKind.BEFORE, anchor.record.metadata.id),
                anchor.record.revision,
            )
        )
