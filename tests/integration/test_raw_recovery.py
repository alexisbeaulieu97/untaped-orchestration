from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from tests.builders import DECISION_ID, STORE_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.infrastructure.filesystem import (
    AmbiguousRawPrefixError,
    PathSafetyError,
    location_from_root,
    raw_reference_by_prefix,
)
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def _fixture(tmp_path: Path):
    target = tmp_path / "repository"
    target.mkdir()
    repository = FilesystemStoreRepository()
    InitializeStore(repository, repository, FileLockManager(), MarkdownViewRenderer()).execute(
        InitRequest(target, STORE_ID, "Local", "UTC")
    )
    location = location_from_root(target / ".untaped" / "orchestration")
    return repository, location


def test_raw_prefix_lookup_survives_invalid_toml_and_preserves_invalid_utf8(
    tmp_path: Path,
) -> None:
    repository, location = _fixture(tmp_path)
    path = PurePosixPath(f"decisions/{DECISION_ID}-broken.md")
    target = location.real_root.joinpath(*path.parts)
    target.parent.mkdir()
    exact = b"+++\nschema = nope\n+++\nbody\xff"
    target.write_bytes(exact)
    snapshot = repository.load_local(location, headers_only=True)
    reference = raw_reference_by_prefix(snapshot.raw_index, f"{DECISION_ID}-")
    raw = repository.read_raw(location, reference.path)
    assert raw.path == path
    assert raw.content == exact
    assert raw.size == len(exact)


def test_ambiguous_prefix_names_paths_without_reading_content(tmp_path: Path) -> None:
    repository, location = _fixture(tmp_path)
    root = location.real_root / "decisions"
    root.mkdir()
    for suffix in ("a", "b"):
        root.joinpath(f"{DECISION_ID}-{suffix}.md").write_bytes(b"secret")
    snapshot = repository.load_local(location, headers_only=True)
    with pytest.raises(AmbiguousRawPrefixError) as raised:
        raw_reference_by_prefix(snapshot.raw_index, f"{DECISION_ID}-")
    assert [value.name for value in raised.value.paths] == [
        f"{DECISION_ID}-a.md",
        f"{DECISION_ID}-b.md",
    ]
    assert "secret" not in str(raised.value)


def test_path_targeted_raw_inspect_accepts_broken_id_but_rejects_internal_symlink(
    tmp_path: Path,
) -> None:
    repository, location = _fixture(tmp_path)
    path = PurePosixPath("tasks/not-an-id.md")
    target = location.real_root.joinpath(*path.parts)
    target.parent.mkdir()
    target.write_bytes(b"broken")
    assert repository.read_raw(location, path).content == b"broken"

    link = location.real_root / "decisions" / "linked.md"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(target)
    with pytest.raises(PathSafetyError):
        repository.read_raw(location, PurePosixPath("decisions/linked.md"))
