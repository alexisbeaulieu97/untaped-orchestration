from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from untaped_orchestration.application.ports import (
    CanonicalFormatter,
    FileReplacement,
    LockManager,
    StoreDiscoveryInvalid,
    StoreDiscoveryMissing,
    StoreLockTimeout,
    StoreReader,
    StoreWriter,
    ViewRenderer,
)
from untaped_orchestration.application.results import (
    AdministrativeState,
    Completeness,
    FederatedSnapshot,
    FederationAnchor,
    IncompletenessReason,
    IncompleteStore,
    ItemRevision,
    MutationReceipt,
    RawRecord,
    StoreEntry,
    StoreLocation,
    StoreSnapshot,
)
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.application.view_management import apply_views
from untaped_orchestration.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticError,
    DiagnosticSeverity,
    diagnostic_sort_key,
    expected_diagnostic,
    validation_diagnostic,
)
from untaped_orchestration.domain.ids import StoreId
from untaped_orchestration.domain.models import Registry, RegistryChild, Revision

DEFAULT_LOCK_TIMEOUT = 10.0


class RegistryRevisionConflict(DiagnosticError):
    def __init__(self, message: str) -> None:
        super().__init__(expected_diagnostic("ORC007", message, field="registry_revision"))


class RegistryMutationConflict(DiagnosticError):
    def __init__(self, message: str) -> None:
        super().__init__(expected_diagnostic("ORC005", message, field="children"))


class RegistryPathConflict(DiagnosticError):
    def __init__(self, error: ValidationError) -> None:
        super().__init__(
            validation_diagnostic(
                error,
                "ORC003",
                message_prefix="invalid registry child path",
            )
        )


@dataclass(frozen=True, slots=True)
class AddChildRequest:
    location: StoreLocation
    child_id: StoreId | str
    path: str
    expected_registry_revision: Revision | None
    force_current: bool = False

    def __post_init__(self) -> None:
        if self.force_current == (self.expected_registry_revision is not None):
            raise ValueError("provide exactly one of registry revision or force-current")


@dataclass(frozen=True, slots=True)
class RemoveChildRequest:
    location: StoreLocation
    child_id: StoreId | str
    expected_registry_revision: Revision | None
    force_current: bool = False

    def __post_init__(self) -> None:
        if self.force_current == (self.expected_registry_revision is not None):
            raise ValueError("provide exactly one of registry revision or force-current")


@dataclass(frozen=True, slots=True)
class ListChildrenRequest:
    location: StoreLocation
    limit: int = 50


@dataclass(frozen=True, slots=True)
class RegistryChildRow:
    store_id: StoreId
    path: str


@dataclass(frozen=True, slots=True)
class ListChildrenResult:
    children: tuple[RegistryChildRow, ...]
    registry_revision: Revision
    truncated: bool = False


class _NoopLocks:
    @contextmanager
    def acquire(self, locations: Sequence[StoreLocation], *, timeout: float) -> Iterator[None]:
        del locations, timeout
        yield


class UnidentifiedStoreError(DiagnosticError):
    """The selected snapshot has no truthful immutable identity."""

    def __init__(self, location: StoreLocation) -> None:
        self.location = location
        super().__init__(
            expected_diagnostic(
                "ORC003",
                "selected store identity is unreadable",
                path=location.root.as_posix(),
                field="id",
            )
        )


@dataclass(slots=True)
class _Participant:
    expected_store_id: StoreId
    snapshot: StoreSnapshot
    exposed: bool


@dataclass(slots=True)
class _Resolution:
    participants: dict[str, _Participant]
    locations: dict[str, StoreLocation]
    declared_ids: set[str]
    entries: list[IncompleteStore]


@dataclass(frozen=True, slots=True)
class FederationRead:
    snapshot: FederatedSnapshot
    reader: StoreReader | None


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path)).casefold()


def _location_sort_key(location: StoreLocation) -> tuple[str, str]:
    return (_path_key(location.real_root), str(location.real_root))


def _identity(snapshot: StoreSnapshot) -> StoreId | None:
    if snapshot.store is not None:
        return snapshot.store.id
    if snapshot.registry is not None:
        return snapshot.registry.store_id
    return None


def _registry_path(snapshot: StoreSnapshot) -> str:
    return (snapshot.location.root / "registry.toml").as_posix()


def _incomplete(
    expected_store_id: StoreId,
    *,
    reason: IncompletenessReason,
    code: DiagnosticCode,
    severity: DiagnosticSeverity,
    path: str,
    field: str,
    message: str,
    hint: str,
) -> IncompleteStore:
    return IncompleteStore(
        expected_store_id=expected_store_id,
        reason=reason,
        diagnostic=Diagnostic(
            code=code,
            severity=severity,
            path=path,
            field=field,
            message=message,
            hint=hint,
        ),
    )


def _sorted_entries(entries: list[IncompleteStore]) -> tuple[IncompleteStore, ...]:
    unique: dict[tuple[str, IncompletenessReason, str, str, str], IncompleteStore] = {}
    for entry in entries:
        diagnostic = entry.diagnostic
        key = (
            entry.expected_store_id.root,
            entry.reason,
            diagnostic.path,
            diagnostic.field,
            diagnostic.message,
        )
        unique[key] = entry
    return tuple(
        sorted(
            unique.values(),
            key=lambda entry: (
                diagnostic_sort_key(entry.diagnostic),
                entry.expected_store_id.root,
                entry.reason,
            ),
        )
    )


class FederationService:
    def __init__(
        self,
        reader: StoreReader,
        locks: LockManager,
        *,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        if lock_timeout < 0:
            raise ValueError("lock timeout must be nonnegative")
        self._reader = reader
        self._locks = locks
        self._lock_timeout = lock_timeout

    def load(
        self,
        location: StoreLocation,
        *,
        local: bool,
        headers_only: bool,
    ) -> FederatedSnapshot:
        return self.run(
            location,
            local=local,
            headers_only=headers_only,
            action=lambda lease: lease.snapshot,
        )

    def optimistic_locations(self, location: StoreLocation) -> tuple[StoreLocation, ...]:
        """Resolve one recursive header snapshot for a mutation's lock set."""
        resolution, _ = self._resolve_headers(location, local=False)
        return tuple(
            sorted(
                (participant.snapshot.location for participant in resolution.participants.values()),
                key=_location_sort_key,
            )
        )

    def run[T](
        self,
        location: StoreLocation,
        *,
        local: bool,
        headers_only: bool = True,
        action: Callable[[FederationRead], T],
    ) -> T:
        resolution, key = self._resolve_headers(location, local=local)

        ordered = sorted(
            (value.snapshot.location for value in resolution.participants.values()),
            key=_location_sort_key,
        )
        unavailable: FederatedSnapshot | None = None
        try:
            with self._locks.acquire(ordered, timeout=self._lock_timeout):
                self._reread_under_lock(
                    resolution,
                    headers_only=headers_only,
                )
                snapshot = self._snapshot(resolution, key)
                if any(entry.reason == "changed" for entry in resolution.entries):
                    unavailable = snapshot
                else:
                    fresh_resolution, fresh_key = self._resolve_headers(location, local=local)
                    fresh = self._snapshot(fresh_resolution, fresh_key)
                    if self._anchor_signature(snapshot) != self._anchor_signature(fresh):
                        self._record_change(
                            resolution.participants[key],
                            resolution,
                            "federation participant set changed after lock acquisition",
                        )
                        unavailable = self._snapshot(resolution, key)
                    else:
                        return action(FederationRead(snapshot, self._reader))
        except StoreLockTimeout as error:
            self._record_timeout(error, resolution)
        return action(
            FederationRead(
                unavailable or self._snapshot(resolution, key),
                None,
            )
        )

    def _resolve_headers(
        self,
        location: StoreLocation,
        *,
        local: bool,
    ) -> tuple[_Resolution, str]:
        selected = self._reader.load_local(location, headers_only=True)
        selected_id = _identity(selected)
        if selected_id is None:
            raise UnidentifiedStoreError(location)

        participant = _Participant(selected_id, selected, exposed=True)
        key = _path_key(location.real_root)
        resolution = _Resolution(
            participants={key: participant},
            locations={key: location},
            declared_ids={selected_id.root},
            entries=[],
        )
        if not local and self._registry_is_traversable(participant, resolution, selected=True):
            self._resolve_children(
                participant,
                resolution,
                headers_only=True,
                ancestor_ids=frozenset({selected_id.root}),
                ancestor_paths=frozenset({key}),
            )
        return resolution, key

    @staticmethod
    def _anchor_signature(snapshot: FederatedSnapshot) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (
                _path_key(anchor.location.real_root),
                anchor.store_config_revision.root,
                "" if anchor.registry_revision is None else anchor.registry_revision.root,
            )
            for anchor in snapshot.participants
        )

    @staticmethod
    def _snapshot(resolution: _Resolution, selected_key: str) -> FederatedSnapshot:
        snapshots = tuple(
            participant.snapshot
            for participant in sorted(
                (
                    participant
                    for participant in resolution.participants.values()
                    if participant.exposed
                ),
                key=lambda value: _location_sort_key(value.snapshot.location),
            )
        )
        selected_snapshot = resolution.participants[selected_key].snapshot
        anchors = tuple(
            FederationAnchor(
                participant.snapshot.location,
                participant.snapshot.store_config_revision,
                participant.snapshot.registry_revision,
            )
            for participant in sorted(
                resolution.participants.values(),
                key=lambda value: _location_sort_key(value.snapshot.location),
            )
        )
        return FederatedSnapshot(
            selected=selected_snapshot,
            stores=snapshots,
            completeness=Completeness(_sorted_entries(resolution.entries)),
            participants=anchors,
        )

    def _registry_is_traversable(
        self,
        participant: _Participant,
        resolution: _Resolution,
        *,
        selected: bool,
    ) -> bool:
        snapshot = participant.snapshot
        registry = snapshot.registry
        if registry is None:
            resolution.entries.append(
                _incomplete(
                    participant.expected_store_id,
                    reason="invalid",
                    code="ORC005",
                    severity="error",
                    path=_registry_path(snapshot),
                    field="registry",
                    message="registered store registry is missing or invalid",
                    hint="Restore a valid registry.toml and retry federation resolution.",
                )
            )
            return False
        if registry.store_id != participant.expected_store_id:
            resolution.entries.append(
                _incomplete(
                    participant.expected_store_id,
                    reason="identity-mismatch",
                    code="ORC005",
                    severity="error",
                    path=_registry_path(snapshot),
                    field="store_id",
                    message="registry store identity does not match the expected store ID",
                    hint="Restore the immutable registry identity before retrying.",
                )
            )
            return False
        if selected:
            return True
        if snapshot.store is None:
            resolution.entries.append(
                _incomplete(
                    participant.expected_store_id,
                    reason="invalid",
                    code="ORC005",
                    severity="error",
                    path=(snapshot.location.root / "store.toml").as_posix(),
                    field="store",
                    message="registered child store anchor is invalid",
                    hint="Restore a valid store.toml for the expected child store ID.",
                )
            )
        return True

    def _resolve_children(
        self,
        parent: _Participant,
        resolution: _Resolution,
        *,
        headers_only: bool,
        ancestor_ids: frozenset[str],
        ancestor_paths: frozenset[str],
    ) -> None:
        registry = parent.snapshot.registry
        assert registry is not None
        for child in registry.children:
            field = f"children.{child.id.root}"
            if child.id.root in resolution.declared_ids:
                is_cycle = child.id.root in ancestor_ids
                resolution.entries.append(
                    _incomplete(
                        child.id,
                        reason="cycle" if is_cycle else "duplicate",
                        code="ORC005",
                        severity="error",
                        path=_registry_path(parent.snapshot),
                        field=field,
                        message=(
                            "registry ancestor cycle was detected"
                            if is_cycle
                            else "duplicate store ID was registered in the federation"
                        ),
                        hint="Remove duplicate registrations and break the registry cycle.",
                    )
                )
                continue
            candidate = parent.snapshot.location.root.joinpath(*PurePosixPath(child.path).parts)
            try:
                location = self._reader.discover(parent.snapshot.location.root, override=candidate)
            except FileNotFoundError, StoreDiscoveryMissing:
                resolution.entries.append(
                    _incomplete(
                        child.id,
                        reason="missing",
                        code="ORC005",
                        severity="warning",
                        path=_registry_path(parent.snapshot),
                        field=field,
                        message="registered child store is missing or inaccessible",
                        hint="Restore the child store at its registered path or remove the entry.",
                    )
                )
                resolution.declared_ids.add(child.id.root)
                continue
            except StoreDiscoveryInvalid:
                resolution.entries.append(
                    _incomplete(
                        child.id,
                        reason="invalid",
                        code="ORC005",
                        severity="error",
                        path=_registry_path(parent.snapshot),
                        field=field,
                        message="registered child store path is invalid",
                        hint="Repair the child registry path before retrying.",
                    )
                )
                resolution.declared_ids.add(child.id.root)
                continue
            except DiagnosticError:
                raise
            if self._path_is_duplicate(
                location,
                child.id,
                parent,
                resolution,
                ancestor_paths=ancestor_paths,
            ):
                resolution.declared_ids.add(child.id.root)
                continue
            resolution.declared_ids.add(child.id.root)
            self._load_child(
                location,
                child.id,
                parent,
                resolution,
                headers_only=headers_only,
                ancestor_ids=ancestor_ids | {child.id.root},
                ancestor_paths=ancestor_paths | {_path_key(location.real_root)},
            )

    def _path_is_duplicate(
        self,
        location: StoreLocation,
        expected_store_id: StoreId,
        parent: _Participant,
        resolution: _Resolution,
        *,
        ancestor_paths: frozenset[str],
    ) -> bool:
        key = _path_key(location.real_root)
        existing = resolution.locations.get(key)
        if existing is None:
            resolution.locations[key] = location
            return False
        if key in ancestor_paths:
            reason: IncompletenessReason = "cycle"
            message = "registry ancestor cycle through a normalized store path was detected"
        elif existing.real_root == location.real_root:
            reason = "duplicate"
            message = "duplicate normalized store path was registered in the federation"
        else:
            reason = "duplicate"
            message = "case-folding store path alias conflicts with another registered store"
        resolution.entries.append(
            _incomplete(
                expected_store_id,
                reason=reason,
                code="ORC005",
                severity="error",
                path=_registry_path(parent.snapshot),
                field=f"children.{expected_store_id.root}",
                message=message,
                hint="Use one immutable ID and one normalized real path per registered store.",
            )
        )
        return True

    def _load_child(
        self,
        location: StoreLocation,
        expected_store_id: StoreId,
        parent: _Participant,
        resolution: _Resolution,
        *,
        headers_only: bool,
        ancestor_ids: frozenset[str],
        ancestor_paths: frozenset[str],
    ) -> None:
        try:
            snapshot = self._reader.load_local(location, headers_only=headers_only)
        except FileNotFoundError, StoreDiscoveryMissing:
            resolution.entries.append(
                _incomplete(
                    expected_store_id,
                    reason="missing",
                    code="ORC005",
                    severity="warning",
                    path=_registry_path(parent.snapshot),
                    field=f"children.{expected_store_id.root}",
                    message="registered child store became inaccessible while loading",
                    hint="Restore the child store and retry federation resolution.",
                )
            )
            return
        except DiagnosticError:
            raise

        participant = _Participant(expected_store_id, snapshot, exposed=False)
        key = _path_key(location.real_root)
        resolution.participants[key] = participant
        if snapshot.store is not None and snapshot.store.id != expected_store_id:
            resolution.entries.append(
                _incomplete(
                    expected_store_id,
                    reason="identity-mismatch",
                    code="ORC005",
                    severity="error",
                    path=(location.root / "store.toml").as_posix(),
                    field="id",
                    message=(
                        "registered child store ID does not match the registry-declared expected ID"
                    ),
                    hint="Point the registry entry at the store with the expected immutable ID.",
                )
            )
            return
        if self._registry_is_traversable(participant, resolution, selected=False):
            participant.exposed = parent.exposed and snapshot.store is not None
            self._resolve_children(
                participant,
                resolution,
                headers_only=headers_only,
                ancestor_ids=ancestor_ids,
                ancestor_paths=ancestor_paths,
            )

    def _reread_under_lock(
        self,
        resolution: _Resolution,
        *,
        headers_only: bool,
    ) -> None:
        participants = sorted(
            resolution.participants.values(),
            key=lambda value: _location_sort_key(value.snapshot.location),
        )
        for participant in participants:
            optimistic = participant.snapshot
            try:
                current_location = self._reader.discover(
                    optimistic.location.root,
                    override=optimistic.location.root,
                )
            except FileNotFoundError, StoreDiscoveryMissing, StoreDiscoveryInvalid:
                self._record_change(
                    participant,
                    resolution,
                    "store path or anchor changed during federation resolution",
                )
                continue
            except DiagnosticError:
                raise
            if current_location.real_root != optimistic.location.real_root:
                self._record_change(
                    participant,
                    resolution,
                    "store path changed during federation resolution",
                )
                continue
            try:
                current = self._reader.load_local(current_location, headers_only=headers_only)
            except FileNotFoundError, StoreDiscoveryMissing:
                self._record_change(
                    participant,
                    resolution,
                    "store anchor or registry changed during federation resolution",
                )
                continue
            except DiagnosticError:
                raise
            if self._anchors_changed(optimistic, current):
                self._record_change(
                    participant,
                    resolution,
                    "store anchor or registry changed during federation resolution",
                )
                continue
            participant.snapshot = current

    @staticmethod
    def _anchors_changed(optimistic: StoreSnapshot, current: StoreSnapshot) -> bool:
        return (
            optimistic.store_config_revision != current.store_config_revision
            or optimistic.registry_revision != current.registry_revision
        )

    @staticmethod
    def _record_change(
        participant: _Participant,
        resolution: _Resolution,
        message: str,
    ) -> None:
        resolution.entries.append(
            _incomplete(
                participant.expected_store_id,
                reason="changed",
                code="ORC007",
                severity="error",
                path=_registry_path(participant.snapshot),
                field="revision",
                message=message,
                hint="Retry from a freshly resolved federation snapshot.",
            )
        )

    @staticmethod
    def _record_timeout(error: StoreLockTimeout, resolution: _Resolution) -> None:
        participant = resolution.participants.get(_path_key(error.location.real_root))
        if participant is None:
            raise error
        resolution.entries.append(
            _incomplete(
                participant.expected_store_id,
                reason="timeout",
                code="ORC007",
                severity="error",
                path=(error.location.root / ".lock").as_posix(),
                field="lock",
                message="timed out acquiring the registered store lock",
                hint="Retry after the conflicting store operation completes.",
            )
        )


class _OverlayReader:
    def __init__(self, reader: StoreReader, parent: StoreLocation, snapshot: StoreSnapshot) -> None:
        self._reader = reader
        self._parent = _path_key(parent.real_root)
        self._snapshot = snapshot

    def discover(self, start: Path, override: Path | None = None) -> StoreLocation:
        return self._reader.discover(start, override)

    def load_local(self, location: StoreLocation, *, headers_only: bool) -> StoreSnapshot:
        if _path_key(location.real_root) == self._parent:
            return self._snapshot
        return self._reader.load_local(location, headers_only=headers_only)

    def read_raw(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord:
        return self._reader.read_raw(location, relative_path)

    def read_file(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord:
        return self._reader.read_file(location, relative_path)

    def read_item_body(self, location: StoreLocation, relative_path: PurePosixPath) -> bytes:
        return self._reader.read_item_body(location, relative_path)

    def list_entries(self, location: StoreLocation) -> tuple[StoreEntry, ...]:
        return self._reader.list_entries(location)

    def inspect_administrative(self, location: StoreLocation) -> AdministrativeState:
        return self._reader.inspect_administrative(location)


class FederationRegistryService:
    """Single-parent registry writes with optimistic, globally ordered federation locks."""

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

    def list_children(self, request: ListChildrenRequest) -> ListChildrenResult:
        if not 1 <= request.limit <= 200:
            raise ValueError("limit must be in range 1..200")
        snapshot = self._reader.load_local(request.location, headers_only=True)
        if snapshot.registry is None or snapshot.registry_revision is None:
            raise RegistryMutationConflict("selected registry is invalid")
        rows = tuple(RegistryChildRow(value.id, value.path) for value in snapshot.registry.children)
        return ListChildrenResult(
            rows[: request.limit],
            snapshot.registry_revision,
            len(rows) > request.limit,
        )

    def add_child(self, request: AddChildRequest) -> MutationReceipt:
        child_id = (
            request.child_id if isinstance(request.child_id, StoreId) else StoreId(request.child_id)
        )
        try:
            child = RegistryChild(id=child_id, path=request.path)
        except ValidationError as error:
            raise RegistryPathConflict(error) from error
        current = self._optimistic(request.location)
        selected = current.selected
        if selected.registry is None:
            raise RegistryMutationConflict("selected registry is invalid")
        if any(value.id == child_id for value in selected.registry.children):
            raise RegistryMutationConflict("child store ID is already registered")
        child_location = self._reader.discover(
            request.location.root,
            override=request.location.root.joinpath(*PurePosixPath(child.path).parts),
        )
        proposed = self._optimistic(child_location)
        if (
            proposed.selected.store is None
            or proposed.selected.store.id != child_id
            or not proposed.completeness.complete
        ):
            raise RegistryMutationConflict(
                "proposed child subtree is not complete with expected identity"
            )
        registry = Registry(
            schema=selected.registry.schema_,
            store_id=selected.registry.store_id,
            children=(*selected.registry.children, child),
        )
        return self._mutate(request, current, proposed, registry)

    def remove_child(self, request: RemoveChildRequest) -> MutationReceipt:
        child_id = (
            request.child_id if isinstance(request.child_id, StoreId) else StoreId(request.child_id)
        )
        current = self._optimistic(request.location)
        selected = current.selected
        if selected.registry is None:
            raise RegistryMutationConflict("selected registry is invalid")
        children = tuple(value for value in selected.registry.children if value.id != child_id)
        if len(children) == len(selected.registry.children):
            raise RegistryMutationConflict("child store ID is not registered")
        registry = Registry(
            schema=selected.registry.schema_,
            store_id=selected.registry.store_id,
            children=children,
        )
        return self._mutate(request, current, None, registry)

    def _optimistic(self, location: StoreLocation) -> FederatedSnapshot:
        return FederationService(self._reader, _NoopLocks()).load(
            location, local=False, headers_only=True
        )

    def _reread_locked(
        self,
        locations: tuple[StoreLocation, ...],
        optimistic: dict[str, tuple[Revision, Revision | None]],
    ) -> None:
        for location in locations:
            try:
                rediscovered = self._reader.discover(location.root, override=location.root)
                if rediscovered.real_root != location.real_root:
                    raise RegistryRevisionConflict(
                        "a participating store path changed during registry mutation"
                    )
                present = self._reader.load_local(rediscovered, headers_only=True)
            except RegistryRevisionConflict:
                raise
            except (OSError, ValueError) as error:
                raise RegistryRevisionConflict(
                    "a participating path, anchor, or registry changed during mutation"
                ) from error
            if optimistic[_path_key(location.real_root)] != (
                present.store_config_revision,
                present.registry_revision,
            ):
                raise RegistryRevisionConflict(
                    "a participating anchor or registry changed during registry mutation"
                )

    def _mutate(
        self,
        request: AddChildRequest | RemoveChildRequest,
        current: FederatedSnapshot,
        proposed: FederatedSnapshot | None,
        registry: Registry,
    ) -> MutationReceipt:
        by_path = {
            _path_key(value.location.real_root): value.location for value in current.participants
        }
        if proposed is not None:
            by_path.update(
                {
                    _path_key(value.location.real_root): value.location
                    for value in proposed.participants
                }
            )
        locations = tuple(sorted(by_path.values(), key=_location_sort_key))
        optimistic = {
            _path_key(store.location.real_root): (
                store.store_config_revision,
                store.registry_revision,
            )
            for federation in (current, proposed)
            if federation is not None
            for store in federation.participants
        }
        with self._locks.acquire(locations, timeout=self._lock_timeout):
            self._reread_locked(locations, optimistic)
            try:
                parent = self._reader.load_local(request.location, headers_only=True)
            except (OSError, ValueError) as error:
                raise RegistryRevisionConflict(
                    "selected registry changed during mutation"
                ) from error
            if (
                not request.force_current
                and parent.registry_revision != request.expected_registry_revision
            ):
                raise RegistryRevisionConflict("registry revision guard does not match exact bytes")
            raw = self._formatter.registry_bytes(registry)
            revision = Revision(f"sha256:{hashlib.sha256(raw).hexdigest()}")
            projected_parent = parent.__class__(
                parent.location,
                parent.store,
                registry,
                parent.records,
                parent.load_diagnostics,
                parent.raw_index,
                parent.store_revision,
                revision,
                parent.store_config_revision,
            )
            overlay = _OverlayReader(self._reader, request.location, projected_parent)
            try:
                final = FederationService(overlay, _NoopLocks()).load(
                    request.location, local=False, headers_only=True
                )
            except (OSError, ValueError) as error:
                raise RegistryRevisionConflict(
                    "registry graph changed while validating the locked union"
                ) from error
            final_paths = {_path_key(value.location.real_root) for value in final.stores}
            if not final_paths <= set(by_path):
                raise RegistryRevisionConflict(
                    "registry graph discovered a path outside the locked union"
                )
            if not final.completeness.complete:
                raise RegistryMutationConflict("final registry graph is incomplete")
            diagnostics = validate_snapshot(final, require_children=True)
            if any(value.severity == "error" for value in diagnostics):
                raise RegistryMutationConflict("final registry graph is invalid")
            registry_path = PurePosixPath("registry.toml")
            self._writer.replace(request.location, FileReplacement(registry_path, raw))
            after = self._reader.load_local(request.location, headers_only=False)
            view_state = apply_views(
                self._reader,
                self._writer,
                request.location,
                self._views,
                after,
            )
            return MutationReceipt(
                applied=True,
                replayed=False,
                canonical_applied=True,
                views_current=view_state.current,
                intended_paths=(registry_path, *view_state.intended_paths),
                changed_paths=(registry_path, *view_state.changed_paths),
                item_revisions=tuple(
                    ItemRevision(value.path, value.revision) for value in after.records
                ),
                store_revision=after.store_revision,
                registry_revision=after.registry_revision,
            )
