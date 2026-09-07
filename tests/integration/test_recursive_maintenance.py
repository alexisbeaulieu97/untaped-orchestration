from __future__ import annotations

import json
import os
import tomllib
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import (
    CHILD_STORE_ID,
    DECISION_ID,
    STORE_ID,
    TASK_ID,
    decision_bytes,
    task_bytes,
    write_store,
)
from untaped_orchestration.__main__ import main
from untaped_orchestration.application.federation import FederationService
from untaped_orchestration.application.maintenance import (
    RecursiveCheckRequest,
    RecursiveFormatRequest,
    RecursiveMaintenanceService,
)
from untaped_orchestration.application.scaffold import AGENTS_BYTES
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.domain.models import ArchivedTask, TaskOutcome, TaskStage
from untaped_orchestration.infrastructure.codec import CanonicalStoreFormatter
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer, ViewError


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


ARCHIVED_TASK_ID = "tsk_019f0000000070008000000000000011"
MISSING_TASK_ID = "tsk_019f0000000070008000000000000012"


def _delivered_task_bytes() -> bytes:
    _, frontmatter, body = task_bytes().split(b"+++\n", 2)
    metadata = tomllib.loads(frontmatter.decode())
    metadata.pop("stage")
    metadata.update(
        {
            "id": ARCHIVED_TASK_ID,
            "closed_from": TaskStage.PLANNED.value,
            "outcome": TaskOutcome.DELIVERED.value,
            "closed_at": "2026-07-10T01:02:03.004Z",
            "close_note": "closed",
        }
    )
    return CanonicalStoreFormatter().item_bytes(ArchivedTask.model_validate(metadata), body)


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


class FailingChildViews(MarkdownViewRenderer):
    def __init__(self, child: Path) -> None:
        self.child = child.resolve()

    def expected(self, snapshot):
        if snapshot.location.real_root == self.child:
            raise ValueError("child renderer failed")
        return super().expected(snapshot)


class TypedFailingLocationViews(MarkdownViewRenderer):
    def __init__(self, location: Path, error: Exception) -> None:
        self.location = location.resolve()
        self.error = error

    def expected(self, snapshot):
        if snapshot.location.real_root == self.location:
            raise self.error
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


def _cross_store_navigation_fixture(tmp_path: Path) -> tuple[Path, Path]:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    parent.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    child.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    _registry(parent, child)
    archive = parent / "archive" / "tasks" / f"{ARCHIVED_TASK_ID}-delivered.md"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(_delivered_task_bytes())
    decision = child / "decisions" / f"{DECISION_ID}-ruling.md"
    decision.parent.mkdir()
    decision.write_bytes(decision_bytes())
    task = parent / "tasks" / f"{TASK_ID}-task.md"
    task.parent.mkdir()
    task.write_bytes(
        task_bytes()
        .replace(b'stage = "inbox"\n', b'stage = "planned"\n')
        .replace(
            b"waiting_on = []\n+++",
            (
                "waiting_on = []\n\n"
                "[[links]]\n"
                'relation = "governed-by"\n'
                f'target_store_id = "{CHILD_STORE_ID}"\n'
                f'target = "{DECISION_ID}"\n'
                "+++"
            ).encode(),
        )
    )
    _render_local(parent)
    _render_local(child)
    return parent, child


@pytest.mark.integration
def test_recursive_check_reports_all_invalid_children_but_missing_is_warning_by_default(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    parent.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    child.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    _registry(parent, child)
    _render_local(parent)
    bad = child / "tasks" / f"{TASK_ID}-bad.md"
    bad.parent.mkdir()
    bad.write_bytes(b"not front matter")
    location = location_from_root(parent)

    result = _service().check(RecursiveCheckRequest(location))

    assert not result.valid
    assert any(value.path.endswith(f"{TASK_ID}-bad.md") for value in result.diagnostics)
    child_check = next(value for value in result.checks if value.store_id == CHILD_STORE_ID)
    assert not child_check.valid
    assert any(value.path.endswith(f"{TASK_ID}-bad.md") for value in child_check.diagnostics)

    child.rename(tmp_path / "gone")
    missing = _service().check(RecursiveCheckRequest(location))
    assert missing.valid
    assert not missing.complete
    assert {value.severity for value in missing.diagnostics} == {"warning"}
    required = _service().check(RecursiveCheckRequest(location, require_children=True))
    assert not required.valid
    assert {value.severity for value in required.diagnostics} == {"error"}
    selected_required = next(value for value in required.checks if value.store_id == STORE_ID)
    assert not selected_required.valid
    assert any(value.code == "ORC005" for value in selected_required.diagnostics)


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

    assert set(views.locations) == {parent.resolve(), child.resolve()}
    assert before == _durable_files(child)


@pytest.mark.integration
@pytest.mark.parametrize("failing_store", ("parent", "child"))
def test_recursive_check_preserves_typed_view_diagnostic_from_each_comparison_path(
    tmp_path: Path,
    failing_store: str,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    _registry(parent, child)
    _render_local(parent)
    _render_local(child)
    error = ViewError("typed view failure")
    target = parent if failing_store == "parent" else child

    with pytest.raises(ViewError) as captured:
        _service(TypedFailingLocationViews(target, error)).check(
            RecursiveCheckRequest(location_from_root(parent))
        )

    assert captured.value is error


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


@pytest.mark.integration
def test_local_fmt_and_render_validate_resolved_cross_store_navigation(tmp_path: Path) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    parent.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    child.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    _registry(parent, child)
    decision = child / "decisions" / f"{DECISION_ID}-ruling.md"
    decision.parent.mkdir()
    decision.write_bytes(decision_bytes())
    task = parent / "tasks" / f"{TASK_ID}-task.md"
    task.parent.mkdir()
    linked = task_bytes().replace(
        b"waiting_on = []\n+++",
        (
            "waiting_on = []\n\n"
            "[[links]]\n"
            'relation = "governed-by"\n'
            f'target_store_id = "{CHILD_STORE_ID}"\n'
            f'target = "{DECISION_ID}"\n'
            "+++"
        ).encode(),
    )
    task.write_bytes(linked.replace(b"+++\n", b"+++\n# hand edit\n", 1))
    location = location_from_root(parent)
    context = CliContext.resolve(str(parent))

    checked = context.maintenance().fmt_check(RecursiveFormatRequest(location, local=True))
    assert not any(value.code == "ORC004" for value in checked.diagnostics)
    revision = context.repository.load_local(location, headers_only=True).store_revision
    written = context.maintenance().fmt_write(
        RecursiveFormatRequest(location, local=True),
        expected_store_revision=revision,
    )
    assert written.matches

    rendered = context.maintenance().render_write(location)
    assert rendered.views_current
    assert context.maintenance().render_check(location).matches

    child.rename(tmp_path / "missing-child")
    task.write_bytes(task.read_bytes().replace(b"+++\n", b"+++\n# second hand edit\n", 1))
    missing_context = CliContext.resolve(str(parent))
    unresolved = missing_context.maintenance().fmt_check(
        RecursiveFormatRequest(location, local=True)
    )
    assert not any(value.code == "ORC004" for value in unresolved.diagnostics)
    assert any(value.code == "ORC005" for value in unresolved.diagnostics)
    revision = missing_context.repository.load_local(location, headers_only=True).store_revision
    repaired = missing_context.maintenance().fmt_write(
        RecursiveFormatRequest(location, local=True),
        expected_store_revision=revision,
    )
    assert repaired.matches
    assert missing_context.maintenance().render_write(location).views_current


@pytest.mark.integration
def test_recursive_check_attributes_valid_cross_store_navigation_to_source_row(
    tmp_path: Path,
) -> None:
    parent, _ = _cross_store_navigation_fixture(tmp_path)
    location = location_from_root(parent)

    result = _service().check(RecursiveCheckRequest(location))

    assert result.valid
    selected = next(value for value in result.checks if value.store_id == STORE_ID)
    assert selected.valid
    assert not any(value.code == "ORC004" for value in selected.diagnostics)


@pytest.mark.integration
def test_recursive_check_owns_overlapping_symlink_child_diagnostic_only_in_child_row(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    parent.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    child.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    linked_child = parent / "linked-child"
    linked_child.symlink_to(child, target_is_directory=True)
    _registry(parent, linked_child)
    task = child / "tasks" / f"{TASK_ID}-bad.md"
    task.parent.mkdir()
    task.write_bytes(
        task_bytes().replace(
            b"waiting_on = []\n+++",
            (
                "waiting_on = []\n\n"
                "[[links]]\n"
                'relation = "depends-on"\n'
                f'target_store_id = "{CHILD_STORE_ID}"\n'
                f'target = "{MISSING_TASK_ID}"\n'
                "+++"
            ).encode(),
        )
    )
    _render_local(parent)
    _render_local(child)

    result = _service().check(RecursiveCheckRequest(location_from_root(parent)))

    assert not result.valid
    parent_check = next(value for value in result.checks if value.store_id == STORE_ID)
    child_check = next(value for value in result.checks if value.store_id == CHILD_STORE_ID)
    assert parent_check.valid
    assert not any(value.code == "ORC004" for value in parent_check.diagnostics)
    assert not child_check.valid
    assert any(value.code == "ORC004" for value in child_check.diagnostics)


@pytest.mark.integration
def test_local_check_uses_selected_local_navigation_projection_with_delivered_archive(
    tmp_path: Path,
) -> None:
    parent, _ = _cross_store_navigation_fixture(tmp_path)
    location = location_from_root(parent)
    context = CliContext.resolve(str(parent))

    direct = context.check_store().execute(location)
    assert direct.valid
    assert any(
        value.code == "ORC005" and value.severity == "warning" for value in direct.diagnostics
    )
    assert not any(value.code == "ORC004" for value in direct.diagnostics)

    result = context.maintenance().check(RecursiveCheckRequest(location, local=True))

    assert result.valid
    assert result.complete
    assert any(
        value.code == "ORC005" and value.severity == "warning" for value in result.diagnostics
    )
    assert not any(value.code == "ORC004" for value in result.diagnostics)
    assert not any(
        value.code == "ORC006" and "delivered closure requires complete federation" in value.message
        for value in result.diagnostics
    )


@pytest.mark.integration
def test_local_fmt_and_render_ignore_navigation_incompleteness_for_delivered_archive(
    tmp_path: Path,
) -> None:
    parent, _ = _cross_store_navigation_fixture(tmp_path)
    location = location_from_root(parent)
    context = CliContext.resolve(str(parent))

    formatted = context.maintenance().fmt_check(RecursiveFormatRequest(location, local=True))
    rendered = context.maintenance().render_check(location)

    assert formatted.matches
    assert rendered.matches
    assert any(
        value.code == "ORC005" and value.severity == "warning" for value in formatted.diagnostics
    )
    assert any(
        value.code == "ORC005" and value.severity == "warning" for value in rendered.diagnostics
    )
    assert not any(value.code == "ORC006" for value in formatted.diagnostics)
    assert not any(value.code == "ORC006" for value in rendered.diagnostics)


@pytest.mark.integration
def test_cli_local_check_fmt_and_render_accept_cross_store_navigation_with_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent, _ = _cross_store_navigation_fixture(tmp_path)

    def invoke(*tokens: str) -> tuple[int, dict[str, object]]:
        monkeypatch.setattr(
            "sys.argv",
            ["untaped-orchestration", *tokens, "--store", str(parent), "--format", "json"],
        )
        with pytest.raises(SystemExit) as raised:
            main()
        return raised.value.code, json.loads(capsys.readouterr().out)

    check_code, check = invoke("check", "--local")
    fmt_code, fmt = invoke("fmt", "--check", "--local")
    render_code, render = invoke("render", "--check")

    assert check_code == 0
    assert fmt_code == 0
    assert render_code == 0
    for payload in (check, fmt):
        diagnostics = payload["diagnostics"]
        assert any(value["code"] == "ORC005" for value in diagnostics)
        assert not any(
            value["code"] == "ORC004" or value["code"] == "ORC006" for value in diagnostics
        )
    assert any(value["code"] == "ORC005" for value in render["diagnostics"])
    assert not any(
        value["code"] == "ORC004" or value["code"] == "ORC006" for value in render["diagnostics"]
    )


@pytest.mark.integration
def test_local_maintenance_never_loads_or_locks_registered_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, child = _cross_store_navigation_fixture(tmp_path)
    location = location_from_root(parent)
    loaded_roots: list[Path] = []
    locked_sets: list[tuple[Path, ...]] = []
    original_load = FilesystemStoreRepository.load_local
    original_acquire = FileLockManager.acquire

    def tracked_load(self, store_location, *, headers_only):
        loaded_roots.append(store_location.real_root)
        return original_load(self, store_location, headers_only=headers_only)

    @contextmanager
    def tracked_acquire(self, locations, *, timeout):
        locked_sets.append(tuple(value.real_root for value in locations))
        with original_acquire(self, locations, timeout=timeout):
            yield

    monkeypatch.setattr(FilesystemStoreRepository, "load_local", tracked_load)
    monkeypatch.setattr(FileLockManager, "acquire", tracked_acquire)
    context = CliContext.resolve(str(parent))
    store_location = location
    revision = context.repository.load_local(store_location, headers_only=True).store_revision

    context.maintenance().check(RecursiveCheckRequest(store_location, local=True))
    context.maintenance().fmt_check(RecursiveFormatRequest(store_location, local=True))
    context.maintenance().fmt_write(
        RecursiveFormatRequest(store_location, local=True), expected_store_revision=revision
    )
    context.maintenance().render_check(store_location)
    context.maintenance().render_write(store_location)

    assert loaded_roots
    assert set(loaded_roots) == {parent.resolve()}
    assert locked_sets
    assert all(lock_set == (parent.resolve(),) for lock_set in locked_sets)
    assert child.resolve() not in loaded_roots


@pytest.mark.integration
def test_recursive_fmt_qualifies_identical_paths_and_invalid_data_never_matches(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    _registry(parent, child)
    relative = PurePosixPath(f"tasks/{TASK_ID}-same.md")
    for root in (parent, child):
        target = root.joinpath(*relative.parts)
        target.parent.mkdir()
        target.write_bytes(task_bytes())
    result = _service().fmt_check(RecursiveFormatRequest(location_from_root(parent)))
    attributed = [value.store_id for value in result.comparisons if value.path == relative]
    assert attributed == [CHILD_STORE_ID, STORE_ID]

    child.joinpath(*relative.parts).write_bytes(b"not front matter")
    invalid = _service().fmt_check(RecursiveFormatRequest(location_from_root(parent)))
    assert not invalid.matches
    assert any(value.code == "ORC001" for value in invalid.diagnostics)


@pytest.mark.integration
def test_recursive_check_reads_child_view_revision_without_rendering_child(tmp_path: Path) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    _registry(parent, child)
    _render_local(parent)
    _render_local(child)
    child.joinpath("registry.toml").write_bytes(
        child.joinpath("registry.toml").read_bytes() + b"\n"
    )
    views = RecordingViews()

    result = _service(views).check(RecursiveCheckRequest(location_from_root(parent)))

    child_check = next(value for value in result.checks if value.store_id == CHILD_STORE_ID)
    assert not child_check.views_current
    assert child.resolve() in views.locations


@pytest.mark.integration
def test_recursive_check_compares_every_child_view_byte_for_byte_without_writing(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    _registry(parent, child)
    _render_local(parent)
    _render_local(child)
    exact = _durable_files(child)

    current = _service().check(RecursiveCheckRequest(location_from_root(parent)))
    child_current = next(value for value in current.checks if value.store_id == CHILD_STORE_ID)
    assert child_current.views_current
    assert exact == _durable_files(child)

    path = child / "views" / "roadmap.md"
    marker = f"Store revision: `{child_current.store_revision.root}`".encode()
    path.write_bytes(b"garbage\n" + marker + b"\n")
    before = _durable_files(child)

    stale = _service().check(RecursiveCheckRequest(location_from_root(parent)))

    child_stale = next(value for value in stale.checks if value.store_id == CHILD_STORE_ID)
    assert not child_stale.views_current
    assert any(
        value.code == "ORC008" and value.path == (child / "views").as_posix()
        for value in child_stale.diagnostics
    )
    assert before == _durable_files(child)


@pytest.mark.integration
def test_recursive_check_uses_decision_only_child_applicability_and_reports_renderer_failure(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    _registry(parent, child)
    repository = FilesystemStoreRepository()
    child_location = location_from_root(child)
    snapshot = repository.load_local(child_location, headers_only=False)
    assert snapshot.store is not None
    decision_only = snapshot.store.model_copy(
        update={
            "capabilities": snapshot.store.capabilities.model_copy(update={"active_tasks": False})
        }
    )
    child.joinpath("store.toml").write_bytes(repository.store_bytes(decision_only))
    _render_local(parent)
    _render_local(child)
    assert tuple(path.name for path in child.joinpath("views").iterdir()) == ("decisions.md",)

    applicable = _service().check(RecursiveCheckRequest(location_from_root(parent)))
    child_check = next(value for value in applicable.checks if value.store_id == CHILD_STORE_ID)
    assert child_check.views_current

    before = _durable_files(child)
    failed = _service(FailingChildViews(child)).check(
        RecursiveCheckRequest(location_from_root(parent))
    )
    failed_child = next(value for value in failed.checks if value.store_id == CHILD_STORE_ID)
    assert not failed_child.views_current
    assert any(
        value.code == "ORC008" and value.path == (child / "views").as_posix()
        for value in failed_child.diagnostics
    )
    assert before == _durable_files(child)
