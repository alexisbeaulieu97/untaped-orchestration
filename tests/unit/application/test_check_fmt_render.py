from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from tests.builders import DECISION_ID, STORE_ID, decision_bytes
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.maintenance import (
    CheckStore,
    FormatStore,
    InvalidStoreState,
    RenderStore,
    RevisionConflict,
)
from untaped_orchestration.infrastructure.codec import CanonicalStoreFormatter
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


class RecordingWriter:
    def __init__(self, delegate: FilesystemStoreRepository) -> None:
        self.delegate = delegate
        self.replacements: list[PurePosixPath] = []

    def prepare(self, root: Path):
        return self.delegate.prepare(root)

    def replace(self, location, change) -> None:
        self.replacements.append(change.path)
        self.delegate.replace(location, change)

    def delete(self, location, change) -> None:
        self.delegate.delete(location, change)


class FailingViews:
    def expected(self, snapshot):
        del snapshot
        raise OSError("renderer unavailable")


def _initialized(tmp_path: Path):
    target = tmp_path / "repository"
    target.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(target, STORE_ID, "Store", "UTC")
    )
    root = target / ".untaped" / "orchestration"
    return root, repository, locks, views


def test_check_reports_missing_and_stale_views_with_sorted_orc008_diagnostics(
    tmp_path: Path,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    root.joinpath("views/backlog.md").unlink()
    root.joinpath("views/inbox.md").write_text("hand edited\n")
    location = location_from_root(root)

    result = CheckStore(repository, locks, views).execute(location)

    assert not result.valid
    assert not result.views_current
    assert [(value.code, value.path) for value in result.diagnostics] == [
        ("ORC008", "views/backlog.md"),
        ("ORC008", "views/inbox.md"),
    ]


def test_check_and_check_modes_never_call_the_writer(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    location = location_from_root(root)
    recording = RecordingWriter(repository)

    check = CheckStore(repository, locks, views).execute(location)
    fmt = FormatStore(repository, recording, locks, views, CanonicalStoreFormatter()).check(
        location
    )
    render = RenderStore(repository, recording, locks, views).check(location)

    assert check.valid
    assert fmt.matches
    assert render.matches
    assert recording.replacements == []


def test_render_write_is_a_fixpoint_and_repairs_every_applicable_view(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    location = location_from_root(root)
    root.joinpath("views/roadmap.md").write_text("stale\n")

    first = RenderStore(repository, repository, locks, views).write(location)
    second = RenderStore(repository, repository, locks, views).write(location)

    assert first.applied
    assert first.views_current
    assert PurePosixPath("views/roadmap.md") in first.changed_paths
    assert not second.applied
    assert second.matches
    assert second.changed_paths == ()


def test_fmt_check_compares_full_bytes_and_write_preserves_item_body(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    item = root / "decisions" / f"{DECISION_ID}-choice.md"
    item.parent.mkdir()
    body = b"Opaque\r\nbody with \\ and | bytes.\n"
    raw = decision_bytes()
    delimiter = raw.index(b"+++\n", 4) + 4
    noncanonical = raw[:4] + b"# removable comment\n" + raw[4:delimiter] + body
    item.write_bytes(noncanonical)
    store = root / "store.toml"
    store.write_bytes(b"# removable comment\n" + store.read_bytes())
    location = location_from_root(root)
    formatter = FormatStore(repository, repository, locks, views, CanonicalStoreFormatter())

    checked = formatter.check(location)
    revision = repository.load_local(location, headers_only=True).store_revision
    written = formatter.write(location, expected_store_revision=revision)

    assert not checked.matches
    assert {value.path for value in checked.comparisons if not value.matches} == {
        PurePosixPath("store.toml"),
        PurePosixPath(f"decisions/{DECISION_ID}-choice.md"),
    }
    assert written.canonical_applied
    assert item.read_bytes().endswith(body)
    assert b"removable comment" not in item.read_bytes()
    assert b"removable comment" not in store.read_bytes()
    assert formatter.check(location).matches


def test_fmt_write_rejects_a_stale_store_guard_before_writing(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    store = root / "store.toml"
    store.write_bytes(b"# comment\n" + store.read_bytes())
    recording = RecordingWriter(repository)
    location = location_from_root(root)

    with pytest.raises(RevisionConflict):
        FormatStore(repository, recording, locks, views, CanonicalStoreFormatter()).write(
            location, expected_store_revision="sha256:" + "0" * 64
        )

    assert recording.replacements == []
    assert store.read_bytes().startswith(b"# comment\n")


def test_fmt_refuses_invalid_metadata_without_rewriting_any_file(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    store = root / "store.toml"
    invalid = store.read_bytes().replace(b'visibility = "private"', b'visibility = "invalid"')
    store.write_bytes(invalid)
    recording = RecordingWriter(repository)
    location = location_from_root(root)
    revision = repository.load_local(location, headers_only=True).store_revision

    with pytest.raises(InvalidStoreState):
        FormatStore(repository, recording, locks, views, CanonicalStoreFormatter()).write(
            location, expected_store_revision=revision
        )

    assert recording.replacements == []
    assert store.read_bytes() == invalid


def test_check_reports_invalid_store_metadata_without_crashing(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    store = root / "store.toml"
    store.write_bytes(
        store.read_bytes().replace(b'visibility = "private"', b'visibility = "invalid"')
    )

    result = CheckStore(repository, locks, views).execute(location_from_root(root))

    assert not result.valid
    assert result.store_id == STORE_ID
    assert any(
        value.code == "ORC002" and value.path == "store.toml" for value in result.diagnostics
    )


def test_fmt_view_failure_preserves_canonical_write_and_reports_stale_views(
    tmp_path: Path,
) -> None:
    root, repository, locks, _ = _initialized(tmp_path)
    store = root / "store.toml"
    store.write_bytes(b"# comment\n" + store.read_bytes())
    location = location_from_root(root)
    revision = repository.load_local(location, headers_only=True).store_revision

    result = FormatStore(
        repository, repository, locks, FailingViews(), CanonicalStoreFormatter()
    ).write(location, expected_store_revision=revision)

    assert result.canonical_applied
    assert not result.views_current
    assert b"# comment" not in store.read_bytes()
