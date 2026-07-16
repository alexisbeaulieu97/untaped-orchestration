from __future__ import annotations

from pathlib import Path

from tests.builders import STORE_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def initialized_repository(tmp_path: Path) -> Path:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    repository = FilesystemStoreRepository()
    InitializeStore(
        repository,
        repository,
        FileLockManager(),
        MarkdownViewRenderer(),
    ).execute(InitRequest(repository_root, STORE_ID, "CLI fixture", "UTC"))
    return location_from_root(repository_root / ".untaped" / "orchestration").root
