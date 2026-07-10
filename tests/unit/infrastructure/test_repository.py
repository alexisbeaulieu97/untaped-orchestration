import hashlib
import io
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import (
    DECISION_ID,
    TASK_ID,
    decision_bytes,
    task_bytes,
)
from untaped_orchestration.application.results import FileDeletion, FileReplacement
from untaped_orchestration.infrastructure.codec import ItemCodec
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


STANDARD_REPLACE_EVENTS = [
    "open-temp",
    "flush",
    "fsync-temp",
    "replace",
    "fsync-parent",
    "before-ack",
]


class BoundedReader(io.BytesIO):
    def __init__(self, raw: bytes) -> None:
        super().__init__(raw)
        self.max_request = 0
        self.unbounded_reads = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            self.unbounded_reads += 1
            raise AssertionError("streaming item reads must always be bounded")
        self.max_request = max(self.max_request, size)
        return super().read(size)

    def readline(self, size: int = -1) -> bytes:
        if size < 0:
            self.unbounded_reads += 1
            raise AssertionError("streaming item line reads must always be bounded")
        self.max_request = max(self.max_request, size)
        return super().readline(size)


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


def test_headers_only_repository_never_materializes_an_item_with_read_bytes(
    local_store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = local_store / "decisions" / f"{DECISION_ID}-choice.md"
    item.parent.mkdir()
    item.write_bytes(decision_bytes())
    original = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.parent == item.parent:
            raise AssertionError("header-only item load used Path.read_bytes")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    snapshot = FilesystemStoreRepository().load_local(
        location_from_root(local_store), headers_only=True
    )

    assert snapshot.records[0].body is None


def test_streaming_header_read_is_bounded_and_hashes_an_oversized_body_exactly() -> None:
    canonical = decision_bytes()
    closing_end = canonical.index(b"+++\n", 4) + 4
    raw = canonical[:closing_end] + b"x" * (1024 * 1024 + 1)
    stream = BoundedReader(raw)

    result = ItemCodec().parse_stream(
        stream,
        relative_path=PurePosixPath(f"decisions/{DECISION_ID}-choice.md"),
        headers_only=True,
    )

    assert result.metadata is None
    assert result.body is None
    assert result.diagnostic is not None
    assert result.diagnostic.code == "ORC001"
    assert result.diagnostic.field == "body"
    assert result.revision.root == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert result.size == len(raw)
    assert stream.unbounded_reads == 0
    assert stream.max_request <= 64 * 1024


def test_streaming_full_body_mode_retains_only_a_valid_bounded_body() -> None:
    raw = decision_bytes()
    stream = BoundedReader(raw)

    result = ItemCodec().parse_stream(
        stream,
        relative_path=PurePosixPath(f"decisions/{DECISION_ID}-choice.md"),
        headers_only=False,
    )

    assert result.diagnostic is None
    assert result.metadata is not None
    assert result.body == b"The envelope is machine-owned.\n"
    assert result.revision == file_revision(raw)
    assert stream.max_request <= 64 * 1024


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

    assert filesystem.events == STANDARD_REPLACE_EVENTS
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


@pytest.mark.parametrize(
    ("relative_path", "created_events"),
    [
        (
            PurePosixPath(f"tasks/{TASK_ID}-first.md"),
            ["mkdir:tasks", "fsync-dir-parent:."],
        ),
        (
            PurePosixPath(f"decisions/{DECISION_ID}-first.md"),
            ["mkdir:decisions", "fsync-dir-parent:."],
        ),
        (
            PurePosixPath(f"archive/tasks/{TASK_ID}-first.md"),
            [
                "mkdir:archive",
                "fsync-dir-parent:.",
                "mkdir:archive/tasks",
                "fsync-dir-parent:archive",
            ],
        ),
        (
            PurePosixPath("views/roadmap.md"),
            ["mkdir:views", "fsync-dir-parent:."],
        ),
    ],
)
def test_first_write_fsyncs_each_new_directory_entry_before_file_acknowledgement(
    local_store: Path,
    relative_path: PurePosixPath,
    created_events: list[str],
) -> None:
    filesystem = FaultInjectingFilesystem()

    FilesystemStoreRepository(atomic=filesystem).replace(
        location_from_root(local_store),
        FileReplacement(relative_path, b"durable\n"),
    )

    assert filesystem.events == created_events + STANDARD_REPLACE_EVENTS
    assert local_store.joinpath(*relative_path.parts).read_bytes() == b"durable\n"


@pytest.mark.parametrize(
    "boundary",
    [
        "mkdir:archive",
        "fsync-dir-parent:.",
        "mkdir:archive/tasks",
        "fsync-dir-parent:archive",
    ],
)
def test_archive_parent_creation_can_stop_and_retry_at_every_durable_boundary(
    local_store: Path,
    boundary: str,
) -> None:
    relative = PurePosixPath(f"archive/tasks/{TASK_ID}-closed.md")
    filesystem = FaultInjectingFilesystem(fail_at=boundary)
    repository = FilesystemStoreRepository(atomic=filesystem)

    with pytest.raises(InjectedBoundaryError) as captured:
        repository.replace(location_from_root(local_store), FileReplacement(relative, b"archive"))
    assert captured.value.boundary == boundary
    assert not local_store.joinpath(*relative.parts).exists()

    filesystem.fail_at = None
    filesystem.events.clear()
    repository.replace(location_from_root(local_store), FileReplacement(relative, b"archive"))
    assert local_store.joinpath(*relative.parts).read_bytes() == b"archive"
    assert filesystem.events[-6:] == STANDARD_REPLACE_EVENTS


def test_archive_destination_is_fully_durable_before_active_source_deletion(
    local_store: Path,
) -> None:
    active_relative = PurePosixPath(f"tasks/{TASK_ID}-close.md")
    archive_relative = PurePosixPath(f"archive/tasks/{TASK_ID}-close.md")
    active = local_store.joinpath(*active_relative.parts)
    active.parent.mkdir()
    active.write_bytes(b"active")
    filesystem = FaultInjectingFilesystem()
    repository = FilesystemStoreRepository(atomic=filesystem)

    repository.replace(
        location_from_root(local_store),
        FileReplacement(archive_relative, b"archive"),
    )
    archive_ack = len(filesystem.events) - 1
    repository.delete(location_from_root(local_store), FileDeletion(active_relative))

    assert filesystem.events[archive_ack] == "before-ack"
    assert filesystem.events[archive_ack - 1] == "fsync-parent"
    assert not active.exists()
    assert local_store.joinpath(*archive_relative.parts).read_bytes() == b"archive"
