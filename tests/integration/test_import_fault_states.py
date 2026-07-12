from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from tests.unit.application.test_import import _fixture, _manifest
from untaped_orchestration.application.maintenance import ImportConflict, ImportRequest
from untaped_orchestration.application.ports import FileReplacement


def test_exact_subset_resume_reconstructs_original_revision_and_refuses_divergence(
    tmp_path: Path,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    preview = service.execute(ImportRequest(location, manifest))
    record = preview.records[0]
    target = location.real_root.joinpath(*record.path.parts)
    target.parent.mkdir()
    target.write_bytes(record.content)

    resumed = service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))
    assert resumed.records[0].already_present is True
    assert resumed.base_revision == base
    assert resumed.receipt.canonical_applied is False
    assert resumed.receipt.views_current is True

    target.write_bytes(record.content + b"divergent")
    with pytest.raises(ImportConflict, match="divergent"):
        service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))


def test_unexpected_item_and_acknowledgement_loss_are_fail_closed(tmp_path: Path) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    unexpected = location.real_root / "decisions" / "dec_019f0000000070008000000000000002-extra.md"
    unexpected.parent.mkdir()
    unexpected.write_bytes(b"unexpected")
    with pytest.raises(ImportConflict):
        service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))

    unexpected.unlink()
    orphan = location.real_root / "decisions" / ".item.md.untaped-tmp-orphan"
    orphan.write_bytes(b"orphan")
    with pytest.raises(ValueError):
        service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))


def test_manifest_paths_cannot_escape_or_alias_destination(tmp_path: Path) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    manifest.write_text(
        manifest.read_text().replace("records/decision.md", "../outside.md"),
        encoding="utf-8",
    )
    with pytest.raises(ImportConflict, match="manifest"):
        service.execute(ImportRequest(location, manifest))


def test_manifest_record_symlinks_and_changed_base_retry_are_refused(tmp_path: Path) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    body = tmp_path / "records" / "decision.md"
    actual = tmp_path / "body-source.md"
    actual.write_bytes(body.read_bytes())
    body.unlink()
    body.symlink_to(actual)
    with pytest.raises(ImportConflict, match="manifest"):
        service.execute(ImportRequest(location, manifest))

    body.unlink()
    body.write_bytes(actual.read_bytes())
    preview = service.execute(ImportRequest(location, manifest))
    target = location.real_root.joinpath(*preview.records[0].path.parts)
    target.parent.mkdir()
    target.write_bytes(preview.records[0].content)
    snapshot = repository.load_local(location, headers_only=False)
    assert snapshot.store is not None
    repository.replace(
        location,
        FileReplacement(
            PurePosixPath("store.toml"),
            repository.store_bytes(snapshot.store.model_copy(update={"name": "Changed"})),
        ),
    )
    with pytest.raises(ImportConflict, match="revision"):
        service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))


def test_completed_subset_retry_repairs_lost_views_without_canonical_rewrite(
    tmp_path: Path,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    first = service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))
    assert first.receipt.views_current
    view = location.real_root / "views" / "decisions.md"
    view.write_bytes(b"stale")

    replay = service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))

    assert replay.receipt.canonical_applied is False
    assert replay.receipt.views_current is True
    assert PurePosixPath("views/decisions.md") in replay.receipt.changed_paths


def test_first_clean_apply_requires_current_views(tmp_path: Path) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    (location.real_root / "views" / "decisions.md").write_bytes(b"stale")

    with pytest.raises(ImportConflict, match="current views"):
        service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))
