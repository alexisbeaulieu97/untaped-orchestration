from pathlib import Path, PurePosixPath

import pytest

from tests.builders import (
    DECISION_ID,
    TASK_ID,
    decision_bytes,
    task_bytes,
)
from untaped_orchestration.application.results import FileDeletion, FileReplacement
from untaped_orchestration.infrastructure.filesystem import (
    AtomicFilesystem,
    PathSafetyError,
    file_revision,
    location_from_root,
    store_revision,
)
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository


class InjectedBoundaryError(RuntimeError):
    def __init__(self, boundary: str) -> None:
        self.boundary = boundary
        super().__init__(boundary)


class FaultInjectingFilesystem(AtomicFilesystem):
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.events: list[str] = []
        self.fail_at = fail_at
        super().__init__(event_hook=self._record)

    def _record(self, event: str) -> None:
        self.events.append(event)
        if event == self.fail_at:
            raise InjectedBoundaryError(event)


def test_load_local_aggregates_valid_records_and_three_malformed_raw_references(
    local_store: Path,
) -> None:
    valid = local_store / "tasks" / f"{TASK_ID}-valid.md"
    bad_task = local_store / "tasks" / f"{TASK_ID[:-1]}1-bad-toml.md"
    bad_decision = local_store / "decisions" / f"{DECISION_ID[:-1]}2-bad-envelope.md"
    bad_archive = local_store / "archive" / "tasks" / f"{TASK_ID[:-1]}3-bad-shape.md"
    for path in (valid, bad_task, bad_decision, bad_archive):
        path.parent.mkdir(parents=True, exist_ok=True)
    valid.write_bytes(task_bytes())
    bad_task.write_bytes(task_bytes().replace(b"rank = 1000", b"rank ="))
    bad_decision.write_bytes(decision_bytes().removeprefix(b"+++\n"))
    bad_archive.write_bytes(task_bytes())

    snapshot = FilesystemStoreRepository().load_local(
        location_from_root(local_store), headers_only=False
    )

    assert snapshot.store is not None
    assert snapshot.registry is not None
    assert len(snapshot.records) == 1
    assert snapshot.records[0].path == PurePosixPath(f"tasks/{TASK_ID}-valid.md")
    assert snapshot.records[0].body == b"## Context\n\nOpaque Markdown body.\n"
    assert [diagnostic.path for diagnostic in snapshot.load_diagnostics] == [
        f"archive/tasks/{TASK_ID[:-1]}3-bad-shape.md",
        f"decisions/{DECISION_ID[:-1]}2-bad-envelope.md",
        f"tasks/{TASK_ID[:-1]}1-bad-toml.md",
    ]
    assert [reference.path.name for reference in snapshot.raw_index] == sorted(
        [valid.name, bad_task.name, bad_decision.name, bad_archive.name],
        key=lambda name: (name.casefold(), name),
    )


def test_headers_only_discards_bodies_but_preserves_exact_item_revisions(local_store: Path) -> None:
    item = local_store / "decisions" / f"{DECISION_ID}-choice.md"
    item.parent.mkdir()
    raw = decision_bytes()
    item.write_bytes(raw)

    snapshot = FilesystemStoreRepository().load_local(
        location_from_root(local_store), headers_only=True
    )

    assert snapshot.records[0].body is None
    assert snapshot.records[0].revision == file_revision(raw)


def test_malformed_store_keeps_registry_and_item_diagnostics(local_store: Path) -> None:
    local_store.joinpath("store.toml").write_bytes(b"schema =")
    item = local_store / "decisions" / f"{DECISION_ID}-broken.md"
    item.parent.mkdir()
    item.write_bytes(b"broken")

    snapshot = FilesystemStoreRepository().load_local(
        location_from_root(local_store), headers_only=True
    )

    assert snapshot.store is None
    assert snapshot.registry is not None
    assert [diagnostic.path for diagnostic in snapshot.load_diagnostics] == [
        f"decisions/{DECISION_ID}-broken.md",
        "store.toml",
    ]


def test_registry_revision_hashes_exact_bytes_and_store_revision_excludes_views_and_noise(
    local_store: Path,
) -> None:
    repository = FilesystemStoreRepository()
    location = location_from_root(local_store)
    first = repository.load_local(location, headers_only=True)
    registry_raw = local_store.joinpath("registry.toml").read_bytes()
    canonical = {
        path: local_store.joinpath(*path.parts).read_bytes()
        for path in (
            PurePosixPath("AGENTS.md"),
            PurePosixPath("CLAUDE.md"),
            PurePosixPath("registry.toml"),
            PurePosixPath("store.toml"),
        )
    }

    assert first.registry_revision == file_revision(registry_raw)
    assert first.store_revision == store_revision(canonical)

    views = local_store / "views"
    views.mkdir()
    views.joinpath("roadmap.md").write_bytes(b"derived")
    local_store.joinpath(".lock").write_bytes(b"lock")
    local_store.joinpath(".store.toml.untaped-tmp-orphan").write_bytes(b"temp")
    assert repository.load_local(location, headers_only=True).store_revision == first.store_revision


def test_read_raw_returns_exact_binary_content_and_metadata(local_store: Path) -> None:
    path = PurePosixPath(f"tasks/{TASK_ID}-broken.md")
    absolute = local_store.joinpath(*path.parts)
    absolute.parent.mkdir()
    absolute.write_bytes(b"\xffraw\x00")

    raw = FilesystemStoreRepository().read_raw(location_from_root(local_store), path)

    assert raw.path == path
    assert raw.content == b"\xffraw\x00"
    assert raw.size == 5
    assert raw.revision == file_revision(raw.content)


@pytest.mark.parametrize(
    ("boundary", "destination_changed"),
    [
        ("open-temp", False),
        ("flush", False),
        ("fsync-temp", False),
        ("replace", True),
        ("fsync-parent", True),
        ("before-ack", True),
    ],
)
def test_atomic_replacement_can_stop_at_every_durable_boundary(
    local_store: Path,
    boundary: str,
    destination_changed: bool,
) -> None:
    target = local_store / "AGENTS.md"
    target.write_bytes(b"old\n")
    filesystem = FaultInjectingFilesystem(fail_at=boundary)
    repository = FilesystemStoreRepository(atomic=filesystem)

    with pytest.raises(InjectedBoundaryError) as captured:
        repository.replace(
            location_from_root(local_store),
            FileReplacement(PurePosixPath("AGENTS.md"), b"new\n"),
        )

    assert captured.value.boundary == boundary
    assert target.read_bytes() == (b"new\n" if destination_changed else b"old\n")
    assert (
        filesystem.events
        == [
            "open-temp",
            "flush",
            "fsync-temp",
            "replace",
            "fsync-parent",
            "before-ack",
        ][: filesystem.events.index(boundary) + 1]
    )


def test_atomic_replacement_event_order_and_sibling_temp_cleanup(local_store: Path) -> None:
    filesystem = FaultInjectingFilesystem()
    repository = FilesystemStoreRepository(atomic=filesystem)

    repository.replace(
        location_from_root(local_store),
        FileReplacement(PurePosixPath("AGENTS.md"), b"new\n"),
    )

    assert filesystem.events == [
        "open-temp",
        "flush",
        "fsync-temp",
        "replace",
        "fsync-parent",
        "before-ack",
    ]
    assert local_store.joinpath("AGENTS.md").read_bytes() == b"new\n"
    assert list(local_store.glob(".AGENTS.md.untaped-tmp-*")) == []


def test_delete_unlinks_then_fsyncs_parent_before_ack(local_store: Path) -> None:
    target = local_store / "tasks" / f"{TASK_ID}-remove.md"
    target.parent.mkdir()
    target.write_bytes(task_bytes())
    filesystem = FaultInjectingFilesystem()
    repository = FilesystemStoreRepository(atomic=filesystem)

    repository.delete(
        location_from_root(local_store),
        FileDeletion(PurePosixPath(f"tasks/{TASK_ID}-remove.md")),
    )

    assert not target.exists()
    assert filesystem.events == ["fsync-parent", "before-ack"]


def test_writer_rejects_traversal_and_symlinked_destination_parents(
    tmp_path: Path,
    local_store: Path,
) -> None:
    repository = FilesystemStoreRepository()
    location = location_from_root(local_store)
    with pytest.raises(PathSafetyError):
        repository.replace(
            location,
            FileReplacement(PurePosixPath("../outside"), b"unsafe"),
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    local_store.joinpath("tasks").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSafetyError):
        repository.replace(
            location,
            FileReplacement(PurePosixPath(f"tasks/{TASK_ID}-unsafe.md"), task_bytes()),
        )
