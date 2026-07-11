from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from untaped_orchestration.application.federation import FederationService
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
from untaped_orchestration.application.scaffold import ShapeInspection, inspect_store_shape
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.application.view_management import apply_views, view_comparisons
from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics
from untaped_orchestration.domain.models import Revision

DEFAULT_LOCK_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class RecursiveCheckRequest:
    location: StoreLocation
    local: bool = False
    require_children: bool = False


@dataclass(frozen=True, slots=True)
class RecursiveFormatRequest:
    location: StoreLocation
    local: bool = False


@dataclass(frozen=True, slots=True)
class RecursiveCheckResult:
    valid: bool
    complete: bool
    checks: tuple[CheckResult, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class RecursiveFormatResult:
    comparisons: tuple[PathComparison, ...]
    complete: bool
    diagnostics: tuple[Diagnostic, ...]

    @property
    def matches(self) -> bool:
        return all(value.matches for value in self.comparisons)


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


def _store_id(snapshot: StoreSnapshot) -> str | None:
    if snapshot.store is not None:
        return snapshot.store.id.root
    if snapshot.registry is not None:
        return snapshot.registry.store_id.root
    return None


def _invalid_result(
    snapshot: StoreSnapshot,
    diagnostics: tuple[Diagnostic, ...],
) -> CheckResult:
    return CheckResult(
        store_id=_store_id(snapshot),
        store_revision=snapshot.store_revision,
        registry_revision=snapshot.registry_revision,
        valid=False,
        views_current=False,
        diagnostics=diagnostics,
    )


def _shape_result(
    reader: StoreReader,
    location: StoreLocation,
    inspection: ShapeInspection,
) -> CheckResult:
    administrative = reader.inspect_administrative(location)
    return CheckResult(
        store_id=administrative.store_id,
        store_revision=None,
        registry_revision=administrative.registry_revision,
        valid=False,
        views_current=False,
        diagnostics=inspection.diagnostics,
    )


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
            inspection = inspect_store_shape(self._reader, location)
            if not inspection.load_safe:
                return _shape_result(self._reader, location, inspection)
            snapshot = self._reader.load_local(location, headers_only=False)
            semantic_diagnostics = _validate(snapshot)
            diagnostics = list(semantic_diagnostics)
            diagnostics.extend(inspection.diagnostics)
            view_matches = False
            if snapshot.store is not None and not _invalid(tuple(diagnostics)):
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
            return CheckResult(
                store_id=_store_id(snapshot),
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
            inspection = inspect_store_shape(self._reader, location)
            if inspection.diagnostics:
                raise InvalidStoreState(
                    inspection.diagnostics,
                    _shape_result(self._reader, location, inspection),
                )
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
                result_comparisons = state.comparisons
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
            inspection = inspect_store_shape(self._reader, location)
            if inspection.diagnostics:
                raise InvalidStoreState(
                    inspection.diagnostics,
                    _shape_result(self._reader, location, inspection),
                )
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


class RecursiveMaintenanceService:
    """Coordinates recursive read-only maintenance while keeping writes selected-local."""

    def __init__(
        self,
        federation: FederationService,
        reader: StoreReader,
        formatter: CanonicalFormatter,
        views: ViewRenderer,
        *,
        local_formatter: FormatStore | None = None,
        local_renderer: RenderStore | None = None,
    ) -> None:
        self._federation = federation
        self._reader = reader
        self._formatter = formatter
        self._views = views
        self._local_formatter = local_formatter
        self._local_renderer = local_renderer

    def check(self, request: RecursiveCheckRequest) -> RecursiveCheckResult:
        snapshot = self._federation.load(
            request.location,
            local=request.local,
            headers_only=False,
        )
        diagnostics = list(validate_snapshot(snapshot, require_children=request.require_children))
        selected_views_current = False
        if snapshot.selected.store is not None and not any(
            value.severity == "error" for value in snapshot.selected.load_diagnostics
        ):
            try:
                _, managed = view_comparisons(
                    self._reader,
                    snapshot.selected.location,
                    self._views,
                    snapshot.selected,
                )
                selected_views_current = all(managed.values())
                for path, matches in managed.items():
                    if not matches:
                        diagnostics.append(
                            Diagnostic(
                                code="ORC008",
                                severity="error",
                                path=path.as_posix(),
                                field="",
                                message="generated view is missing, stale, or inapplicable",
                                hint="Run render --write locally in the selected store.",
                            )
                        )
            except OSError, ValueError:
                selected_views_current = False
        ordered = sort_diagnostics(diagnostics)
        checks = []
        for store in snapshot.stores:
            local_diagnostics = tuple(
                value
                for value in ordered
                if value.path.startswith(store.location.root.as_posix())
                or value.path in {record.path.as_posix() for record in store.records}
            )
            checks.append(
                CheckResult(
                    _store_id(store),
                    store.store_revision,
                    store.registry_revision,
                    not any(value.severity == "error" for value in local_diagnostics),
                    selected_views_current
                    if store.location.real_root == snapshot.selected.location.real_root
                    else True,
                    local_diagnostics,
                )
            )
        return RecursiveCheckResult(
            not any(value.severity == "error" for value in ordered),
            True if request.local else snapshot.completeness.complete,
            tuple(checks),
            ordered,
        )

    def fmt_check(self, request: RecursiveFormatRequest) -> RecursiveFormatResult:
        snapshot = self._federation.load(
            request.location,
            local=request.local,
            headers_only=False,
        )
        diagnostics = validate_snapshot(snapshot, require_children=False)
        comparisons: list[PathComparison] = []
        for store in snapshot.stores:
            if store.store is None or store.registry is None:
                continue
            expected = {
                PurePosixPath("store.toml"): self._formatter.store_bytes(store.store),
                PurePosixPath("registry.toml"): self._formatter.registry_bytes(store.registry),
            }
            for record in store.records:
                assert record.body is not None
                expected[record.path] = self._formatter.item_bytes(record.metadata, record.body)
            comparisons.extend(_comparisons(self._reader, store.location, expected))
        return RecursiveFormatResult(
            tuple(comparisons),
            True if request.local else snapshot.completeness.complete,
            diagnostics,
        )

    def fmt_write(
        self,
        request: RecursiveFormatRequest,
        *,
        expected_store_revision: Revision | str | None,
    ) -> MaintenanceResult:
        if not request.local:
            raise ValueError("fmt --write requires local mode")
        if expected_store_revision is None:
            raise ValueError("fmt --write requires a store revision")
        if self._local_formatter is None:
            raise RuntimeError("local formatter was not configured")
        return self._local_formatter.write(
            request.location,
            expected_store_revision=expected_store_revision,
        )

    def render_check(self, location: StoreLocation) -> MaintenanceResult:
        if self._local_renderer is None:
            raise RuntimeError("local renderer was not configured")
        return self._local_renderer.check(location)

    def render_write(self, location: StoreLocation) -> MaintenanceResult:
        if self._local_renderer is None:
            raise RuntimeError("local renderer was not configured")
        return self._local_renderer.write(location)
