from __future__ import annotations

from pathlib import Path

from tests.builders import STORE_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.maintenance import CheckStore, FormatStore, RenderStore
from untaped_orchestration.infrastructure.codec import CanonicalStoreFormatter
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def test_local_store_init_check_format_and_render_recovery(tmp_path: Path) -> None:
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    initialized = InitializeStore(repository, repository, locks, views).execute(
        InitRequest(repository_path, STORE_ID, "Local", "America/Montreal")
    )
    root = repository_path / ".untaped" / "orchestration"
    location = location_from_root(root)

    assert initialized.views_current
    assert CheckStore(repository, locks, views).execute(location).valid

    root.joinpath("store.toml").write_bytes(
        b"# hand-edited comment\n" + root.joinpath("store.toml").read_bytes()
    )
    format_service = FormatStore(repository, repository, locks, views, CanonicalStoreFormatter())
    assert not format_service.check(location).matches
    revision = repository.load_local(location, headers_only=True).store_revision
    formatted = format_service.write(location, expected_store_revision=revision)
    assert formatted.canonical_applied
    assert formatted.views_current

    root.joinpath("views/decisions.md").unlink()
    assert not RenderStore(repository, repository, locks, views).check(location).matches
    rendered = RenderStore(repository, repository, locks, views).write(location)
    assert rendered.views_current
    assert CheckStore(repository, locks, views).execute(location).valid
