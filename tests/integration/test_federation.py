from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from filelock import FileLock

from tests.builders import (
    CHILD_STORE_ID,
    DECISION_ID,
    STORE_ID,
    decision_bytes,
    store_bytes,
    store_root,
    write_store,
)
from untaped_orchestration.application.federation import FederationService
from untaped_orchestration.application.results import StoreLocation
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository

SECOND_CHILD_ID = "sto_019f0000000070008000000000000002"
GRANDCHILD_ID = "sto_019f0000000070008000000000000003"
WRONG_STORE_ID = "sto_019f0000000070008000000000000004"
MISSING_STORE_ID = "sto_019f0000000070008000000000000005"
INVALID_STORE_ID = "sto_019f0000000070008000000000000006"


def _relative(parent: Path, child: Path) -> str:
    return Path(os.path.relpath(child, parent)).as_posix()


def _write_registry(root: Path, store_id: str, *children: tuple[str, str]) -> None:
    records = "".join(
        f'\n[[children]]\nid = "{child_id}"\npath = "{path}"\n' for child_id, path in children
    )
    root.joinpath("registry.toml").write_text(
        f'schema = "untaped.orchestration.registry/v1"\nstore_id = "{store_id}"\n{records}',
        encoding="utf-8",
    )


def _service() -> FederationService:
    return FederationService(FilesystemStoreRepository(), FileLockManager())


class MutatingLocks:
    def __init__(self, mutation: Callable[[], None]) -> None:
        self._mutation = mutation

    @contextmanager
    def acquire(
        self,
        locations: Sequence[StoreLocation],
        *,
        timeout: float,
    ) -> Iterator[None]:
        with FileLockManager().acquire(locations, timeout=timeout):
            self._mutation()
            yield


@pytest.mark.integration
def test_recurses_through_sibling_dotdot_and_symlinked_repository_roots_only(
    tmp_path: Path,
) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    child = write_store(tmp_path / "child-real", store_id=CHILD_STORE_ID)
    grandchild = write_store(tmp_path / "grandchild", store_id=GRANDCHILD_ID)
    write_store(tmp_path / "ambient", store_id=SECOND_CHILD_ID)
    child_link = tmp_path / "child-link"
    child_link.symlink_to(child.parents[1], target_is_directory=True)
    linked_child_root = store_root(child_link)
    _write_registry(
        selected,
        STORE_ID,
        (CHILD_STORE_ID, _relative(selected, linked_child_root)),
    )
    _write_registry(
        child,
        CHILD_STORE_ID,
        (GRANDCHILD_ID, _relative(child, grandchild)),
    )

    result = _service().load(
        location_from_root(selected),
        local=False,
        headers_only=True,
    )

    assert result.completeness.complete
    assert {snapshot.store.id.root for snapshot in result.stores if snapshot.store is not None} == {
        STORE_ID,
        CHILD_STORE_ID,
        GRANDCHILD_ID,
    }
    child_snapshot = next(
        snapshot
        for snapshot in result.stores
        if snapshot.store is not None and snapshot.store.id.root == CHILD_STORE_ID
    )
    assert "child-link" in str(child_snapshot.location.root)
    assert child_snapshot.location.real_root == child.resolve()


@pytest.mark.integration
def test_missing_invalid_and_wrong_id_siblings_all_name_their_expected_store_ids(
    tmp_path: Path,
) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    invalid = write_store(tmp_path / "invalid", store_id=INVALID_STORE_ID)
    wrong = write_store(tmp_path / "wrong", store_id=WRONG_STORE_ID)
    invalid.joinpath("store.toml").write_bytes(b"schema =")
    leaked_record = wrong / "decisions" / f"{DECISION_ID}-must-not-leak.md"
    leaked_record.parent.mkdir()
    leaked_record.write_bytes(decision_bytes())
    _write_registry(
        selected,
        STORE_ID,
        (MISSING_STORE_ID, _relative(selected, store_root(tmp_path / "missing"))),
        (INVALID_STORE_ID, _relative(selected, invalid)),
        (SECOND_CHILD_ID, _relative(selected, wrong)),
    )

    result = _service().load(
        location_from_root(selected),
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert set(result.completeness.missing_store_ids) == {
        MISSING_STORE_ID,
        INVALID_STORE_ID,
        SECOND_CHILD_ID,
    }
    assert all(entry.expected_store_id.root for entry in result.completeness.entries)
    assert {entry.diagnostic.code for entry in result.completeness.entries} == {"ORC005"}
    assert len(result.stores) == 1
    assert result.stores == (result.selected,)
    assert all(snapshot.location.real_root != wrong.resolve() for snapshot in result.stores)
    by_expected_id = {entry.expected_store_id.root: entry for entry in result.completeness.entries}
    assert by_expected_id[MISSING_STORE_ID].diagnostic.severity == "warning"
    assert by_expected_id[INVALID_STORE_ID].diagnostic.severity == "error"
    assert by_expected_id[SECOND_CHILD_ID].diagnostic.severity == "error"
    assert [entry.diagnostic.severity for entry in result.completeness.entries] == [
        "error",
        "error",
        "warning",
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("edge_id", "edge_target", "message_fragment"),
    [
        (STORE_ID, "selected", "cycle"),
        (STORE_ID, "child", "cycle"),
        (SECOND_CHILD_ID, "selected", "cycle"),
    ],
)
def test_self_ancestor_and_normalized_path_cycles_are_incomplete_without_duplicate_locks(
    tmp_path: Path,
    edge_id: str,
    edge_target: str,
    message_fragment: str,
) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    _write_registry(selected, STORE_ID, (CHILD_STORE_ID, _relative(selected, child)))
    target = selected if edge_target == "selected" else child
    edge_path = "../orchestration" if edge_target == "child" else _relative(child, target)
    _write_registry(child, CHILD_STORE_ID, (edge_id, edge_path))

    result = _service().load(
        location_from_root(selected),
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert len(result.stores) == 2
    assert any(
        message_fragment in entry.diagnostic.message for entry in result.completeness.entries
    )
    assert all(entry.diagnostic.severity == "error" for entry in result.completeness.entries)


@pytest.mark.integration
def test_local_mode_ignores_an_invalid_registered_child(tmp_path: Path) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    invalid = write_store(tmp_path / "invalid", store_id=CHILD_STORE_ID)
    invalid.joinpath("store.toml").write_bytes(store_bytes(store_id=WRONG_STORE_ID))
    _write_registry(selected, STORE_ID, (CHILD_STORE_ID, _relative(selected, invalid)))

    result = _service().load(
        location_from_root(selected),
        local=True,
        headers_only=True,
    )

    assert result.completeness.complete
    assert len(result.stores) == 1
    assert result.selected.store is not None
    assert result.selected.store.id.root == STORE_ID


@pytest.mark.integration
def test_duplicate_ids_and_normalized_paths_across_sibling_subtrees_are_deduped(
    tmp_path: Path,
) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    first = write_store(tmp_path / "first", store_id=CHILD_STORE_ID)
    second = write_store(tmp_path / "second", store_id=SECOND_CHILD_ID)
    shared = write_store(tmp_path / "shared", store_id=GRANDCHILD_ID)
    duplicate_id_target = write_store(tmp_path / "other", store_id=GRANDCHILD_ID)
    _write_registry(
        selected,
        STORE_ID,
        (CHILD_STORE_ID, _relative(selected, first)),
        (SECOND_CHILD_ID, _relative(selected, second)),
    )
    _write_registry(
        first,
        CHILD_STORE_ID,
        (GRANDCHILD_ID, _relative(first, shared)),
    )
    _write_registry(
        second,
        SECOND_CHILD_ID,
        (GRANDCHILD_ID, _relative(second, duplicate_id_target)),
        (WRONG_STORE_ID, _relative(second, shared)),
    )

    result = _service().load(
        location_from_root(selected),
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert set(result.completeness.missing_store_ids) == {GRANDCHILD_ID, WRONG_STORE_ID}
    assert len(result.stores) == 4
    messages = [entry.diagnostic.message for entry in result.completeness.entries]
    assert any("duplicate store ID" in message for message in messages)
    assert any("duplicate normalized store path" in message for message in messages)
    assert all(entry.diagnostic.severity == "error" for entry in result.completeness.entries)


@pytest.mark.integration
def test_malformed_selected_anchor_uses_valid_registry_identity_without_fabricating_incompleteness(
    tmp_path: Path,
) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    selected.joinpath("store.toml").write_bytes(b"schema =")

    result = _service().load(
        location_from_root(selected),
        local=False,
        headers_only=True,
    )

    assert result.completeness.complete
    assert result.selected.store is None
    assert [diagnostic.path for diagnostic in result.selected.load_diagnostics] == ["store.toml"]


@pytest.mark.integration
def test_exact_anchor_byte_change_under_lock_is_a_conflict_even_when_typed_config_is_equal(
    tmp_path: Path,
) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    anchor = selected / "store.toml"
    service = FederationService(
        FilesystemStoreRepository(),
        MutatingLocks(lambda: anchor.write_bytes(anchor.read_bytes() + b"# changed\n")),
    )

    result = service.load(
        location_from_root(selected),
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert result.completeness.missing_store_ids == (STORE_ID,)
    assert result.completeness.entries[0].reason == "changed"
    assert result.completeness.entries[0].diagnostic.code == "ORC007"
    assert result.completeness.entries[0].diagnostic.severity == "error"


@pytest.mark.integration
def test_real_child_lock_timeout_names_only_the_registry_expected_child_id(
    tmp_path: Path,
) -> None:
    selected = write_store(tmp_path / "a-hub", store_id=STORE_ID)
    child = write_store(tmp_path / "z-child", store_id=CHILD_STORE_ID)
    _write_registry(selected, STORE_ID, (CHILD_STORE_ID, _relative(selected, child)))
    held = FileLock(child / ".lock")
    held.acquire()
    try:
        result = FederationService(
            FilesystemStoreRepository(),
            FileLockManager(),
            lock_timeout=0.01,
        ).load(
            location_from_root(selected),
            local=False,
            headers_only=True,
        )
    finally:
        held.release()

    assert not result.completeness.complete
    assert result.completeness.missing_store_ids == (CHILD_STORE_ID,)
    assert result.completeness.entries[0].reason == "timeout"
    assert result.completeness.entries[0].diagnostic.code == "ORC007"


@pytest.mark.integration
def test_wrong_id_duplicate_of_valid_subtree_id_cannot_leak_or_poison_valid_store(
    tmp_path: Path,
) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    valid = write_store(tmp_path / "valid", store_id=CHILD_STORE_ID)
    wrong = write_store(tmp_path / "wrong", store_id=CHILD_STORE_ID)
    leaked = wrong / "decisions" / f"{DECISION_ID}-wrong-duplicate.md"
    leaked.parent.mkdir()
    leaked.write_bytes(decision_bytes())
    _write_registry(
        selected,
        STORE_ID,
        (CHILD_STORE_ID, _relative(selected, valid)),
        (SECOND_CHILD_ID, _relative(selected, wrong)),
    )

    result = _service().load(
        location_from_root(selected),
        local=False,
        headers_only=False,
    )

    assert not result.completeness.complete
    assert [snapshot.store.id.root for snapshot in result.stores if snapshot.store is not None] == [
        STORE_ID,
        CHILD_STORE_ID,
    ]
    assert all(snapshot.location.real_root != wrong.resolve() for snapshot in result.stores)
    valid_snapshot = next(
        snapshot
        for snapshot in result.stores
        if snapshot.store is not None and snapshot.store.id.root == CHILD_STORE_ID
    )
    assert valid_snapshot.location.real_root == valid.resolve()
    assert valid_snapshot.records == ()


@pytest.mark.integration
def test_real_discovery_reports_absent_child_as_warning_and_unsafe_anchors_as_errors(
    tmp_path: Path,
) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    symlinked = store_root(tmp_path / "symlinked")
    symlinked.mkdir(parents=True)
    target = tmp_path / "outside-store.toml"
    target.write_bytes(store_bytes(store_id=CHILD_STORE_ID))
    symlinked.joinpath("store.toml").symlink_to(target)
    nonregular = store_root(tmp_path / "nonregular")
    nonregular.mkdir(parents=True)
    nonregular.joinpath("store.toml").mkdir()
    absent = store_root(tmp_path / "absent")
    _write_registry(
        selected,
        STORE_ID,
        (CHILD_STORE_ID, _relative(selected, symlinked)),
        (SECOND_CHILD_ID, _relative(selected, nonregular)),
        (GRANDCHILD_ID, _relative(selected, absent)),
    )

    result = _service().load(
        location_from_root(selected),
        local=False,
        headers_only=True,
    )

    by_id = {entry.expected_store_id.root: entry for entry in result.completeness.entries}
    assert by_id[CHILD_STORE_ID].diagnostic.severity == "error"
    assert by_id[SECOND_CHILD_ID].diagnostic.severity == "error"
    assert by_id[GRANDCHILD_ID].diagnostic.severity == "warning"


@pytest.mark.integration
def test_stable_anchors_accept_under_lock_record_content_change_as_complete(
    tmp_path: Path,
) -> None:
    selected = write_store(tmp_path / "hub", store_id=STORE_ID)
    record = selected / "decisions" / f"{DECISION_ID}-changing.md"
    record.parent.mkdir()
    record.write_bytes(decision_bytes())
    changed = decision_bytes().replace(
        b"The envelope is machine-owned.\n",
        b"Changed while waiting for the lock.\n",
    )
    service = FederationService(
        FilesystemStoreRepository(),
        MutatingLocks(lambda: record.write_bytes(changed)),
    )

    result = service.load(
        location_from_root(selected),
        local=False,
        headers_only=False,
    )

    assert result.completeness.complete
    assert result.selected.records[0].body == b"Changed while waiting for the lock.\n"
    assert result.stores == (result.selected,)
