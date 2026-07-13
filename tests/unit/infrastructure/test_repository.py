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
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    FileDeletion,
    FileReplacement,
)
from untaped_orchestration.domain.diagnostics import DiagnosticError
from untaped_orchestration.domain.limits import FRONTMATTER_LIMIT, ITEM_FILE_LIMIT
from untaped_orchestration.infrastructure.codec import CodecError, ItemCodec
from untaped_orchestration.infrastructure.filesystem import (
    AtomicFilesystem,
    PathSafetyError,
    StoreNotFoundError,
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


def _direct_diagnostic(raw: bytes, relative_path: PurePosixPath):
    try:
        ItemCodec().parse(raw, relative_path=relative_path)
    except CodecError as error:
        return error.diagnostic
    return None


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf" + decision_bytes(),
        decision_bytes().removeprefix(b"+++") + b"x" * (1024 * 1024 + 1),
        decision_bytes()[:20],
        decision_bytes().replace(b'title = "Choice"', b'title = "\xffChoice"'),
        decision_bytes() + b"\xff",
        decision_bytes().replace(b'title = "Choice"', b"title ="),
        decision_bytes().replace(
            DECISION_ID.encode(),
            f"{DECISION_ID[:-1]}2".encode(),
            1,
        ),
        decision_bytes().replace(b'title = "Choice"', b"title =") + b"x" * (1024 * 1024 + 1),
    ],
    ids=[
        "byte-zero-bom",
        "missing-opener-plus-oversized",
        "missing-closing",
        "invalid-utf8-header",
        "invalid-utf8-body",
        "invalid-toml",
        "filename-id-mismatch",
        "invalid-toml-plus-oversized",
    ],
)
def test_streaming_diagnostics_match_direct_codec_exactly(raw: bytes) -> None:
    relative = PurePosixPath(f"decisions/{DECISION_ID}-choice.md")

    streamed = ItemCodec().parse_stream(
        BoundedReader(raw),
        relative_path=relative,
        headers_only=True,
    )

    assert streamed.diagnostic == _direct_diagnostic(raw, relative)


def test_streaming_diagnostic_location_matches_across_a_chunk_boundary() -> None:
    canonical = decision_bytes()
    closing_end = canonical.index(b"+++\n", 4) + 4
    header = canonical[:closing_end]
    raw = header + b"x" * (64 * 1024 - len(header)) + b"\xff"
    relative = PurePosixPath(f"decisions/{DECISION_ID}-choice.md")

    streamed = ItemCodec().parse_stream(
        BoundedReader(raw),
        relative_path=relative,
        headers_only=True,
    )

    assert streamed.diagnostic == _direct_diagnostic(raw, relative)


@pytest.mark.parametrize(
    "suffix",
    [
        b"\xc3\xa9\n",
        b"\xc3x\n",
    ],
    ids=["valid-split-sequence", "invalid-split-continuation"],
)
def test_streaming_utf8_multibyte_state_matches_direct_codec_across_chunks(
    suffix: bytes,
) -> None:
    canonical = decision_bytes()
    closing_end = canonical.index(b"+++\n", 4) + 4
    header = canonical[:closing_end]
    raw = header + b"x" * (64 * 1024 - len(header) - 1) + suffix
    relative = PurePosixPath(f"decisions/{DECISION_ID}-choice.md")

    streamed = ItemCodec().parse_stream(
        BoundedReader(raw),
        relative_path=relative,
        headers_only=True,
    )

    assert streamed.diagnostic == _direct_diagnostic(raw, relative)


def test_streaming_utf8_incomplete_eof_sequence_matches_direct_codec() -> None:
    raw = decision_bytes() + b"\xc3"
    relative = PurePosixPath(f"decisions/{DECISION_ID}-choice.md")

    streamed = ItemCodec().parse_stream(
        BoundedReader(raw),
        relative_path=relative,
        headers_only=True,
    )

    assert streamed.diagnostic == _direct_diagnostic(raw, relative)


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


def test_load_local_refuses_a_location_whose_exact_anchor_disappeared(local_store: Path) -> None:
    location = location_from_root(local_store)
    local_store.joinpath("store.toml").unlink()

    with pytest.raises(StoreNotFoundError):
        FilesystemStoreRepository().load_local(location, headers_only=True)


def test_registry_revision_hashes_exact_bytes_and_store_revision_excludes_views_and_noise(
    local_store: Path,
) -> None:
    repository = FilesystemStoreRepository()
    location = location_from_root(local_store)
    first = repository.load_local(location, headers_only=True)
    registry_raw = local_store.joinpath("registry.toml").read_bytes()
    store_config_raw = local_store.joinpath("store.toml").read_bytes()
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
    assert first.store_config_revision == file_revision(store_config_raw)
    assert first.store_revision == store_revision(canonical)

    views = local_store / "views"
    views.mkdir()
    views.joinpath("roadmap.md").write_bytes(b"derived")
    local_store.joinpath(".lock").write_bytes(b"lock")
    local_store.joinpath(".store.toml.untaped-tmp-orphan").write_bytes(b"temp")
    assert repository.load_local(location, headers_only=True).store_revision == first.store_revision


@pytest.mark.parametrize(
    "relative",
    (
        PurePosixPath("store.toml"),
        PurePosixPath("registry.toml"),
        PurePosixPath("AGENTS.md"),
        PurePosixPath("CLAUDE.md"),
    ),
)
def test_load_local_rejects_oversized_canonical_admin_and_instruction_files(
    local_store: Path,
    relative: PurePosixPath,
) -> None:
    local_store.joinpath(*relative.parts).write_bytes(b"x" * (FRONTMATTER_LIMIT + 1))

    with pytest.raises(DiagnosticError) as captured:
        FilesystemStoreRepository().load_local(
            location_from_root(local_store),
            headers_only=True,
        )

    assert captured.value.diagnostics[0].code == "ORC001"
    assert captured.value.diagnostics[0].path == relative.as_posix()


def test_load_local_accepts_exact_item_limit_and_rejects_limit_plus_one(
    local_store: Path,
) -> None:
    canonical = decision_bytes()
    metadata, body = canonical.split(b"+++\n")[1:]
    metadata_padding = 64 * 1024 - len(metadata)
    metadata = metadata + b"#" * (metadata_padding - 1) + b"\n"
    body = body + b"x" * (1024 * 1024 - len(body))
    exact = b"+++\n" + metadata + b"+++\n" + body
    assert len(exact) == ITEM_FILE_LIMIT
    item = local_store / "decisions" / f"{DECISION_ID}-choice.md"
    item.parent.mkdir()
    item.write_bytes(exact)

    snapshot = FilesystemStoreRepository().load_local(
        location_from_root(local_store),
        headers_only=True,
    )
    assert snapshot.records[0].metadata.id.root == DECISION_ID

    item.write_bytes(exact + b"x")
    with pytest.raises(DiagnosticError) as captured:
        FilesystemStoreRepository().load_local(
            location_from_root(local_store),
            headers_only=True,
        )
    assert captured.value.diagnostics[0].code == "ORC001"


def test_projection_reads_only_bounded_canonical_content_but_keeps_all_entries(
    local_store: Path,
) -> None:
    class ReadSpy:
        def __init__(self) -> None:
            self.paths: list[Path] = []

        def read_external(self, path: Path, *, limit: int, field: str) -> bytes:
            del limit, field
            self.paths.append(path)
            with path.open("rb") as stream:
                return stream.read()

    spy = ReadSpy()
    repository = FilesystemStoreRepository(external_files=spy)
    location = location_from_root(local_store)
    selected = repository.load_local(location, headers_only=False)
    current = FederatedSnapshot(selected, (selected,), Completeness())
    spy.paths.clear()
    noise = {
        PurePosixPath("views/roadmap.md"): b"v" * (2 * 1024 * 1024),
        PurePosixPath("notes.txt"): b"unexpected",
        PurePosixPath(".orphan.tmp"): b"temporary",
        PurePosixPath("scratch.md~"): b"editor",
        PurePosixPath(".store.toml.untaped-tmp-orphan"): b"atomic",
    }
    for relative, raw in noise.items():
        target = local_store.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

    projection = repository.project(current, location, (), ())

    assert {path.relative_to(local_store).as_posix() for path in spy.paths} == {
        "AGENTS.md",
        "CLAUDE.md",
        "registry.toml",
        "store.toml",
    }
    assert set(noise) <= {entry.path for entry in projection.entries}
    assert not set(noise) & projection.contents.keys()


def test_store_entry_enumeration_exposes_directories_symlinks_and_atomic_temporaries(
    local_store: Path,
    tmp_path: Path,
) -> None:
    local_store.joinpath("views").mkdir()
    local_store.joinpath("views", "nested").mkdir()
    local_store.joinpath(".store.toml.untaped-tmp-orphan").write_bytes(b"partial")
    local_store.joinpath("linked").symlink_to(tmp_path)

    entries = FilesystemStoreRepository().list_entries(location_from_root(local_store))

    assert {(value.path.as_posix(), value.kind) for value in entries} >= {
        ("views", "directory"),
        ("views/nested", "directory"),
        (".store.toml.untaped-tmp-orphan", "file"),
        ("linked", "symlink"),
    }


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
