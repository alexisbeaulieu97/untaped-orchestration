from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath

from tests.builders import write_store
from untaped_orchestration.application.mutations import IntendedMutation, MutationExecutor
from untaped_orchestration.application.ports import FileReplacement
from untaped_orchestration.application.results import Completeness, FederatedSnapshot
from untaped_orchestration.infrastructure.filesystem import file_revision, location_from_root
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository


class RecordingLocks:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = False

    @contextmanager
    def acquire(self, locations: Sequence, *, timeout: float) -> Iterator[None]:
        assert timeout == 10.0
        assert len(locations) == 2
        self.events.append("lock")
        self.active = True
        try:
            yield
        finally:
            self.active = False
            self.events.append("unlock")


class RecordingReader:
    def __init__(self, snapshots, events: list[str], locks: RecordingLocks) -> None:
        self.snapshots = list(snapshots)
        self.events = events
        self.locks = locks

    def load_local(self, location, *, headers_only: bool):
        assert self.locks.active
        self.events.append("reload")
        return self.snapshots.pop(0)


class RecordingWriter:
    def __init__(self, events: list[str], locks: RecordingLocks) -> None:
        self.events = events
        self.locks = locks

    def replace(self, location, change) -> None:
        assert self.locks.active
        self.events.append(f"write:{change.path.as_posix()}")

    def delete(self, location, change) -> None:
        assert self.locks.active
        self.events.append(f"delete:{change.path.as_posix()}")


class RecordingViews:
    def __init__(self, events: list[str], locks: RecordingLocks, *, fail: bool = False) -> None:
        self.events = events
        self.locks = locks
        self.fail = fail

    def expected(self, snapshot) -> Mapping[PurePosixPath, bytes]:
        assert self.locks.active
        self.events.append("render")
        if self.fail:
            raise OSError("renderer unavailable")
        return {PurePosixPath("views/decisions.md"): b"view\n"}


def _snapshots(tmp_path: Path):
    first_root = write_store(tmp_path / "first")
    second_root = write_store(tmp_path / "second", store_id="sto_019f0000000070008000000000000001")
    repository = FilesystemStoreRepository()
    first = repository.load_local(location_from_root(first_root), headers_only=False)
    second = repository.load_local(location_from_root(second_root), headers_only=False)
    current = FederatedSnapshot(first, (first, second), Completeness())
    after = replace(first, store_revision=file_revision(b"after"))
    intended = FederatedSnapshot(after, (after, second), Completeness())
    return first, second, current, intended


def test_shared_finalizer_enforces_the_exact_phase_order_under_the_complete_lock_set(
    tmp_path: Path,
) -> None:
    first, second, current, intended = _snapshots(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    reader = RecordingReader((intended.selected,), events, locks)
    writer = RecordingWriter(events, locks)
    views = RecordingViews(events, locks)

    def load() -> FederatedSnapshot:
        assert locks.active
        events.append("load")
        return current

    validations = iter(("validate-current", "validate-intended"))

    def validate(snapshot: FederatedSnapshot):
        del snapshot
        events.append(next(validations))
        return ()

    def guard(snapshot: FederatedSnapshot) -> None:
        assert snapshot is current
        events.append("guard")

    def build(snapshot: FederatedSnapshot) -> IntendedMutation:
        assert snapshot is current
        events.append("build")
        return IntendedMutation(
            snapshot=intended,
            replacements=(FileReplacement(PurePosixPath("registry.toml"), b"new\n"),),
        )

    result = MutationExecutor(reader, writer, locks, views, validator=validate).execute(
        locations=(second.location, first.location),
        selected=first.location,
        load=load,
        guard=guard,
        build=build,
    )

    assert events == [
        "lock",
        "load",
        "validate-current",
        "guard",
        "build",
        "validate-intended",
        "write:registry.toml",
        "reload",
        "render",
        "write:views/decisions.md",
        "unlock",
    ]
    assert result.canonical_applied
    assert result.views_current
    assert result.store_revision == file_revision(b"after")
    assert result.intended_paths == (
        PurePosixPath("registry.toml"),
        PurePosixPath("views/decisions.md"),
    )


def test_renderer_failure_preserves_canonical_success_and_reports_views_not_current(
    tmp_path: Path,
) -> None:
    first, second, current, intended = _snapshots(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    reader = RecordingReader((intended.selected,), events, locks)
    writer = RecordingWriter(events, locks)
    views = RecordingViews(events, locks, fail=True)

    result = MutationExecutor(reader, writer, locks, views, validator=lambda _: ()).execute(
        locations=(first.location, second.location),
        selected=first.location,
        load=lambda: current,
        guard=lambda _: None,
        build=lambda _: IntendedMutation(
            snapshot=intended,
            replacements=(FileReplacement(PurePosixPath("registry.toml"), b"new\n"),),
        ),
    )

    assert "write:registry.toml" in events
    assert result.applied
    assert result.canonical_applied
    assert not result.views_current
    assert result.changed_paths == (PurePosixPath("registry.toml"),)
