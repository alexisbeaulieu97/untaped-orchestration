from __future__ import annotations

from pathlib import Path

import pytest
from filelock import FileLock

from tests.builders import write_store
from untaped_orchestration.application.results import StoreLocation
from untaped_orchestration.infrastructure.filesystem import PathSafetyError, location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager, StoreLockTimeout


def test_acquires_multiple_store_locks_in_deterministic_real_path_order(tmp_path: Path) -> None:
    first = location_from_root(write_store(tmp_path / "z-store"))
    second = location_from_root(write_store(tmp_path / "a-store"))
    manager = FileLockManager()

    assert tuple(location.real_root for location in manager.ordered((first, second, first))) == (
        second.real_root,
        first.real_root,
    )
    with manager.acquire((first, second, first), timeout=0.1):
        assert second.real_root.joinpath(".lock").exists()
        assert first.real_root.joinpath(".lock").exists()


def test_lock_timeout_releases_already_acquired_locks_and_names_conflicting_store(
    tmp_path: Path,
) -> None:
    first = location_from_root(write_store(tmp_path / "a-store"))
    second = location_from_root(write_store(tmp_path / "b-store"))
    held = FileLock(second.real_root / ".lock")
    held.acquire()
    try:
        with (
            pytest.raises(StoreLockTimeout) as captured,
            FileLockManager().acquire((first, second), timeout=0.01),
        ):
            pytest.fail("lock acquisition should time out")
        assert captured.value.location == second

        probe = FileLock(first.real_root / ".lock")
        probe.acquire(timeout=0)
        probe.release()
    finally:
        held.release()


def test_rejects_casefold_store_aliases_before_acquiring_any_lock(tmp_path: Path) -> None:
    first = location_from_root(write_store(tmp_path / "Store"))
    alias = StoreLocation(root=first.root, real_root=Path(str(first.real_root).swapcase()))

    with pytest.raises(PathSafetyError):
        FileLockManager().ordered((first, alias))


def test_rejects_a_symlinked_lock_file(tmp_path: Path) -> None:
    location = location_from_root(write_store(tmp_path / "store"))
    outside = tmp_path / "outside.lock"
    outside.write_text("")
    location.real_root.joinpath(".lock").symlink_to(outside)

    with (
        pytest.raises(PathSafetyError),
        FileLockManager().acquire((location,), timeout=0.1),
    ):
        pytest.fail("symlinked locks must not be acquired")
