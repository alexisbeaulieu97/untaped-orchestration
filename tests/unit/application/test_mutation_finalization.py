from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import STORE_ID
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.item_support import (
    MutationExecutionScope,
    execute_mutation,
)
from untaped_orchestration.application.mutations import (
    IntendedMutation,
    InvalidMutationState,
    MutationExecutor,
    MutationLockSetError,
    MutationWriteError,
    validate_selected_local,
)
from untaped_orchestration.application.ports import (
    FileDeletion,
    FileReplacement,
    StoreLockTimeout,
)
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    FederationAnchor,
    ItemRevision,
)
from untaped_orchestration.application.view_management import apply_views
from untaped_orchestration.cli.output import CommandResult, run_command
from untaped_orchestration.domain.diagnostics import DiagnosticError, expected_diagnostic
from untaped_orchestration.infrastructure.filesystem import PathSafetyError, location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer, ViewError


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


class FailingCanonicalWriter(RecordingRepository):
    def __init__(self, delegate, events, locks, *, failure: Exception, fail_on: int) -> None:
        super().__init__(delegate, events, locks)
        self.failure = failure
        self.fail_on = fail_on
        self.calls = 0

    def replace(self, location, change) -> None:
        self.calls += 1
        if self.calls == self.fail_on:
            raise self.failure
        super().replace(location, change)


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


class MultipleRecordingViews(RecordingViews):
    def managed_paths(self) -> tuple[PurePosixPath, ...]:
        return (
            PurePosixPath("views/decisions.md"),
            PurePosixPath("views/roadmap.md"),
        )

    def expected(self, snapshot) -> Mapping[PurePosixPath, bytes]:
        del snapshot
        assert self.locks.active
        self.events.append("render")
        return {
            PurePosixPath("views/decisions.md"): b"decisions\n",
            PurePosixPath("views/roadmap.md"): b"roadmap\n",
        }


class TypedFailingViews(RecordingViews):
    def __init__(
        self,
        events: list[str],
        locks: RecordingLocks,
        error: DiagnosticError,
    ) -> None:
        super().__init__(events, locks)
        self.error = error

    def expected(self, snapshot) -> Mapping[PurePosixPath, bytes]:
        del snapshot
        assert self.locks.active
        raise self.error


class FailReadAfterFirstComparison(RecordingRepository):
    def __init__(self, delegate, events, locks, error: DiagnosticError) -> None:
        super().__init__(delegate, events, locks)
        self.error = error
        self.reads = 0

    def read_file(self, location, path):
        self.reads += 1
        if self.reads == 2:
            raise self.error
        return super().read_file(location, path)


def test_execute_mutation_invokes_one_scope_factory_immediately_before_execution() -> None:
    location = location_from_root(Path("/"))
    events: list[str] = []
    sentinel = object()

    def load() -> FederatedSnapshot:
        raise AssertionError("fake executor must not invoke the loader")

    def factory() -> MutationExecutionScope:
        events.append("factory")
        return MutationExecutionScope((location,), location, load)

    class Executor:
        def execute(self, **kwargs):
            events.append("execute")
            assert kwargs["locations"] == (location,)
            assert kwargs["selected"] == location
            assert kwargs["load"] is load
            return sentinel

    result = execute_mutation(
        Executor(),  # type: ignore[arg-type]
        factory,
        lambda _: None,
        lambda _: IntendedMutation(),
    )

    assert result is sentinel
    assert events == ["factory", "execute"]


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


def test_finalizer_invokes_distinct_current_and_projected_validators_in_order(
    tmp_path: Path,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = RecordingRepository(repository, events, locks)

    def current_validator(snapshot: FederatedSnapshot):
        del snapshot
        events.append("validate-current-only")
        return ()

    def projected_validator(snapshot: FederatedSnapshot):
        del snapshot
        events.append("validate-projected-only")
        return ()

    MutationExecutor(
        adapter,
        adapter,
        locks,
        RecordingViews(events, locks),
        projector=RecordingProjector(repository, events, locks),
    ).execute(
        locations=tuple(value.location for value in current.stores),
        selected=current.selected.location,
        load=lambda: current,
        guard=lambda _: events.append("guard"),
        build=lambda _: events.append("build") or IntendedMutation(),
        current_validator=current_validator,
        projected_validator=projected_validator,
    )

    assert events.index("validate-current-only") < events.index("guard")
    assert events.index("guard") < events.index("build")
    assert events.count("validate-current-only") == 1
    assert events.count("validate-projected-only") == 2


@pytest.mark.parametrize("failure", ["current", "projected"])
def test_finalizer_validator_failure_prevents_canonical_writes(
    tmp_path: Path,
    failure: str,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = RecordingRepository(repository, events, locks)
    diagnostics = expected_diagnostic("ORC002", f"{failure} state rejected")

    def current_validator(snapshot: FederatedSnapshot):
        del snapshot
        events.append("validate-current-only")
        return diagnostics if failure == "current" else ()

    def projected_validator(snapshot: FederatedSnapshot):
        del snapshot
        events.append("validate-projected-only")
        return diagnostics if failure == "projected" else ()

    with pytest.raises(InvalidMutationState):
        MutationExecutor(
            adapter,
            adapter,
            locks,
            RecordingViews(events, locks),
            projector=RecordingProjector(repository, events, locks),
        ).execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: events.append("guard"),
            build=lambda snapshot: (
                events.append("build") or IntendedMutation(replacements=(_replacement(snapshot),))
            ),
            current_validator=current_validator,
            projected_validator=projected_validator,
        )

    assert adapter.writes == []
    if failure == "current":
        assert "guard" not in events
        assert "build" not in events
        assert "validate-projected-only" not in events
    else:
        assert events.count("validate-projected-only") == 1


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


def test_finalizer_exact_lock_set_includes_unexposed_participant_anchors(
    tmp_path: Path,
) -> None:
    repository, resolved = _state(tmp_path)
    participants = tuple(
        FederationAnchor(
            store.location,
            store.store_config_revision,
            store.registry_revision,
        )
        for store in resolved.stores
    )
    current = replace(
        resolved,
        stores=(resolved.selected,),
        participants=participants,
    )
    events: list[str] = []
    locks = RecordingLocks(events)
    adapter = RecordingRepository(repository, events, locks)

    MutationExecutor(
        adapter,
        adapter,
        locks,
        RecordingViews(events, locks),
        projector=RecordingProjector(repository, events, locks),
    ).execute(
        locations=tuple(anchor.location for anchor in participants),
        selected=current.selected.location,
        load=lambda: current,
        guard=lambda _: None,
        build=lambda _: IntendedMutation(),
        validator=lambda _: (),
        dry_run=True,
    )

    assert events[0] == "lock:2"


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


def test_apply_views_preserves_typed_initial_comparison_failure(tmp_path: Path) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    error = ViewError("typed initial view failure")
    locks.active = True

    with pytest.raises(ViewError) as captured:
        apply_views(
            repository,
            repository,
            current.selected.location,
            TypedFailingViews(events, locks, error),
            current.selected,
        )

    assert captured.value is error


def test_apply_views_preserves_typed_view_writer_failure(tmp_path: Path) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    error = StoreLockTimeout(current.selected.location)
    writer = FailingCanonicalWriter(repository, events, locks, failure=error, fail_on=1)
    locks.active = True

    with pytest.raises(StoreLockTimeout) as captured:
        apply_views(
            repository,
            writer,
            current.selected.location,
            RecordingViews(events, locks),
            current.selected,
        )

    assert captured.value is error


def test_apply_views_preserves_typed_post_render_comparison_failure(tmp_path: Path) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    error = PathSafetyError(PurePosixPath("views/decisions.md"), "unsafe rendered view")
    reader = FailReadAfterFirstComparison(repository, events, locks, error)
    locks.active = True

    with pytest.raises(PathSafetyError) as captured:
        apply_views(
            reader,
            repository,
            current.selected.location,
            RecordingViews(events, locks),
            current.selected,
        )

    assert captured.value is error


def test_mutation_finalization_preserves_typed_view_writer_failure(tmp_path: Path) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    error = StoreLockTimeout(current.selected.location)
    writer = FailingCanonicalWriter(repository, events, locks, failure=error, fail_on=2)

    with pytest.raises(StoreLockTimeout) as captured:
        MutationExecutor(
            repository,
            writer,
            locks,
            RecordingViews(events, locks),
            projector=repository,
        ).execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda snapshot: IntendedMutation(replacements=(_replacement(snapshot),)),
        )

    assert captured.value is error
    durable = repository.load_local(current.selected.location, headers_only=False)
    assert captured.value.receipt.canonical_applied is True  # type: ignore[attr-defined]
    assert captured.value.receipt.views_current is False  # type: ignore[attr-defined]
    assert captured.value.receipt.intended_paths == (  # type: ignore[attr-defined]
        PurePosixPath("registry.toml"),
    )
    assert captured.value.receipt.changed_paths == (  # type: ignore[attr-defined]
        PurePosixPath("registry.toml"),
    )
    assert captured.value.receipt.item_revisions == tuple(  # type: ignore[attr-defined]
        ItemRevision(record.path, record.revision) for record in durable.records
    )
    assert captured.value.receipt.store_revision == durable.store_revision  # type: ignore[attr-defined]
    assert captured.value.receipt.registry_revision == durable.registry_revision  # type: ignore[attr-defined]


def test_mutation_finalization_receipt_includes_only_acknowledged_view_paths(
    tmp_path: Path,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    error = StoreLockTimeout(current.selected.location)
    writer = FailingCanonicalWriter(repository, events, locks, failure=error, fail_on=3)
    roadmap_path = current.selected.location.root / "views" / "roadmap.md"
    roadmap_before = roadmap_path.read_bytes()

    with pytest.raises(StoreLockTimeout) as captured:
        MutationExecutor(
            repository,
            writer,
            locks,
            MultipleRecordingViews(events, locks),
            projector=repository,
        ).execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda snapshot: IntendedMutation(replacements=(_replacement(snapshot),)),
        )

    assert captured.value is error
    assert captured.value.receipt.intended_paths == (  # type: ignore[attr-defined]
        PurePosixPath("registry.toml"),
        PurePosixPath("views/decisions.md"),
    )
    assert captured.value.receipt.changed_paths == (  # type: ignore[attr-defined]
        PurePosixPath("registry.toml"),
        PurePosixPath("views/decisions.md"),
    )
    assert current.selected.location.root.joinpath("views/decisions.md").read_bytes() == (
        b"decisions\n"
    )
    assert roadmap_path.read_bytes() == roadmap_before


@pytest.mark.parametrize("fmt", ["json", "table"])
def test_typed_view_finalization_failure_emits_durable_receipt_and_exact_exit(
    tmp_path: Path,
    fmt: str,
    capfd,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    writer = FailingCanonicalWriter(
        repository,
        events,
        locks,
        failure=StoreLockTimeout(current.selected.location),
        fail_on=2,
    )
    executor = MutationExecutor(
        repository,
        writer,
        locks,
        RecordingViews(events, locks),
        projector=repository,
    )

    def fail() -> CommandResult:
        executor.execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda snapshot: IntendedMutation(replacements=(_replacement(snapshot),)),
        )
        raise AssertionError("typed view failure was not raised")

    with pytest.raises(SystemExit) as captured:
        run_command(
            "task update",
            fail,
            fmt=fmt,  # type: ignore[arg-type]
            allowed=("json", "table"),
        )

    assert captured.value.code == 4
    output = capfd.readouterr()
    assert "registry.toml" in output.out
    if fmt == "json":
        payload = json.loads(output.out)
        durable = repository.load_local(current.selected.location, headers_only=False)
        assert payload["data"]["canonical_applied"] is True
        assert payload["data"]["views_current"] is False
        assert payload["data"]["intended_paths"] == ["registry.toml"]
        assert payload["data"]["changed_paths"] == ["registry.toml"]
        assert payload["data"]["store_revision"] == durable.store_revision.root
        assert payload["data"]["registry_revision"] == durable.registry_revision.root
        assert output.err == ""
    else:
        assert output.out.startswith(
            "applied\treplayed\tcanonical_applied\tviews_current\tintended_paths"
        )
        assert output.err == (
            "ORC007: "
            f"{current.selected.location.real_root.as_posix()}: "
            "timed out acquiring orchestration store lock\n"
        )


def test_later_canonical_failure_receipt_preserves_acknowledged_paths(
    tmp_path: Path,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    writer = FailingCanonicalWriter(
        repository,
        events,
        locks,
        failure=OSError("second canonical write failed"),
        fail_on=2,
    )
    store_path = current.selected.location.root / "store.toml"
    store_replacement = FileReplacement(
        PurePosixPath("store.toml"),
        store_path.read_bytes(),
    )

    with pytest.raises(MutationWriteError) as captured:
        MutationExecutor(
            repository,
            writer,
            locks,
            RecordingViews(events, locks),
            projector=repository,
        ).execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda snapshot: IntendedMutation(
                replacements=(store_replacement, _replacement(snapshot))
            ),
        )

    assert captured.value.receipt.applied is True
    assert captured.value.receipt.canonical_applied is True
    assert captured.value.receipt.views_current is False
    assert captured.value.receipt.changed_paths == (PurePosixPath("store.toml"),)


def test_canonical_writer_preserves_typed_diagnostic_error(tmp_path: Path) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    timeout = StoreLockTimeout(current.selected.location)
    writer = FailingCanonicalWriter(
        repository,
        events,
        locks,
        failure=timeout,
        fail_on=1,
    )

    with pytest.raises(StoreLockTimeout) as captured:
        MutationExecutor(
            repository,
            writer,
            locks,
            RecordingViews(events, locks),
            projector=repository,
        ).execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda snapshot: IntendedMutation(replacements=(_replacement(snapshot),)),
        )

    assert captured.value is timeout
    assert captured.value.diagnostics[0].code == "ORC007"
    assert captured.value.receipt.applied is False  # type: ignore[attr-defined]
    assert captured.value.receipt.canonical_applied is False  # type: ignore[attr-defined]
    assert captured.value.receipt.views_current is False  # type: ignore[attr-defined]
    assert captured.value.receipt.changed_paths == ()  # type: ignore[attr-defined]


@pytest.mark.parametrize("fmt", ["json", "table"])
def test_later_typed_writer_failure_emits_partial_receipt_and_exact_diagnostic(
    tmp_path: Path,
    fmt: str,
    capfd,
) -> None:
    repository, current = _state(tmp_path)
    events: list[str] = []
    locks = RecordingLocks(events)
    writer = FailingCanonicalWriter(
        repository,
        events,
        locks,
        failure=StoreLockTimeout(current.selected.location),
        fail_on=2,
    )
    store_path = current.selected.location.root / "store.toml"
    store_replacement = FileReplacement(
        PurePosixPath("store.toml"),
        store_path.read_bytes(),
    )
    executor = MutationExecutor(
        repository,
        writer,
        locks,
        RecordingViews(events, locks),
        projector=repository,
    )

    def fail() -> CommandResult:
        executor.execute(
            locations=tuple(value.location for value in current.stores),
            selected=current.selected.location,
            load=lambda: current,
            guard=lambda _: None,
            build=lambda snapshot: IntendedMutation(
                replacements=(store_replacement, _replacement(snapshot))
            ),
        )
        raise AssertionError("typed writer failure was not raised")

    with pytest.raises(SystemExit) as captured:
        run_command(
            "repair frontmatter",
            fail,
            fmt=fmt,  # type: ignore[arg-type]
            allowed=("json", "table"),
        )

    assert captured.value.code == 4
    output = capfd.readouterr()
    assert "store.toml" in output.out
    assert "registry.toml" in output.out
    if fmt == "json":
        payload = json.loads(output.out)
        assert payload["data"]["applied"] is True
        assert payload["data"]["canonical_applied"] is True
        assert payload["data"]["views_current"] is False
        assert payload["data"]["changed_paths"] == ["store.toml"]
        assert payload["diagnostics"] == [
            {
                "code": "ORC007",
                "severity": "error",
                "path": current.selected.location.real_root.as_posix(),
                "field": "lock",
                "message": "timed out acquiring orchestration store lock",
                "hint": "Retry after the current store mutation finishes.",
            }
        ]
        assert output.err == ""
    else:
        assert output.out.startswith(
            "applied\treplayed\tcanonical_applied\tviews_current\tintended_paths"
        )
        assert output.err == (
            "ORC007: "
            f"{current.selected.location.real_root.as_posix()}: "
            "timed out acquiring orchestration store lock\n"
        )


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
