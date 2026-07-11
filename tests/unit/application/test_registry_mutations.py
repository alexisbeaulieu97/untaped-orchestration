from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.builders import CHILD_STORE_ID, STORE_ID, write_store
from untaped_orchestration.application.federation import (
    AddChildRequest,
    FederationRegistryService,
    ListChildrenRequest,
    RegistryRevisionConflict,
    RemoveChildRequest,
)
from untaped_orchestration.application.results import StoreLocation
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def _relative(parent: Path, child: Path) -> str:
    return Path(os.path.relpath(child, parent)).as_posix()


def _service() -> FederationRegistryService:
    repository = FilesystemStoreRepository()
    return FederationRegistryService(
        repository,
        repository,
        FileLockManager(),
        MarkdownViewRenderer(),
        repository,
    )


def _durable_files(root: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".lock"
    }


def test_child_add_list_remove_use_exact_registry_revision_and_only_write_parent(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    parent_location = location_from_root(parent)
    repository = FilesystemStoreRepository()
    before = repository.load_local(parent_location, headers_only=True)
    child_before = _durable_files(child)
    service = _service()

    added = service.add_child(
        AddChildRequest(
            parent_location,
            CHILD_STORE_ID,
            _relative(parent, child),
            expected_registry_revision=before.registry_revision,
        )
    )

    assert added.canonical_applied
    assert added.changed_paths[0].as_posix() == "registry.toml"
    listed = service.list_children(ListChildrenRequest(parent_location))
    assert [(row.store_id.root, row.path) for row in listed.children] == [
        (CHILD_STORE_ID, _relative(parent, child))
    ]
    assert listed.registry_revision == added.registry_revision
    assert child_before == _durable_files(child)

    removed = service.remove_child(
        RemoveChildRequest(
            parent_location,
            CHILD_STORE_ID,
            expected_registry_revision=added.registry_revision,
        )
    )

    assert removed.changed_paths[0].as_posix() == "registry.toml"
    assert service.list_children(ListChildrenRequest(parent_location)).children == ()
    assert child_before == _durable_files(child)


def test_child_mutation_rejects_stale_exact_bytes_but_human_force_bypasses_only_guard(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    location = location_from_root(parent)
    repository = FilesystemStoreRepository()
    stale = repository.load_local(location, headers_only=True).registry_revision
    assert stale is not None
    parent.joinpath("registry.toml").write_bytes(
        parent.joinpath("registry.toml").read_bytes() + b"\n"
    )
    request = AddChildRequest(location, CHILD_STORE_ID, _relative(parent, child), stale)

    with pytest.raises(RegistryRevisionConflict):
        _service().add_child(request)

    forced = _service().add_child(
        AddChildRequest(
            location,
            CHILD_STORE_ID,
            _relative(parent, child),
            expected_registry_revision=None,
            force_current=True,
        )
    )
    assert forced.canonical_applied


def test_force_current_does_not_bypass_invalid_child_identity(tmp_path: Path) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    wrong = write_store(tmp_path / "wrong", store_id="sto_019f0000000070008000000000000099")
    location = location_from_root(parent)

    with pytest.raises(ValueError, match=r"complete|identity"):
        _service().add_child(
            AddChildRequest(
                location,
                CHILD_STORE_ID,
                _relative(parent, wrong),
                expected_registry_revision=None,
                force_current=True,
            )
        )


def test_add_rejects_registry_change_that_would_discover_an_unlocked_path(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    grandchild_id = "sto_019f0000000070008000000000000098"
    grandchild = write_store(tmp_path / "grandchild", store_id=grandchild_id)
    repository = FilesystemStoreRepository()
    location = location_from_root(parent)
    before = repository.load_local(location, headers_only=True)

    class MutatingLocks:
        @contextmanager
        def acquire(
            self,
            locations: Sequence[StoreLocation],
            *,
            timeout: float,
        ) -> Iterator[None]:
            with FileLockManager().acquire(locations, timeout=timeout):
                child.joinpath("registry.toml").write_text(
                    f'''schema = "untaped.orchestration.registry/v1"
store_id = "{CHILD_STORE_ID}"

[[children]]
id = "{grandchild_id}"
path = "{_relative(child, grandchild)}"
''',
                    encoding="utf-8",
                )
                yield

    service = FederationRegistryService(
        repository,
        repository,
        MutatingLocks(),
        MarkdownViewRenderer(),
        repository,
    )

    with pytest.raises(RegistryRevisionConflict, match="changed"):
        service.add_child(
            AddChildRequest(
                location,
                CHILD_STORE_ID,
                _relative(parent, child),
                expected_registry_revision=before.registry_revision,
            )
        )

    assert repository.load_local(location, headers_only=True).registry.children == ()
