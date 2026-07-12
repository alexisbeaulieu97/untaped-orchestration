from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from tests.builders import DECISION_ID, STORE_ID, decision_bytes
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.maintenance import (
    RepairConflict,
    RepairFrontmatterRequest,
    RepairService,
)
from untaped_orchestration.application.tasks import RepairDuplicateRequest
from untaped_orchestration.domain.ids import TaskId
from untaped_orchestration.infrastructure.filesystem import file_revision, location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer

PATH = PurePosixPath(f"decisions/{DECISION_ID}-use-toml-front-matter-and-opaque-markdown-bodies.md")


def _fixture(tmp_path: Path):
    target = tmp_path / "repository"
    target.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(target, STORE_ID, "Local", "UTC")
    )
    location = location_from_root(target / ".untaped" / "orchestration")
    item = location.real_root.joinpath(*PATH.parts)
    item.parent.mkdir()
    item.write_bytes(decision_bytes())
    return repository, location, RepairService(repository, repository, locks, views), item


def _metadata(raw: bytes) -> bytes:
    return raw.split(b"+++\n", 2)[1]


def test_frontmatter_dry_run_preserves_proven_body_and_never_renames(tmp_path: Path) -> None:
    _, location, service, item = _fixture(tmp_path)
    original = item.read_bytes()
    replacement = tmp_path / "replacement.toml"
    replacement.write_bytes(_metadata(original).replace(b"tags = [", b'tags = [\n    "repaired",'))

    result = service.frontmatter(
        RepairFrontmatterRequest(location, PATH, replacement, file_revision(original))
    )

    assert result.receipt.applied is False
    assert result.before == original
    assert result.after.endswith(original.split(b"+++\n", 2)[2])
    assert result.after != original
    assert item.read_bytes() == original
    assert result.receipt.intended_paths == (PATH,)


def test_unprovable_boundary_requires_explicit_valid_body_and_exact_guard(
    tmp_path: Path,
) -> None:
    _, location, service, item = _fixture(tmp_path)
    broken = b"not an envelope\xff"
    item.write_bytes(broken)
    frontmatter = tmp_path / "replacement.toml"
    frontmatter.write_bytes(_metadata(decision_bytes()))

    with pytest.raises(RepairConflict, match="body-file"):
        service.frontmatter(
            RepairFrontmatterRequest(location, PATH, frontmatter, file_revision(broken))
        )
    with pytest.raises(RepairConflict, match="revision"):
        service.frontmatter(
            RepairFrontmatterRequest(
                location,
                PATH,
                frontmatter,
                file_revision(b"stale"),
                body_file=tmp_path / "body.md",
            )
        )

    body = tmp_path / "body.md"
    body.write_bytes(b"Exact repaired body.\n")
    result = service.frontmatter(
        RepairFrontmatterRequest(
            location, PATH, frontmatter, file_revision(broken), body_file=body, apply=True
        )
    )
    assert result.receipt.canonical_applied is True
    assert item.read_bytes().endswith(b"Exact repaired body.\n")
    assert item.name == PATH.name


@pytest.mark.parametrize("body", [b"invalid\xff", b"x" * (1024 * 1024 + 1)])
def test_explicit_body_must_be_valid_utf8_and_within_codec_bounds(
    tmp_path: Path, body: bytes
) -> None:
    _, location, service, item = _fixture(tmp_path)
    broken = b"not an envelope"
    item.write_bytes(broken)
    frontmatter = tmp_path / "replacement.toml"
    frontmatter.write_bytes(_metadata(decision_bytes()))
    body_file = tmp_path / "body.md"
    body_file.write_bytes(body)
    with pytest.raises(RepairConflict):
        service.frontmatter(
            RepairFrontmatterRequest(
                location,
                PATH,
                frontmatter,
                file_revision(broken),
                body_file=body_file,
            )
        )


def test_frontmatter_and_body_inputs_must_not_be_symlinks(tmp_path: Path) -> None:
    _, location, service, item = _fixture(tmp_path)
    metadata = tmp_path / "metadata-source.toml"
    metadata.write_bytes(_metadata(decision_bytes()))
    link = tmp_path / "replacement.toml"
    link.symlink_to(metadata)
    with pytest.raises(RepairConflict):
        service.frontmatter(
            RepairFrontmatterRequest(location, PATH, link, file_revision(item.read_bytes()))
        )

    link.unlink()
    link.write_bytes(metadata.read_bytes())
    broken = b"not an envelope"
    item.write_bytes(broken)
    body_source = tmp_path / "body-source.md"
    body_source.write_bytes(b"body\n")
    body_link = tmp_path / "body.md"
    body_link.symlink_to(body_source)
    with pytest.raises(RepairConflict):
        service.frontmatter(
            RepairFrontmatterRequest(
                location,
                PATH,
                link,
                file_revision(broken),
                body_file=body_link,
            )
        )


def test_duplicate_repair_facade_delegates_exact_guarded_request(tmp_path: Path) -> None:
    repository, _, _, _ = _fixture(tmp_path)
    sentinel = object()

    class DuplicateRepair:
        received: RepairDuplicateRequest | None = None

        def repair_duplicate(self, request: RepairDuplicateRequest) -> object:
            self.received = request
            return sentinel

    delegate = DuplicateRepair()
    service = RepairService(
        repository,
        repository,
        FileLockManager(),
        MarkdownViewRenderer(),
        duplicate_repair=delegate,
    )
    request = RepairDuplicateRequest(
        TaskId("tsk_019f0000000070008000000000000010"),
        file_revision(b"active"),
        file_revision(b"archive"),
    )
    assert service.duplicate(request) is sentinel
    assert delegate.received is request


@pytest.mark.parametrize("external", ["frontmatter", "body"])
def test_repair_rejects_inputs_under_a_symlinked_lexical_root(
    tmp_path: Path, external: str
) -> None:
    _, location, service, item = _fixture(tmp_path)
    real = tmp_path / "real-input"
    real.mkdir()
    (real / "frontmatter.toml").write_bytes(_metadata(decision_bytes()))
    (real / "body.md").write_bytes(b"explicit body\n")
    linked = tmp_path / "linked-input"
    linked.symlink_to(real, target_is_directory=True)
    broken = b"not an envelope"
    if external == "body":
        item.write_bytes(broken)

    request = RepairFrontmatterRequest(
        location,
        PATH,
        linked / "frontmatter.toml" if external == "frontmatter" else real / "frontmatter.toml",
        file_revision(broken if external == "body" else item.read_bytes()),
        body_file=linked / "body.md" if external == "body" else None,
    )
    with pytest.raises(RepairConflict):
        service.frontmatter(request)
