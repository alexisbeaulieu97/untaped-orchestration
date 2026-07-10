from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import STORE_ID
from untaped_orchestration.application.bootstrap import (
    InitConflictError,
    InitializeStore,
    InitRequest,
)
from untaped_orchestration.infrastructure.filesystem import AtomicFilesystem
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer

EXPECTED_AGENTS = b"""# Local orchestration store

Use `untaped-orchestration` for all canonical reads and writes. Do not read generated
views as tool input. Keep unfinished tasks private, preserve revision guards, and get
explicit approval before pushes, merges, releases, publications, or external changes.
"""


def _service(*, atomic: AtomicFilesystem | None = None) -> InitializeStore:
    repository = FilesystemStoreRepository(atomic=atomic)
    return InitializeStore(repository, repository, FileLockManager(), MarkdownViewRenderer())


def _request(target: Path, **changes: object) -> InitRequest:
    values: dict[str, object] = {
        "target": target,
        "store_id": STORE_ID,
        "name": "Local store",
        "timezone": "America/Montreal",
    }
    values.update(changes)
    return InitRequest(**values)  # type: ignore[arg-type]


def _files(target: Path) -> dict[PurePosixPath, bytes]:
    root = target / ".untaped" / "orchestration"
    order = (
        PurePosixPath("store.toml"),
        PurePosixPath("registry.toml"),
        PurePosixPath("AGENTS.md"),
        PurePosixPath("CLAUDE.md"),
        PurePosixPath("views/roadmap.md"),
        PurePosixPath("views/backlog.md"),
        PurePosixPath("views/inbox.md"),
        PurePosixPath("views/decisions.md"),
    )
    return {
        path: root.joinpath(*path.parts).read_bytes()
        for path in order
        if root.joinpath(*path.parts).is_file()
    }


@pytest.mark.parametrize(
    ("options", "expected_paths"),
    [
        (
            {},
            (
                "store.toml",
                "registry.toml",
                "AGENTS.md",
                "CLAUDE.md",
                "views/roadmap.md",
                "views/backlog.md",
                "views/inbox.md",
                "views/decisions.md",
            ),
        ),
        (
            {"decisions_only": True},
            ("store.toml", "registry.toml", "AGENTS.md", "CLAUDE.md", "views/decisions.md"),
        ),
        (
            {"public": True},
            ("store.toml", "registry.toml", "AGENTS.md", "CLAUDE.md", "views/decisions.md"),
        ),
    ],
)
def test_init_writes_the_exact_deterministic_scaffold_without_eager_item_directories(
    tmp_path: Path,
    options: dict[str, object],
    expected_paths: tuple[str, ...],
) -> None:
    target = tmp_path / "repository"
    target.mkdir()

    result = _service().execute(_request(target, **options))

    files = _files(target)
    assert tuple(path.as_posix() for path in files) == expected_paths
    assert files[PurePosixPath("AGENTS.md")] == EXPECTED_AGENTS
    assert files[PurePosixPath("CLAUDE.md")] == b"@AGENTS.md\n"
    assert not (target / ".untaped" / "orchestration" / "tasks").exists()
    assert not (target / ".untaped" / "orchestration" / "decisions").exists()
    assert result.applied
    assert not result.replayed
    assert result.canonical_applied
    assert result.views_current
    assert tuple(path.as_posix() for path in result.intended_paths) == expected_paths
    store = files[PurePosixPath("store.toml")]
    assert (
        b'visibility = "public"' in store
        if options.get("public")
        else b'visibility = "private"' in store
    )
    assert (b"active_tasks = false" in store) is bool(options)


def test_init_accepts_every_exact_anchored_prefix_and_completed_replay(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    _service().execute(_request(reference))
    expected = _files(reference)
    ordered = tuple(expected)

    for length in range(1, len(ordered) + 1):
        target = tmp_path / f"prefix-{length}"
        target.mkdir()
        root = target / ".untaped" / "orchestration"
        for path in ordered[:length]:
            destination = root.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(expected[path])

        result = _service().execute(_request(target))

        assert _files(target) == expected
        assert result.replayed is (length == len(ordered))


@pytest.mark.parametrize("event", ["mkdir:views", "fsync-dir-parent:."])
def test_init_retries_after_durable_empty_views_parent_creation(
    tmp_path: Path,
    event: str,
) -> None:
    target = tmp_path / "repository"
    target.mkdir()

    def stop(boundary: str) -> None:
        if boundary == event:
            raise _StopBeforeAnchor(boundary)

    with pytest.raises(_StopBeforeAnchor):
        _service(atomic=AtomicFilesystem(event_hook=stop)).execute(_request(target))

    root = target / ".untaped" / "orchestration"
    assert root.joinpath("views").is_dir()
    assert not tuple(root.joinpath("views").iterdir())
    assert tuple(_files(target)) == (
        PurePosixPath("store.toml"),
        PurePosixPath("registry.toml"),
        PurePosixPath("AGENTS.md"),
        PurePosixPath("CLAUDE.md"),
    )

    recovered = _service().execute(_request(target))

    assert recovered.applied
    assert len(_files(target)) == 8


def test_init_rejects_empty_views_parent_without_the_exact_anchored_admin_prefix(
    tmp_path: Path,
) -> None:
    target = tmp_path / "repository"
    root = target / ".untaped" / "orchestration"
    root.joinpath("views").mkdir(parents=True)

    with pytest.raises(InitConflictError, match="unexpected directory"):
        _service().execute(_request(target))


@pytest.mark.parametrize("divergent_index", range(8))
def test_init_refuses_divergence_at_every_scaffold_position(
    tmp_path: Path,
    divergent_index: int,
) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    _service().execute(_request(reference))
    expected = _files(reference)
    ordered = tuple(expected)
    target = tmp_path / f"divergent-{divergent_index}"
    target.mkdir()
    root = target / ".untaped" / "orchestration"
    for path in ordered[: divergent_index + 1]:
        destination = root.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(expected[path])
    divergent = root.joinpath(*ordered[divergent_index].parts)
    divergent.write_bytes(divergent.read_bytes() + b"divergent\n")

    with pytest.raises(InitConflictError, match="divergent"):
        _service().execute(_request(target))

    assert divergent.read_bytes().endswith(b"divergent\n")


def test_init_refuses_an_unexpected_nonignored_file_instead_of_treating_it_as_a_prefix(
    tmp_path: Path,
) -> None:
    target = tmp_path / "repository"
    root = target / ".untaped" / "orchestration"
    root.mkdir(parents=True)
    root.joinpath("notes.md").write_text("unrelated\n")

    with pytest.raises(InitConflictError, match="unexpected"):
        _service().execute(_request(target))

    assert root.joinpath("notes.md").read_text() == "unrelated\n"
    assert not root.joinpath("store.toml").exists()


@pytest.mark.parametrize("unsafe", ["empty-directory", "nested-directory", "symlink", "nonregular"])
def test_init_refuses_unexpected_directory_and_symlink_entries(
    tmp_path: Path,
    unsafe: str,
) -> None:
    target = tmp_path / "repository"
    root = target / ".untaped" / "orchestration"
    root.mkdir(parents=True)
    if unsafe == "empty-directory":
        root.joinpath("notes").mkdir()
    elif unsafe == "nested-directory":
        root.joinpath("views", "nested").mkdir(parents=True)
    elif unsafe == "symlink":
        root.joinpath("notes").symlink_to(tmp_path)
    else:
        os.mkfifo(root / "pipe")

    with pytest.raises(InitConflictError, match=r"unexpected|unsafe"):
        _service().execute(_request(target))

    assert not root.joinpath("store.toml").exists()


class _StopBeforeAnchor(RuntimeError):
    pass


@pytest.mark.parametrize("event", ["open-temp", "flush", "fsync-temp"])
def test_pre_anchor_failure_removes_only_its_validated_temporary(
    tmp_path: Path,
    event: str,
) -> None:
    target = tmp_path / event
    target.mkdir()
    root = target / ".untaped" / "orchestration"

    def stop(boundary: str) -> None:
        if boundary == event:
            raise _StopBeforeAnchor(boundary)

    with pytest.raises(_StopBeforeAnchor):
        _service(atomic=AtomicFilesystem(event_hook=stop)).execute(_request(target))

    assert not root.joinpath("store.toml").exists()
    assert tuple(path for path in root.rglob("*") if path.is_file() and path.name != ".lock") == ()


class _AcknowledgementLost(RuntimeError):
    pass


class _FailAfterDurableWrite:
    def __init__(self, repository: FilesystemStoreRepository, stop_after: int) -> None:
        self._repository = repository
        self._stop_after = stop_after
        self._writes = 0

    def prepare(self, root: Path):
        return self._repository.prepare(root)

    def replace(self, location, change) -> None:
        self._repository.replace(location, change)
        self._writes += 1
        if self._writes == self._stop_after:
            raise _AcknowledgementLost(change.path.as_posix())

    def delete(self, location, change) -> None:
        self._repository.delete(location, change)


@pytest.mark.parametrize("stop_after", range(1, 9))
def test_init_recovers_after_acknowledgement_loss_at_every_durable_file_boundary(
    tmp_path: Path,
    stop_after: int,
) -> None:
    target = tmp_path / str(stop_after)
    target.mkdir()
    repository = FilesystemStoreRepository()
    failing = _FailAfterDurableWrite(repository, stop_after)
    service = InitializeStore(repository, failing, FileLockManager(), MarkdownViewRenderer())

    with pytest.raises(_AcknowledgementLost):
        service.execute(_request(target))

    recovered = _service().execute(_request(target))
    assert recovered.applied
    assert recovered.replayed is (stop_after == 8)
    assert len(_files(target)) == 8


@pytest.mark.parametrize(
    "name", ["line\nbreak", "line\rbreak", "line\u2028break", "line\u2029break"]
)
def test_init_rejects_store_name_line_breaks_before_any_canonical_write(
    tmp_path: Path,
    name: str,
) -> None:
    target = tmp_path / "repository"
    target.mkdir()

    with pytest.raises(ValueError, match="line breaks"):
        _service().execute(_request(target, name=name))

    root = target / ".untaped" / "orchestration"
    assert not root.exists() or not root.joinpath("store.toml").exists()


def test_init_flags_are_mutually_exclusive(tmp_path: Path) -> None:
    target = tmp_path / "repository"
    target.mkdir()

    with pytest.raises(ValueError, match="mutually exclusive"):
        _service().execute(_request(target, public=True, decisions_only=True))
