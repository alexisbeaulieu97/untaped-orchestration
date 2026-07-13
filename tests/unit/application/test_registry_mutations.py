from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.builders import CHILD_STORE_ID, STORE_ID, registry_bytes, store_bytes, write_store
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
    listed = service.list_children(ListChildrenRequest(parent_location, limit=50))
    assert [(row.store_id.root, row.path) for row in listed.children] == [
        (CHILD_STORE_ID, _relative(parent, child))
    ]
    assert listed.registry_revision == added.registry_revision
    assert not listed.truncated
    assert child_before == _durable_files(child)

    with pytest.raises(ValueError, match=r"1\.\.200"):
        service.list_children(ListChildrenRequest(parent_location, limit=201))

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


def test_remove_locks_invalid_child_and_conflicts_if_identity_is_repaired(tmp_path: Path) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id="sto_019f0000000070008000000000000099")
    parent.joinpath("registry.toml").write_text(
        f'''schema = "untaped.orchestration.registry/v1"
store_id = "{STORE_ID}"

[[children]]
id = "{CHILD_STORE_ID}"
path = "{_relative(parent, child)}"
''',
        encoding="utf-8",
    )
    repository = FilesystemStoreRepository()
    location = location_from_root(parent)
    revision = repository.load_local(location, headers_only=True).registry_revision
    assert revision is not None
    locked: list[Path] = []

    class RepairOnLock:
        @contextmanager
        def acquire(self, locations, *, timeout):
            del timeout
            locked.extend(value.real_root for value in locations)
            child.joinpath("store.toml").write_bytes(store_bytes(store_id=CHILD_STORE_ID))
            child.joinpath("registry.toml").write_bytes(registry_bytes(store_id=CHILD_STORE_ID))
            yield

    service = FederationRegistryService(
        repository,
        repository,
        RepairOnLock(),
        MarkdownViewRenderer(),
        repository,
    )

    with pytest.raises(RegistryRevisionConflict):
        service.remove_child(RemoveChildRequest(location, CHILD_STORE_ID, revision))

    assert child.resolve() in locked
    assert CHILD_STORE_ID in parent.joinpath("registry.toml").read_text()


def test_invalid_absolute_child_path_is_rejected_before_any_store_io(tmp_path: Path) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)

    class CountingRepository(FilesystemStoreRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def discover(self, start: Path, override: Path | None = None):
            self.calls += 1
            return super().discover(start, override)

        def load_local(self, location, *, headers_only):
            self.calls += 1
            return super().load_local(location, headers_only=headers_only)

    repository = CountingRepository()
    service = FederationRegistryService(
        repository, repository, FileLockManager(), MarkdownViewRenderer(), repository
    )

    with pytest.raises(ValueError) as captured:
        service.add_child(
            AddChildRequest(
                location_from_root(parent),
                CHILD_STORE_ID,
                "/absolute/unsafe",
                expected_registry_revision=repository.inspect_administrative(
                    location_from_root(parent)
                ).registry_revision,
            )
        )

    assert captured.value.diagnostics[0].code == "ORC003"
    assert captured.value.diagnostics[0].field == "path"

    assert repository.calls == 0


def test_renderer_failure_returns_truthful_canonical_receipt(tmp_path: Path) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    repository = FilesystemStoreRepository()
    location = location_from_root(parent)
    before = repository.load_local(location, headers_only=True)

    class FailingViews(MarkdownViewRenderer):
        def expected(self, snapshot):
            raise ValueError("renderer failed")

    service = FederationRegistryService(
        repository, repository, FileLockManager(), FailingViews(), repository
    )
    result = service.add_child(
        AddChildRequest(
            location,
            CHILD_STORE_ID,
            _relative(parent, child),
            expected_registry_revision=before.registry_revision,
        )
    )

    assert result.canonical_applied
    assert not result.views_current
    assert result.changed_paths == (Path("registry.toml"),)
    assert len(result.intended_paths) == 5


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


def test_add_rejects_symlinked_child_real_path_swap_under_union_lock(tmp_path: Path) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    first = write_store(tmp_path / "first", store_id=CHILD_STORE_ID)
    second = write_store(tmp_path / "second", store_id=CHILD_STORE_ID)
    link = tmp_path / "child-link"
    link.symlink_to(first.parents[1], target_is_directory=True)
    linked_root = link / ".untaped" / "orchestration"
    repository = FilesystemStoreRepository()
    location = location_from_root(parent)
    before = repository.load_local(location, headers_only=True)

    class SwappingLocks:
        @contextmanager
        def acquire(
            self,
            locations: Sequence[StoreLocation],
            *,
            timeout: float,
        ) -> Iterator[None]:
            with FileLockManager().acquire(locations, timeout=timeout):
                link.unlink()
                link.symlink_to(second.parents[1], target_is_directory=True)
                yield

    service = FederationRegistryService(
        repository,
        repository,
        SwappingLocks(),
        MarkdownViewRenderer(),
        repository,
    )

    with pytest.raises(RegistryRevisionConflict, match="path changed"):
        service.add_child(
            AddChildRequest(
                location,
                CHILD_STORE_ID,
                _relative(parent, linked_root),
                expected_registry_revision=before.registry_revision,
            )
        )
