from __future__ import annotations

from pathlib import PurePosixPath

from untaped_orchestration.application.ports import (
    CanonicalFormatter,
    FileReplacement,
    LockManager,
    StoreLocation,
    StoreReader,
    StoreWriter,
    ViewRenderer,
)
from untaped_orchestration.application.results import (
    CheckResult,
    Completeness,
    FederatedSnapshot,
    ItemRevision,
    MaintenanceResult,
    MutationReceipt,
    PathComparison,
    StoreSnapshot,
)
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.application.view_management import apply_views, view_comparisons
from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics
from untaped_orchestration.domain.models import Revision

DEFAULT_LOCK_TIMEOUT = 10.0


class InvalidStoreState(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...], result: CheckResult) -> None:
        self.diagnostics = diagnostics
        self.result = result
        super().__init__("store state is invalid")


class RevisionConflict(ValueError):
    pass


def _federated(snapshot: StoreSnapshot) -> FederatedSnapshot:
    return FederatedSnapshot(snapshot, (snapshot,), Completeness())


def _validate(snapshot: StoreSnapshot) -> tuple[Diagnostic, ...]:
    return validate_snapshot(_federated(snapshot), require_children=True)


def _invalid(diagnostics: tuple[Diagnostic, ...]) -> bool:
    return any(value.severity == "error" for value in diagnostics)


def _invalid_result(
    snapshot: StoreSnapshot,
    diagnostics: tuple[Diagnostic, ...],
) -> CheckResult:
    store_id = (
        snapshot.store.id.root
        if snapshot.store is not None
        else snapshot.registry.store_id.root
        if snapshot.registry is not None
        else ""
    )
    return CheckResult(
        store_id=store_id,
        store_revision=snapshot.store_revision,
        registry_revision=snapshot.registry_revision,
        valid=False,
        views_current=False,
        diagnostics=diagnostics,
    )


def _shape_diagnostic(path: PurePosixPath, message: str) -> Diagnostic:
    return Diagnostic(
        code="ORC003",
        severity="error",
        path=path.as_posix(),
        field="path",
        message=message,
        hint="Restore the exact scaffold or remove only the proven orphaned entry.",
    )


def _store_shape_diagnostics(
    reader: StoreReader,
    location: StoreLocation,
    managed_views: tuple[PurePosixPath, ...],
) -> tuple[Diagnostic, ...]:
    entries = reader.list_entries(location)
    files = {value.path for value in entries if value.kind == "file"}
    diagnostics: list[Diagnostic] = []
    for required in (
        PurePosixPath("registry.toml"),
        PurePosixPath("AGENTS.md"),
        PurePosixPath("CLAUDE.md"),
    ):
        if required not in files:
            diagnostics.append(_shape_diagnostic(required, "required scaffold file is missing"))

    allowed_directories = {
        PurePosixPath("tasks"),
        PurePosixPath("decisions"),
        PurePosixPath("archive"),
        PurePosixPath("archive/tasks"),
        PurePosixPath("views"),
    }
    allowed_files = {
        PurePosixPath("store.toml"),
        PurePosixPath("registry.toml"),
        PurePosixPath("AGENTS.md"),
        PurePosixPath("CLAUDE.md"),
        PurePosixPath(".lock"),
        *managed_views,
    }
    for entry in entries:
        path = entry.path
        name = path.name
        if ".untaped-tmp-" in name:
            diagnostics.append(_shape_diagnostic(path, "orphan atomic-write temporary exists"))
            continue
        if entry.kind in {"symlink", "other"}:
            diagnostics.append(_shape_diagnostic(path, f"unsafe {entry.kind} entry exists"))
            continue
        if entry.kind == "directory":
            if path not in allowed_directories:
                diagnostics.append(_shape_diagnostic(path, "unexpected directory exists"))
            continue
        if path in allowed_files:
            continue
        if path.parts[:-1] in {("tasks",), ("decisions",), ("archive", "tasks")}:
            continue
        if name == ".DS_Store" or name.endswith(("~", ".swp", ".swo", ".tmp")):
            continue
        if name.startswith((".#", "#")):
            continue
        diagnostics.append(_shape_diagnostic(path, "unexpected store file exists"))
    return sort_diagnostics(diagnostics)


def _comparisons(
    reader: StoreReader,
    location: StoreLocation,
    expected: dict[PurePosixPath, bytes],
) -> tuple[PathComparison, ...]:
    values = []
    for path, content in expected.items():
        try:
            matches = reader.read_file(location, path).content == content
        except FileNotFoundError:
            matches = False
        values.append(PathComparison(path, matches))
    return tuple(values)


def _receipt(
    snapshot: StoreSnapshot,
    *,
    intended: tuple[PurePosixPath, ...],
    changed: tuple[PurePosixPath, ...],
    canonical_applied: bool,
    views_current: bool,
) -> MutationReceipt:
    return MutationReceipt(
        applied=bool(changed),
        replayed=False,
        canonical_applied=canonical_applied,
        views_current=views_current,
        intended_paths=intended,
        changed_paths=changed,
        item_revisions=tuple(
            ItemRevision(value.path, value.revision) for value in snapshot.records
        ),
        store_revision=snapshot.store_revision,
        registry_revision=snapshot.registry_revision,
    )


class CheckStore:
    def __init__(
        self,
        reader: StoreReader,
        locks: LockManager,
        views: ViewRenderer,
        *,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._reader = reader
        self._locks = locks
        self._views = views
        self._lock_timeout = lock_timeout

    def execute(self, location: StoreLocation) -> CheckResult:
        with self._locks.acquire((location,), timeout=self._lock_timeout):
            snapshot = self._reader.load_local(location, headers_only=False)
            semantic_diagnostics = _validate(snapshot)
            diagnostics = list(semantic_diagnostics)
            diagnostics.extend(
                _store_shape_diagnostics(self._reader, location, self._views.managed_paths())
            )
            view_matches = False
            if snapshot.store is not None and not _invalid(semantic_diagnostics):
                _, managed = view_comparisons(self._reader, location, self._views, snapshot)
                view_matches = all(managed.values())
                for path, matches in managed.items():
                    if not matches:
                        diagnostics.append(
                            Diagnostic(
                                code="ORC008",
                                severity="error",
                                path=path.as_posix(),
                                field="",
                                message="generated view is missing, stale, or inapplicable",
                                hint="Run render --write to reconcile the managed view set.",
                            )
                        )
            ordered = sort_diagnostics(diagnostics)
            store_id = (
                snapshot.store.id.root
                if snapshot.store is not None
                else snapshot.registry.store_id.root
                if snapshot.registry is not None
                else ""
            )
            return CheckResult(
                store_id=store_id,
                store_revision=snapshot.store_revision,
                registry_revision=snapshot.registry_revision,
                valid=not _invalid(ordered),
                views_current=view_matches,
                diagnostics=ordered,
            )


class RenderStore:
    def __init__(
        self,
        reader: StoreReader,
        writer: StoreWriter,
        locks: LockManager,
        views: ViewRenderer,
        *,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._locks = locks
        self._views = views
        self._lock_timeout = lock_timeout

    def check(self, location: StoreLocation) -> MaintenanceResult:
        return self._execute(location, write=False)

    def write(self, location: StoreLocation) -> MaintenanceResult:
        return self._execute(location, write=True)

    def _execute(self, location: StoreLocation, *, write: bool) -> MaintenanceResult:
        with self._locks.acquire((location,), timeout=self._lock_timeout):
            snapshot = self._reader.load_local(location, headers_only=False)
            diagnostics = _validate(snapshot)
            if _invalid(diagnostics):
                raise InvalidStoreState(diagnostics, _invalid_result(snapshot, diagnostics))
            expected, managed = view_comparisons(self._reader, location, self._views, snapshot)
            intended = tuple(
                path
                for path in self._views.managed_paths()
                if path in expected or not managed[path]
            )
            if write:
                state = apply_views(self._reader, self._writer, location, self._views, snapshot)
                changed = state.changed_paths
                views_current = state.current
                result_comparisons = tuple(
                    PathComparison(path, state.current or path not in state.intended_paths)
                    for path in self._views.managed_paths()
                )
            else:
                changed = ()
                views_current = all(managed.values())
                result_comparisons = tuple(
                    PathComparison(path, matches) for path, matches in managed.items()
                )
            return MaintenanceResult(
                _receipt(
                    snapshot,
                    intended=intended,
                    changed=changed,
                    canonical_applied=False,
                    views_current=views_current,
                ),
                result_comparisons,
                diagnostics,
            )


class FormatStore:
    def __init__(
        self,
        reader: StoreReader,
        writer: StoreWriter,
        locks: LockManager,
        views: ViewRenderer,
        formatter: CanonicalFormatter,
        *,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._locks = locks
        self._views = views
        self._formatter = formatter
        self._lock_timeout = lock_timeout

    def check(self, location: StoreLocation) -> MaintenanceResult:
        return self._execute(location, write=False, expected_store_revision=None)

    def write(
        self,
        location: StoreLocation,
        *,
        expected_store_revision: Revision | str,
    ) -> MaintenanceResult:
        return self._execute(
            location,
            write=True,
            expected_store_revision=(
                expected_store_revision
                if isinstance(expected_store_revision, Revision)
                else Revision(expected_store_revision)
            ),
        )

    def _execute(
        self,
        location: StoreLocation,
        *,
        write: bool,
        expected_store_revision: Revision | None,
    ) -> MaintenanceResult:
        with self._locks.acquire((location,), timeout=self._lock_timeout):
            snapshot = self._reader.load_local(location, headers_only=False)
            diagnostics = _validate(snapshot)
            if _invalid(diagnostics) or snapshot.store is None or snapshot.registry is None:
                raise InvalidStoreState(diagnostics, _invalid_result(snapshot, diagnostics))
            if (
                expected_store_revision is not None
                and snapshot.store_revision != expected_store_revision
            ):
                raise RevisionConflict("store revision guard does not match current state")

            expected: dict[PurePosixPath, bytes] = {
                PurePosixPath("store.toml"): self._formatter.store_bytes(snapshot.store),
                PurePosixPath("registry.toml"): self._formatter.registry_bytes(snapshot.registry),
            }
            for record in snapshot.records:
                assert record.body is not None
                expected[record.path] = self._formatter.item_bytes(record.metadata, record.body)
            comparisons = _comparisons(self._reader, location, expected)
            changed: list[PurePosixPath] = []
            if write:
                for comparison in comparisons:
                    if not comparison.matches:
                        self._writer.replace(
                            location, FileReplacement(comparison.path, expected[comparison.path])
                        )
                        changed.append(comparison.path)

            canonical_changed = bool(changed)
            after = self._reader.load_local(location, headers_only=False) if changed else snapshot
            intended = list(expected)
            if write:
                view_state = apply_views(self._reader, self._writer, location, self._views, after)
                intended.extend(view_state.intended_paths)
                changed.extend(view_state.changed_paths)
                views_current = view_state.current
            else:
                try:
                    _, managed = view_comparisons(self._reader, location, self._views, after)
                    views_current = all(managed.values())
                except OSError, ValueError:
                    views_current = False
            result_comparisons = (
                tuple(PathComparison(path, True) for path in expected) if write else comparisons
            )
            return MaintenanceResult(
                _receipt(
                    after,
                    intended=tuple(intended),
                    changed=tuple(changed),
                    canonical_applied=canonical_changed,
                    views_current=views_current,
                ),
                result_comparisons,
                diagnostics,
            )
