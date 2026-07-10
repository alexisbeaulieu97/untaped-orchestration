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
from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics
from untaped_orchestration.domain.models import Revision

DEFAULT_LOCK_TIMEOUT = 10.0


class InvalidStoreState(ValueError):
    pass


class RevisionConflict(ValueError):
    pass


def _federated(snapshot: StoreSnapshot) -> FederatedSnapshot:
    return FederatedSnapshot(snapshot, (snapshot,), Completeness())


def _validate(snapshot: StoreSnapshot) -> tuple[Diagnostic, ...]:
    return validate_snapshot(_federated(snapshot), require_children=True)


def _invalid(diagnostics: tuple[Diagnostic, ...]) -> bool:
    return any(value.severity == "error" for value in diagnostics)


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
            diagnostics = list(_validate(snapshot))
            expected = dict(self._views.expected(snapshot)) if snapshot.store is not None else {}
            comparisons = _comparisons(self._reader, location, expected)
            for comparison in comparisons:
                if not comparison.matches:
                    diagnostics.append(
                        Diagnostic(
                            code="ORC008",
                            severity="error",
                            path=comparison.path.as_posix(),
                            field="",
                            message="generated view is missing or stale",
                            hint="Run render --write to replace every applicable view.",
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
                views_current=(
                    snapshot.store is not None and all(value.matches for value in comparisons)
                ),
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
                raise InvalidStoreState("render requires valid canonical store state")
            expected = dict(self._views.expected(snapshot))
            comparisons = _comparisons(self._reader, location, expected)
            changed: list[PurePosixPath] = []
            if write:
                for comparison in comparisons:
                    if not comparison.matches:
                        self._writer.replace(
                            location, FileReplacement(comparison.path, expected[comparison.path])
                        )
                        changed.append(comparison.path)
            result_comparisons = (
                tuple(PathComparison(path, True) for path in expected) if write else comparisons
            )
            return MaintenanceResult(
                _receipt(
                    snapshot,
                    intended=tuple(expected),
                    changed=tuple(changed),
                    canonical_applied=False,
                    views_current=all(value.matches for value in result_comparisons),
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
                raise InvalidStoreState("fmt requires valid metadata")
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
            views_current = True
            intended = list(expected)
            if write and changed:
                try:
                    expected_views = self._views.expected(after)
                    intended.extend(expected_views)
                    for path, content in expected_views.items():
                        try:
                            matches = self._reader.read_file(location, path).content == content
                        except FileNotFoundError:
                            matches = False
                        if not matches:
                            self._writer.replace(location, FileReplacement(path, content))
                            changed.append(path)
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
