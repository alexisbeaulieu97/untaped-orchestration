from __future__ import annotations

from dataclasses import fields
from pathlib import Path, PurePosixPath

import pytest

from tests.unit.application.test_task_transition import Clock, state
from untaped_orchestration.application.decisions import (
    DecisionGuard,
    DecisionLifecycleConflict,
    DecisionService,
    RetireDecisionRequest,
    SupersedeDecisionRequest,
)
from untaped_orchestration.application.items import (
    CreateDecision,
    CreateDecisionRequest,
    RevisionConflict,
)
from untaped_orchestration.application.ports import FileReplacement
from untaped_orchestration.application.results import Completeness, FederatedSnapshot
from untaped_orchestration.application.validation import _graph_state
from untaped_orchestration.domain.graph import DecisionRef, DecisionState, decision_state
from untaped_orchestration.domain.ids import DecisionId, Slug
from untaped_orchestration.domain.models import Decision, LinkRelation


def create_decision(repository, location, scope, executor, suffix: int):
    current = repository.load_local(location, headers_only=False)
    return CreateDecision(executor, repository, Clock()).execute(
        scope,
        CreateDecisionRequest(
            DecisionId(f"dec_019f00000000700080000000000000{suffix:02d}"),
            f"Decision {suffix}",
            f"body-{suffix}".encode(),
            (Slug("architecture"),),
            current.store_revision,
        ),
    )


def pin(repository, location, *decision_ids: DecisionId) -> None:
    current = repository.load_local(location, headers_only=False)
    assert current.store is not None
    brief = current.store.brief.model_copy(update={"pinned_decisions": decision_ids})
    config = current.store.model_copy(update={"brief": brief})
    repository.replace(
        location, FileReplacement(PurePosixPath("store.toml"), repository.store_bytes(config))
    )


def service(executor, repository, scope) -> DecisionService:
    return DecisionService(executor, repository, Clock(), scope)


def supersede_request(predecessors, store_revision, *, successor_suffix=90, title="New ruling"):
    return SupersedeDecisionRequest(
        successor_id=DecisionId(f"dec_019f00000000700080000000000000{successor_suffix:02d}"),
        title=title,
        body=b"new body",
        tags=(Slug("architecture"),),
        predecessors=tuple(
            DecisionGuard(value.record.metadata.id, value.record.revision) for value in predecessors
        ),
        expected_store_revision=store_revision,
    )


def test_requests_are_narrow_frozen_lifecycle_contracts() -> None:
    assert [value.name for value in fields(DecisionGuard)] == ["item_id", "expected_revision"]
    assert [value.name for value in fields(SupersedeDecisionRequest)] == [
        "successor_id",
        "title",
        "body",
        "tags",
        "predecessors",
        "expected_store_revision",
    ]
    assert [value.name for value in fields(RetireDecisionRequest)] == [
        "item_id",
        "note",
        "expected_revision",
        "expected_store_revision",
    ]


def test_supersede_consolidates_predecessors_and_preserves_pin_order(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    unrelated = create_decision(repository, location, scope, executor, 1)
    first = create_decision(repository, location, scope, executor, 2)
    later = create_decision(repository, location, scope, executor, 3)
    tail = create_decision(repository, location, scope, executor, 4)
    pin(
        repository,
        location,
        unrelated.record.metadata.id,
        later.record.metadata.id,
        first.record.metadata.id,
        tail.record.metadata.id,
    )
    current = repository.load_local(location, headers_only=False)
    # Refresh guards after the direct store edit; item revisions are unchanged.
    request = supersede_request((first, later), current.store_revision)

    result = service(executor, repository, scope).supersede(request)
    successor = result.record.metadata
    assert isinstance(successor, Decision)
    assert {link.target for link in successor.links} == {
        first.record.metadata.id,
        later.record.metadata.id,
    }
    assert all(link.relation is LinkRelation.SUPERSEDES for link in successor.links)
    final = repository.load_local(location, headers_only=False)
    assert final.store is not None
    assert final.store.brief.pinned_decisions == (
        unrelated.record.metadata.id,
        successor.id,
        tail.record.metadata.id,
    )
    graph = _graph_state(FederatedSnapshot(final, (final,), Completeness()))
    assert (
        decision_state(DecisionRef(final.store.id, first.record.metadata.id), graph)
        is DecisionState.SUPERSEDED
    )


def test_supersede_rejects_stale_guards_duplicates_and_inactive_predecessors(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    first = create_decision(repository, location, scope, executor, 1)
    second = create_decision(repository, location, scope, executor, 2)
    current = repository.load_local(location, headers_only=False)
    lifecycle = service(executor, repository, scope)
    request = supersede_request((first,), current.store_revision)
    lifecycle.supersede(request)

    with pytest.raises(DecisionLifecycleConflict):
        lifecycle.supersede(
            supersede_request(
                (first, second), repository.load_local(location, headers_only=False).store_revision
            )
        )
    with pytest.raises(DecisionLifecycleConflict):
        lifecycle.supersede(
            supersede_request(
                (second, second), repository.load_local(location, headers_only=False).store_revision
            )
        )
    with pytest.raises(DecisionLifecycleConflict):
        lifecycle.supersede(
            SupersedeDecisionRequest(
                successor_id=second.record.metadata.id,
                title="bad",
                body=b"bad",
                tags=(),
                predecessors=(DecisionGuard(second.record.metadata.id, second.record.revision),),
                expected_store_revision=repository.load_local(
                    location, headers_only=False
                ).store_revision,
            )
        )


def test_lifecycle_requires_exact_predecessor_and_store_guards(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    predecessor = create_decision(repository, location, scope, executor, 1)
    guarded = repository.load_local(location, headers_only=False)
    create_decision(repository, location, scope, executor, 2)
    lifecycle = service(executor, repository, scope)
    with pytest.raises(RevisionConflict, match="store revision"):
        lifecycle.supersede(supersede_request((predecessor,), guarded.store_revision))

    current = repository.load_local(location, headers_only=False)
    stale = predecessor.record.revision.model_copy(update={"root": "sha256:" + "0" * 64})
    request = supersede_request((predecessor,), current.store_revision)
    request = request.__class__(
        request.successor_id,
        request.title,
        request.body,
        request.tags,
        (DecisionGuard(predecessor.record.metadata.id, stale),),
        request.expected_store_revision,
    )
    with pytest.raises(RevisionConflict, match="predecessor"):
        lifecycle.supersede(request)


def test_retired_and_superseded_decisions_refuse_both_lifecycle_commands(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    retired_source = create_decision(repository, location, scope, executor, 1)
    superseded_source = create_decision(repository, location, scope, executor, 2)
    lifecycle = service(executor, repository, scope)
    current = repository.load_local(location, headers_only=False)
    lifecycle.retire(
        RetireDecisionRequest(
            retired_source.record.metadata.id,
            "ended",
            retired_source.record.revision,
            current.store_revision,
        )
    )
    current = repository.load_local(location, headers_only=False)
    lifecycle.supersede(supersede_request((superseded_source,), current.store_revision))
    final = repository.load_local(location, headers_only=False)
    retired = next(
        value for value in final.records if value.metadata.id == retired_source.record.metadata.id
    )
    superseded = next(
        value
        for value in final.records
        if value.metadata.id == superseded_source.record.metadata.id
    )
    with pytest.raises(DecisionLifecycleConflict, match="active predecessors"):
        lifecycle.supersede(
            SupersedeDecisionRequest(
                DecisionId("dec_019f0000000070008000000000000091"),
                "New ruling",
                b"new body",
                (Slug("architecture"),),
                (DecisionGuard(retired.metadata.id, retired.revision),),
                final.store_revision,
            )
        )
    with pytest.raises(DecisionLifecycleConflict, match="superseded"):
        lifecycle.retire(
            RetireDecisionRequest(
                superseded.metadata.id,
                "ended",
                superseded.revision,
                final.store_revision,
            )
        )


def test_retire_sets_pair_then_removes_pin_and_refuses_empty_or_superseded(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    retiring = create_decision(repository, location, scope, executor, 1)
    predecessor = create_decision(repository, location, scope, executor, 2)
    pin(repository, location, retiring.record.metadata.id)
    current = repository.load_local(location, headers_only=False)
    lifecycle = service(executor, repository, scope)
    retired = lifecycle.retire(
        RetireDecisionRequest(
            retiring.record.metadata.id,
            "mechanism ended",
            retiring.record.revision,
            current.store_revision,
        )
    )
    assert retired.record.metadata.retire_note == "mechanism ended"
    final = repository.load_local(location, headers_only=False)
    assert final.store is not None and not final.store.brief.pinned_decisions
    with pytest.raises(DecisionLifecycleConflict):
        lifecycle.retire(
            RetireDecisionRequest(
                predecessor.record.metadata.id,
                " ",
                predecessor.record.revision,
                final.store_revision,
            )
        )


def test_exact_final_states_replay_but_divergence_and_stale_base_conflict(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    predecessor = create_decision(repository, location, scope, executor, 1)
    pin(repository, location, predecessor.record.metadata.id)
    current = repository.load_local(location, headers_only=False)
    lifecycle = service(executor, repository, scope)
    request = supersede_request((predecessor,), current.store_revision)
    lifecycle.supersede(request)
    assert lifecycle.supersede(request).receipt.replayed

    successor = next(
        r
        for r in repository.load_local(location, headers_only=False).records
        if r.metadata.id == request.successor_id
    )
    path = location.real_root.joinpath(*successor.path.parts)
    path.write_bytes(
        repository.item_bytes(
            successor.metadata.model_copy(update={"title": "diverged"}), successor.body or b""
        )
    )
    with pytest.raises((DecisionLifecycleConflict, RevisionConflict)):
        lifecycle.supersede(request)


def test_retire_final_state_replays_only_from_exact_reverse_projection(tmp_path: Path) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    value = create_decision(repository, location, scope, executor, 1)
    pin(repository, location, value.record.metadata.id)
    current = repository.load_local(location, headers_only=False)
    request = RetireDecisionRequest(
        value.record.metadata.id, "ended", value.record.revision, current.store_revision
    )
    lifecycle = service(executor, repository, scope)
    lifecycle.retire(request)
    assert lifecycle.retire(request).receipt.replayed
    retired = next(
        r
        for r in repository.load_local(location, headers_only=False).records
        if r.metadata.id == value.record.metadata.id
    )
    path = location.real_root.joinpath(*retired.path.parts)
    path.write_bytes(
        repository.item_bytes(
            retired.metadata.model_copy(update={"title": "changed"}), retired.body or b""
        )
    )
    with pytest.raises((DecisionLifecycleConflict, RevisionConflict)):
        lifecycle.retire(request)


def test_unpinned_supersede_has_no_admin_write_and_final_state_replays(
    tmp_path: Path,
) -> None:
    repository, location, scope, executor, _ = state(tmp_path)
    predecessor = create_decision(repository, location, scope, executor, 1)
    current = repository.load_local(location, headers_only=False)
    request = supersede_request((predecessor,), current.store_revision)
    lifecycle = service(executor, repository, scope)
    applied = lifecycle.supersede(request)
    assert PurePosixPath("store.toml") not in applied.receipt.changed_paths
    assert lifecycle.supersede(request).receipt.replayed
