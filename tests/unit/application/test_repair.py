from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
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
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.item_support import (
    MutationExecutionScope,
    MutationScope,
)
from untaped_orchestration.application.maintenance import (
    CheckStore,
    RenderStore,
    RepairConflict,
    RepairFrontmatterRequest,
    RepairService,
)
from untaped_orchestration.application.mutations import (
    InvalidMutationState,
    MutationExecutor,
    MutationLockSetError,
)
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    StoreLocation,
    StoreLockTimeout,
)
from untaped_orchestration.application.scaffold import AGENTS_BYTES
from untaped_orchestration.application.tasks import RepairDuplicateRequest
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.domain.ids import DecisionId, TaskId, item_filename
from untaped_orchestration.domain.limits import BODY_LIMIT, FRONTMATTER_LIMIT
from untaped_orchestration.infrastructure.external_files import FilesystemExternalFileReader
from untaped_orchestration.infrastructure.filesystem import file_revision, location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer

PATH = PurePosixPath(f"decisions/{DECISION_ID}-use-toml-front-matter-and-opaque-markdown-bodies.md")
TASK_PATH = PurePosixPath("tasks") / item_filename(
    TaskId(TASK_ID),
    "Land the public orchestration specification",
)


def _fixture(tmp_path: Path, *, external_files=None, locks=None, views=None, writer=None):
    target = tmp_path / "repository"
    target.mkdir()
    repository = FilesystemStoreRepository()
    locks = locks or FileLockManager()
    views = views or MarkdownViewRenderer()
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(target, STORE_ID, "Local", "UTC")
    )
    location = location_from_root(target / ".untaped" / "orchestration")
    item = location.real_root.joinpath(*PATH.parts)
    item.parent.mkdir()
    item.write_bytes(decision_bytes())

    def load() -> FederatedSnapshot:
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    def scope_factory() -> MutationExecutionScope:
        return MutationExecutionScope((location,), location, load)

    return (
        repository,
        location,
        RepairService(
            repository,
            MutationExecutor(
                repository,
                writer or repository,
                locks,
                views,
                projector=repository,
            ),
            MutationScope(scope_factory, scope_factory),
            external_files=external_files or FilesystemExternalFileReader(),
        ),
        item,
    )


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
    assert result.receipt.canonical_applied is False
    assert result.receipt.views_current is False
    assert result.before == original
    assert result.after.endswith(original.split(b"+++\n", 2)[2])
    assert result.after != original
    assert item.read_bytes() == original
    assert result.receipt.intended_paths == (PATH,)


def test_frontmatter_invokes_only_the_lazy_recursive_mutation_scope(tmp_path: Path) -> None:
    repository, location, _, item = _fixture(tmp_path)
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    calls: list[str] = []

    def load() -> FederatedSnapshot:
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    def recursive() -> MutationExecutionScope:
        calls.append("recursive")
        return MutationExecutionScope((location,), location, load)

    def selected_local() -> MutationExecutionScope:
        calls.append("selected-local")
        raise AssertionError("frontmatter repair must use recursive scope")

    service = RepairService(
        repository,
        MutationExecutor(repository, repository, locks, views, projector=repository),
        MutationScope(recursive, selected_local),
        external_files=FilesystemExternalFileReader(),
    )
    replacement = tmp_path / "replacement.toml"
    replacement.write_bytes(_metadata(item.read_bytes()))

    service.frontmatter(
        RepairFrontmatterRequest(location, PATH, replacement, file_revision(item.read_bytes()))
    )

    assert calls == ["recursive"]


@pytest.mark.parametrize("child_state", ["valid", "missing", "target-missing"])
def test_frontmatter_projected_validation_uses_the_resolved_federation(
    tmp_path: Path,
    child_state: str,
) -> None:
    parent = write_store(tmp_path / "parent", store_id=STORE_ID)
    child = write_store(tmp_path / "child", store_id=CHILD_STORE_ID)
    parent.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    child.joinpath("AGENTS.md").write_bytes(AGENTS_BYTES)
    parent.joinpath("registry.toml").write_text(
        f'''schema = "untaped.orchestration.registry/v1"
store_id = "{STORE_ID}"

[[children]]
id = "{CHILD_STORE_ID}"
path = "{os.path.relpath(child, parent)}"
''',
        encoding="utf-8",
    )
    if child_state == "valid":
        decision_path = PurePosixPath("decisions") / item_filename(
            DecisionId(DECISION_ID),
            "Use TOML front matter and opaque Markdown bodies",
        )
        target = child.joinpath(*decision_path.parts)
        target.parent.mkdir()
        target.write_bytes(decision_bytes())
    source = parent.joinpath(*TASK_PATH.parts)
    source.parent.mkdir()
    broken = b"not an envelope"
    source.write_bytes(broken)
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
    frontmatter = tmp_path / "replacement.toml"
    frontmatter.write_bytes(_metadata(linked))
    body = tmp_path / "body.md"
    body.write_bytes(b"repaired task body\n")
    context = CliContext.resolve(str(parent))
    service = RepairService(
        context.repository,
        context.executor,
        context.scope,
        external_files=context.repository,
    )
    request = RepairFrontmatterRequest(
        context.location,
        TASK_PATH,
        frontmatter,
        file_revision(broken),
        body_file=body,
        apply=True,
    )
    if child_state == "missing":
        child.rename(tmp_path / "missing-child")

    if child_state == "target-missing":
        with pytest.raises(InvalidMutationState) as captured:
            service.frontmatter(request)
        assert any(value.code == "ORC004" for value in captured.value.diagnostics)
        assert source.read_bytes() == broken
    else:
        result = service.frontmatter(request)
        assert result.receipt.canonical_applied
        assert result.receipt.views_current
        assert source.read_bytes() == result.after


@pytest.mark.parametrize("fault", ["participant-drift", "lock-timeout"])
def test_frontmatter_required_participant_faults_are_exact_orc007(
    tmp_path: Path,
    fault: str,
) -> None:
    repository, location, _, item = _fixture(tmp_path)
    replacement = tmp_path / "replacement.toml"
    replacement.write_bytes(_metadata(item.read_bytes()))

    class FaultLocks:
        @contextmanager
        def acquire(self, locations: Sequence, *, timeout: float) -> Iterator[None]:
            del timeout
            if fault == "lock-timeout":
                raise StoreLockTimeout(locations[-1])
            yield

    def load() -> FederatedSnapshot:
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    locked_locations = (location,)
    if fault == "participant-drift":
        extra = StoreLocation(tmp_path / "gone", tmp_path / "gone")
        locked_locations = (location, extra)

    def recursive() -> MutationExecutionScope:
        return MutationExecutionScope(locked_locations, location, load)

    service = RepairService(
        repository,
        MutationExecutor(
            repository,
            repository,
            FaultLocks(),
            MarkdownViewRenderer(),
            projector=repository,
        ),
        MutationScope(recursive, recursive),
        external_files=FilesystemExternalFileReader(),
    )

    expected = (MutationLockSetError, StoreLockTimeout)
    with pytest.raises(expected) as captured:
        service.frontmatter(
            RepairFrontmatterRequest(
                location,
                PATH,
                replacement,
                file_revision(item.read_bytes()),
                apply=True,
            )
        )

    assert {value.code for value in captured.value.diagnostics} == {"ORC007"}
    assert item.read_bytes() == decision_bytes()


def test_repair_captures_frontmatter_and_body_once_through_injected_reader(
    tmp_path: Path,
) -> None:
    class ObservingLocks:
        def __init__(self) -> None:
            self.delegate = FileLockManager()
            self.active = False

        @contextmanager
        def acquire(self, locations: Sequence, *, timeout: float) -> Iterator[None]:
            with self.delegate.acquire(locations, timeout=timeout):
                self.active = True
                try:
                    yield
                finally:
                    self.active = False

    locks = ObservingLocks()

    class ReadSpy:
        def __init__(self) -> None:
            self.delegate = FilesystemExternalFileReader()
            self.calls: list[tuple[Path, int, str]] = []

        def read_external(self, path: Path, *, limit: int, field: str) -> bytes:
            assert not locks.active, "bounded repair inputs must be captured before locks"
            self.calls.append((path, limit, field))
            return self.delegate.read_external(path, limit=limit, field=field)

    reader = ReadSpy()
    _, location, service, item = _fixture(tmp_path, external_files=reader, locks=locks)
    broken = b"not an envelope"
    item.write_bytes(broken)
    frontmatter = tmp_path / "frontmatter.toml"
    frontmatter.write_bytes(_metadata(decision_bytes()))
    body = tmp_path / "body.md"
    body.write_bytes(b"replacement body\n")

    service.frontmatter(
        RepairFrontmatterRequest(
            location,
            PATH,
            frontmatter,
            file_revision(broken),
            body_file=body,
        )
    )

    assert reader.calls == [
        (frontmatter, FRONTMATTER_LIMIT, "frontmatter"),
        (body, BODY_LIMIT, "body"),
    ]


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
    body = tmp_path / "body.md"
    body.write_bytes(b"Exact repaired body.\n")
    with pytest.raises(RepairConflict, match="revision"):
        service.frontmatter(
            RepairFrontmatterRequest(
                location,
                PATH,
                frontmatter,
                file_revision(b"stale"),
                body_file=body,
            )
        )

    result = service.frontmatter(
        RepairFrontmatterRequest(
            location, PATH, frontmatter, file_revision(broken), body_file=body, apply=True
        )
    )
    assert result.receipt.canonical_applied is True
    assert result.receipt.views_current is True
    assert item.read_bytes().endswith(b"Exact repaired body.\n")
    assert item.name == PATH.name


def test_frontmatter_view_failure_reports_canonical_success_and_render_recovers(
    tmp_path: Path,
) -> None:
    class ToggleViews(MarkdownViewRenderer):
        fail = False

        def expected(self, snapshot):
            if self.fail:
                raise ValueError("renderer interrupted")
            return super().expected(snapshot)

    views = ToggleViews()
    repository, location, service, item = _fixture(tmp_path, views=views)
    replacement = tmp_path / "replacement.toml"
    replacement.write_bytes(
        _metadata(item.read_bytes()).replace(b"tags = [", b'tags = [\n    "repaired",')
    )
    before = item.read_bytes()
    views.fail = True

    result = service.frontmatter(
        RepairFrontmatterRequest(
            location,
            PATH,
            replacement,
            file_revision(before),
            apply=True,
        )
    )

    assert result.receipt.canonical_applied is True
    assert result.receipt.views_current is False
    assert result.receipt.changed_paths == (PATH,)
    assert item.read_bytes() == result.after

    views.fail = False
    stale = CheckStore(repository, FileLockManager(), views).execute(location)
    assert not stale.views_current
    recovered = RenderStore(repository, repository, FileLockManager(), views).write(location)
    assert recovered.views_current
    assert CheckStore(repository, FileLockManager(), views).execute(location).valid


@pytest.mark.parametrize("phase", ["before", "after"])
def test_frontmatter_canonical_write_interruption_reports_false_flags_and_is_recoverable(
    tmp_path: Path,
    phase: str,
) -> None:
    repository = FilesystemStoreRepository()

    class InterruptedWriter:
        def replace(self, location, change) -> None:
            if change.path == PATH:
                if phase == "after":
                    repository.replace(location, change)
                raise OSError("canonical replacement interrupted")
            repository.replace(location, change)

        def delete(self, location, change) -> None:
            repository.delete(location, change)

    target = tmp_path / "repository"
    target.mkdir()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(target, STORE_ID, "Local", "UTC")
    )
    location = location_from_root(target / ".untaped" / "orchestration")
    item = location.real_root.joinpath(*PATH.parts)
    item.parent.mkdir()
    item.write_bytes(decision_bytes())

    def load() -> FederatedSnapshot:
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    def scope_factory() -> MutationExecutionScope:
        return MutationExecutionScope((location,), location, load)

    service = RepairService(
        repository,
        MutationExecutor(
            repository,
            InterruptedWriter(),
            locks,
            views,
            projector=repository,
        ),
        MutationScope(scope_factory, scope_factory),
        external_files=FilesystemExternalFileReader(),
    )
    before = item.read_bytes()
    replacement = tmp_path / "replacement.toml"
    replacement.write_bytes(_metadata(before).replace(b"tags = [", b'tags = [\n    "repaired",'))

    with pytest.raises(OSError, match="canonical replacement interrupted") as captured:
        service.frontmatter(
            RepairFrontmatterRequest(
                location,
                PATH,
                replacement,
                file_revision(before),
                apply=True,
            )
        )

    receipt = captured.value.receipt  # type: ignore[attr-defined]
    assert receipt.canonical_applied is False
    assert receipt.views_current is False
    assert receipt.applied is False
    if phase == "before":
        assert item.read_bytes() == before
    else:
        assert item.read_bytes() != before
        assert not CheckStore(repository, FileLockManager(), views).execute(location).views_current
        assert (
            RenderStore(repository, repository, FileLockManager(), views)
            .write(location)
            .views_current
        )


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
        MutationExecutor(
            repository,
            repository,
            FileLockManager(),
            MarkdownViewRenderer(),
            projector=repository,
        ),
        MutationScope(
            lambda: pytest.fail("duplicate repair must not resolve mutation scope"),
            lambda: pytest.fail("duplicate repair must not resolve mutation scope"),
        ),
        external_files=FilesystemExternalFileReader(),
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
