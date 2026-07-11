from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import STORE_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.mutations import (
    IntendedMutation,
    InvalidMutationState,
    MutationExecutor,
    MutationLockSetError,
    validate_selected_local,
)
from untaped_orchestration.application.ports import FileDeletion, FileReplacement
from untaped_orchestration.application.results import Completeness, FederatedSnapshot
from untaped_orchestration.application.view_management import apply_views
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


class RecordingLocks:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = False

    @contextmanager
    def acquire(self, locations: Sequence, *, timeout: float) -> Iterator[None]:
        assert timeout == 10.0
        self.events.append(f"lock:{len(locations)}")
        self.active = True
        try:
            yield
        finally:
            self.active = False
            self.events.append("unlock")


class RecordingRepository:
    def __init__(
        self,
        delegate: FilesystemStoreRepository,
        events: list[str],
        locks: RecordingLocks,
    ) -> None:
        self.delegate = delegate
        self.events = events
        self.locks = locks
        self.writes: list[PurePosixPath] = []

    def load_local(self, location, *, headers_only: bool):
        assert self.locks.active
        self.events.append("reload")
        return self.delegate.load_local(location, headers_only=headers_only)

    def read_file(self, location, path):
        return self.delegate.read_file(location, path)

    def list_entries(self, location):
        return self.delegate.list_entries(location)

    def replace(self, location, change) -> None:
        assert self.locks.active
        self.events.append(f"write:{change.path.as_posix()}")
        self.writes.append(change.path)
        self.delegate.replace(location, change)

    def delete(self, location, change) -> None:
        assert self.locks.active
        self.events.append(f"delete:{change.path.as_posix()}")
        self.delegate.delete(location, change)


class RecordingProjector:
    def __init__(
        self,
        delegate: FilesystemStoreRepository,
        events: list[str],
        locks: RecordingLocks,
    ) -> None:
        self.delegate = delegate
        self.events = events
        self.locks = locks

    def project(self, current, selected, replacements, deletions):
        assert self.locks.active
        self.events.append("project")
        return self.delegate.project(current, selected, replacements, deletions)


class StaleReloadRepository(RecordingRepository):
    def __init__(self, delegate, events, locks, stale) -> None:
        super().__init__(delegate, events, locks)
        self.stale = stale

    def load_local(self, location, *, headers_only: bool):
        del location, headers_only
        assert self.locks.active
        self.events.append("reload")
        return self.stale


class RecordingViews:
    def __init__(self, events: list[str], locks: RecordingLocks, *, fail: bool = False) -> None:
        self.events = events
        self.locks = locks
        self.fail = fail

    def managed_paths(self) -> tuple[PurePosixPath, ...]:
        return (PurePosixPath("views/decisions.md"),)

    def expected(self, snapshot) -> Mapping[PurePosixPath, bytes]:
        assert self.locks.active
        self.events.append("render")
        if self.fail:
            raise OSError("renderer unavailable")
        return {PurePosixPath("views/decisions.md"): b"view\n"}


def _state(tmp_path: Path):
    repository = FilesystemStoreRepository()
    views = MarkdownViewRenderer()
    locks = FileLockManager()
    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    first_target.mkdir()
    second_target.mkdir()
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(first_target, STORE_ID, "First", "UTC")
    )
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(
            second_target,
            "sto_019f0000000070008000000000000001",
            "Second",
            "UTC",
        )
    )
    first_root = first_target / ".untaped" / "orchestration"
    second_root = second_target / ".untaped" / "orchestration"
    first = repository.load_local(location_from_root(first_root), headers_only=False)
    second = repository.load_local(location_from_root(second_root), headers_only=False)
    return repository, FederatedSnapshot(first, (first, second), Completeness())


def _replacement(current: FederatedSnapshot) -> FileReplacement:
    assert current.selected.registry is not None
    raw = (
        b'schema = "untaped.orchestration.registry/v1"\n'
        + f'store_id = "{current.selected.registry.store_id.root}"\n'.encode()
        + b"\n[[children]]\n"
        + b'id = "sto_019f0000000070008000000000000001"\n'
        + b'path = "../../second/.untaped/orchestration"\n'
    )
    return FileReplacement(PurePosixPath("registry.toml"), raw)


def test_shared_finalizer_projects_bytes_and_enforces_exact_phase_order(
    tmp_path: Path,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = RecordingRepository(repository, events, locks)
    projector = RecordingProjector(repository, events, locks)
    views = RecordingViews(events, locks)

    validations = iter(("validate-current", "validate-intended", "validate-after"))

    def validate(snapshot: FederatedSnapshot):
        del snapshot
        events.append(next(validations))
        return ()

    def load() -> FederatedSnapshot:
        events.append("load")
        return current

    result = MutationExecutor(
        adapter,
        adapter,
        locks,
        views,
        projector=projector,
        validator=validate,
    ).execute(
        locations=tuple(value.location for value in reversed(current.stores)),
        selected=current.selected.location,
        load=load,
        guard=lambda _: events.append("guard"),
        build=lambda snapshot: (
            events.append("build") or IntendedMutation(replacements=(_replacement(snapshot),))
        ),
    )

    assert events == [
        "lock:2",
        "load",
        "validate-current",
        "guard",
        "build",
        "project",
        "validate-intended",
        "write:registry.toml",
        "reload",
        "validate-after",
        "render",
        "write:views/decisions.md",
        "unlock",
    ]
    assert result.canonical_applied
    assert result.views_current
    assert result.intended_paths == (
        PurePosixPath("registry.toml"),
        PurePosixPath("views/decisions.md"),
    )


def test_finalizer_accepts_an_explicit_per_operation_validator(tmp_path: Path) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = RecordingRepository(repository, events, locks)
    projector = RecordingProjector(repository, events, locks)
    default_calls = 0
    operation_calls = 0

    def default_validator(snapshot: FederatedSnapshot):
        nonlocal default_calls
        del snapshot
        default_calls += 1
        return ()

    def operation_validator(snapshot: FederatedSnapshot):
        nonlocal operation_calls
        del snapshot
        operation_calls += 1
        return ()

    MutationExecutor(
        adapter,
        adapter,
        locks,
        RecordingViews(events, locks),
        projector=projector,
        validator=default_validator,
    ).execute(
        locations=tuple(value.location for value in current.stores),
        selected=current.selected.location,
        load=lambda: current,
        guard=lambda _: None,
        build=lambda _: IntendedMutation(),
        validator=operation_validator,
    )

    assert default_calls == 0
    assert operation_calls == 3
    assert callable(validate_selected_local)


@pytest.mark.parametrize("mode", ["missing", "extra", "wrong-selected"])
def test_finalizer_rejects_any_lock_set_or_selected_location_mismatch_before_build(
    tmp_path: Path,
    mode: str,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = RecordingRepository(repository, events, locks)
    projector = RecordingProjector(repository, events, locks)
    locations = [value.location for value in current.stores]
    selected = current.selected.location
    if mode == "missing":
        locations.pop()
    elif mode == "extra":
        extra_target = tmp_path / "extra"
        extra_target.mkdir()
        InitializeStore(repository, repository, FileLockManager(), MarkdownViewRenderer()).execute(
            InitRequest(
                extra_target,
                "sto_019f0000000070008000000000000002",
                "Extra",
                "UTC",
            )
        )
        extra_root = extra_target / ".untaped" / "orchestration"
        locations.append(location_from_root(extra_root))
    else:
        selected = current.stores[1].location

    with pytest.raises(MutationLockSetError):
        MutationExecutor(
            adapter, adapter, locks, RecordingViews(events, locks), projector=projector
        ).execute(
            locations=locations,
            selected=selected,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda _: pytest.fail("build must not run"),
        )

    assert adapter.writes == []


def test_invalid_replacement_bytes_cannot_hide_behind_a_fabricated_snapshot(
    tmp_path: Path,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = RecordingRepository(repository, events, locks)
    projector = RecordingProjector(repository, events, locks)
    invalid = FileReplacement(PurePosixPath("registry.toml"), b"not = [valid\n")

    with pytest.raises(InvalidMutationState) as captured:
        MutationExecutor(
            adapter, adapter, locks, RecordingViews(events, locks), projector=projector
        ).execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda _: IntendedMutation(replacements=(invalid,)),
        )

    assert captured.value.diagnostics
    assert adapter.writes == []


@pytest.mark.parametrize(
    "change",
    [
        IntendedMutation(deletions=(FileDeletion(PurePosixPath("registry.toml")),)),
        IntendedMutation(deletions=(FileDeletion(PurePosixPath("AGENTS.md")),)),
        IntendedMutation(deletions=(FileDeletion(PurePosixPath("CLAUDE.md")),)),
        IntendedMutation(replacements=(FileReplacement(PurePosixPath("AGENTS.md"), b"changed\n"),)),
        IntendedMutation(
            replacements=(FileReplacement(PurePosixPath("CLAUDE.md"), b"@OTHER.md\n"),)
        ),
        IntendedMutation(
            replacements=(FileReplacement(PurePosixPath("unexpected.txt"), b"unsafe\n"),)
        ),
    ],
)
def test_projected_shape_rejects_required_deletions_instruction_changes_and_unsafe_paths(
    tmp_path: Path,
    change: IntendedMutation,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = RecordingRepository(repository, events, locks)
    projector = RecordingProjector(repository, events, locks)

    with pytest.raises(InvalidMutationState) as captured:
        MutationExecutor(
            adapter,
            adapter,
            locks,
            RecordingViews(events, locks),
            projector=projector,
        ).execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda _: change,
        )

    assert captured.value.diagnostics
    assert adapter.writes == []


def test_renderer_failure_preserves_complete_intended_view_paths(
    tmp_path: Path,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = RecordingRepository(repository, events, locks)
    projector = RecordingProjector(repository, events, locks)
    views = RecordingViews(events, locks, fail=True)

    result = MutationExecutor(
        adapter,
        adapter,
        locks,
        views,
        projector=projector,
        validator=lambda _: (),
    ).execute(
        locations=tuple(value.location for value in current.stores),
        selected=current.selected.location,
        load=lambda: current,
        guard=lambda _: None,
        build=lambda snapshot: IntendedMutation(replacements=(_replacement(snapshot),)),
    )

    assert result.canonical_applied
    assert not result.views_current
    assert result.intended_paths == (
        PurePosixPath("registry.toml"),
        PurePosixPath("views/decisions.md"),
    )
    assert result.changed_paths == (PurePosixPath("registry.toml"),)


def test_observe_only_renderer_failure_never_claims_view_write_intent(
    tmp_path: Path,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    views = RecordingViews(events, locks, fail=True)

    locks.active = True
    result = apply_views(
        repository,
        repository,
        current.selected.location,
        views,
        current.selected,
        write=False,
    )

    assert result.intended_paths == ()
    assert result.changed_paths == ()
    assert not result.current
    assert result.comparisons
    assert all(not comparison.matches for comparison in result.comparisons)


def test_finalizer_rejects_a_durable_result_that_differs_from_the_projection(
    tmp_path: Path,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = StaleReloadRepository(repository, events, locks, current.selected)
    projector = RecordingProjector(repository, events, locks)

    with pytest.raises(InvalidMutationState) as captured:
        MutationExecutor(
            adapter,
            adapter,
            locks,
            RecordingViews(events, locks),
            projector=projector,
            validator=lambda _: (),
        ).execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda snapshot: IntendedMutation(replacements=(_replacement(snapshot),)),
        )

    assert captured.value.diagnostics[0].code == "ORC007"


def test_mutation_deletes_inapplicable_managed_views_under_the_same_lock(
    tmp_path: Path,
) -> None:
    repository, current = _state(tmp_path)
    root = current.selected.location.root
    views_root = root / "views"
    views_root.mkdir(exist_ok=True)
    managed = MarkdownViewRenderer().managed_paths()
    for path in managed:
        root.joinpath(*path.parts).write_bytes(b"sensitive or stale\n")
    store_path = root / "store.toml"
    replacement = FileReplacement(
        PurePosixPath("store.toml"),
        store_path.read_bytes().replace(b"active_tasks = true", b"active_tasks = false"),
    )

    result = MutationExecutor(
        repository,
        repository,
        FileLockManager(),
        MarkdownViewRenderer(),
        projector=repository,
    ).execute(
        locations=tuple(value.location for value in current.stores),
        selected=current.selected.location,
        load=lambda: current,
        guard=lambda _: None,
        build=lambda _: IntendedMutation(replacements=(replacement,)),
    )

    deleted = {
        PurePosixPath("views/roadmap.md"),
        PurePosixPath("views/backlog.md"),
        PurePosixPath("views/inbox.md"),
    }
    assert deleted <= set(result.intended_paths)
    assert deleted <= set(result.changed_paths)
    assert all(not root.joinpath(*path.parts).exists() for path in deleted)
    assert root.joinpath("views/decisions.md").is_file()
