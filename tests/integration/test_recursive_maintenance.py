from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import CHILD_STORE_ID, STORE_ID, TASK_ID, task_bytes, write_store
from untaped_orchestration.application.federation import FederationService
from untaped_orchestration.application.maintenance import (
    RecursiveCheckRequest,
    RecursiveFormatRequest,
    RecursiveMaintenanceService,
)
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def _relative(parent: Path, child: Path) -> str:
    return Path(os.path.relpath(child, parent)).as_posix()


def _registry(parent: Path, child: Path) -> None:
    parent.joinpath("registry.toml").write_text(
        f'''schema = "untaped.orchestration.registry/v1"
store_id = "{STORE_ID}"

[[children]]
id = "{CHILD_STORE_ID}"
path = "{_relative(parent, child)}"
''',
        encoding="utf-8",
    )


class RecordingViews(MarkdownViewRenderer):
    def __init__(self) -> None:
        self.locations: list[Path] = []

    def expected(self, snapshot):
        self.locations.append(
            snapshot.selected.location.real_root
            if hasattr(snapshot, "selected")
            else snapshot.location.real_root
        )
        return super().expected(snapshot)


def _service(views=None):
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    return RecursiveMaintenanceService(
        FederationService(repository, locks),
        repository,
        repository,
        views or MarkdownViewRenderer(),
    )


def _render_local(root: Path) -> None:
    repository = FilesystemStoreRepository()
    location = location_from_root(root)
    snapshot = repository.load_local(location, headers_only=False)
    for path, raw in MarkdownViewRenderer().expected(snapshot).items():
        target = root.joinpath(*path.parts)
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(raw)


def _durable_files(root: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".lock"
    }


@pytest.mark.integration
def test_recursive_check_reports_all_invalid_children_but_missing_is_warning_by_default(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    _registry(parent, child)
    _render_local(parent)
    bad = child / "tasks" / f"{TASK_ID}-bad.md"
    bad.parent.mkdir()
    bad.write_bytes(b"not front matter")
    location = location_from_root(parent)

    result = _service().check(RecursiveCheckRequest(location))

    assert not result.valid
    assert any(value.path.endswith(f"{TASK_ID}-bad.md") for value in result.diagnostics)

    child.rename(tmp_path / "gone")
    missing = _service().check(RecursiveCheckRequest(location))
    assert missing.valid
    assert not missing.complete
    assert {value.severity for value in missing.diagnostics} == {"warning"}
    required = _service().check(RecursiveCheckRequest(location, require_children=True))
    assert not required.valid
    assert {value.severity for value in required.diagnostics} == {"error"}


@pytest.mark.integration
def test_recursive_check_only_evaluates_selected_views_and_never_writes_child(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    _registry(parent, child)
    _render_local(parent)
    before = _durable_files(child)
    views = RecordingViews()

    _service(views).check(RecursiveCheckRequest(location_from_root(parent)))

    assert set(views.locations) == {parent.resolve()}
    assert before == _durable_files(child)


@pytest.mark.integration
def test_recursive_fmt_check_is_read_only_and_fmt_write_requires_local(tmp_path: Path) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    _registry(parent, child)
    item = child / "tasks" / f"{TASK_ID}-task.md"
    item.parent.mkdir()
    item.write_bytes(task_bytes().replace(b"+++\n", b"+++\n# comment\n", 1))
    before = item.read_bytes()
    location = location_from_root(parent)
    service = _service()

    checked = service.fmt_check(RecursiveFormatRequest(location))

    assert not checked.matches
    assert any(
        value.path == PurePosixPath(f"tasks/{TASK_ID}-task.md") and not value.matches
        for value in checked.comparisons
    )
    assert item.read_bytes() == before
    with pytest.raises(ValueError, match="local"):
        service.fmt_write(
            RecursiveFormatRequest(location, local=False), expected_store_revision=None
        )
