from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from untaped_orchestration.application.ports import LockManager, StoreLockTimeout, StoreReader
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    IncompletenessReason,
    IncompleteStore,
    StoreLocation,
    StoreSnapshot,
)
from untaped_orchestration.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticSeverity,
    diagnostic_sort_key,
)
from untaped_orchestration.domain.ids import StoreId

DEFAULT_LOCK_TIMEOUT = 10.0


class UnidentifiedStoreError(ValueError):
    """The selected snapshot has no truthful immutable identity."""

    def __init__(self, location: StoreLocation) -> None:
        self.location = location
        super().__init__(f"selected store identity is unreadable: {location.root}")


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
        selected = self._reader.load_local(location, headers_only=headers_only)
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
                headers_only=headers_only,
                ancestor_ids=frozenset({selected_id.root}),
                ancestor_paths=frozenset({key}),
            )

        ordered = sorted(
            (value.snapshot.location for value in resolution.participants.values()),
            key=_location_sort_key,
        )
        try:
            with self._locks.acquire(ordered, timeout=self._lock_timeout):
                self._reread_under_lock(
                    resolution,
                    headers_only=headers_only,
                )
        except StoreLockTimeout as error:
            self._record_timeout(error, resolution)

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
        selected_snapshot = resolution.participants[key].snapshot
        return FederatedSnapshot(
            selected=selected_snapshot,
            stores=snapshots,
            completeness=Completeness(_sorted_entries(resolution.entries)),
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
            except OSError as error:
                resolution.entries.append(
                    _incomplete(
                        child.id,
                        reason="missing",
                        code="ORC005",
                        severity="warning",
                        path=_registry_path(parent.snapshot),
                        field=field,
                        message=f"registered child store is missing or inaccessible: {error}",
                        hint="Restore the child store at its registered path or remove the entry.",
                    )
                )
                resolution.declared_ids.add(child.id.root)
                continue
            except ValueError as error:
                resolution.entries.append(
                    _incomplete(
                        child.id,
                        reason="invalid",
                        code="ORC005",
                        severity="error",
                        path=_registry_path(parent.snapshot),
                        field=field,
                        message=f"registered child store path is invalid: {error}",
                        hint="Repair the child registry path before retrying.",
                    )
                )
                resolution.declared_ids.add(child.id.root)
                continue
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
        except OSError as error:
            resolution.entries.append(
                _incomplete(
                    expected_store_id,
                    reason="missing",
                    code="ORC005",
                    severity="warning",
                    path=_registry_path(parent.snapshot),
                    field=f"children.{expected_store_id.root}",
                    message=f"registered child store became inaccessible while loading: {error}",
                    hint="Restore the child store and retry federation resolution.",
                )
            )
            return
        except ValueError as error:
            resolution.entries.append(
                _incomplete(
                    expected_store_id,
                    reason="invalid",
                    code="ORC005",
                    severity="error",
                    path=_registry_path(parent.snapshot),
                    field=f"children.{expected_store_id.root}",
                    message=f"registered child store could not be loaded: {error}",
                    hint="Repair the child store filesystem and retry.",
                )
            )
            return

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
            except (OSError, ValueError) as error:
                self._record_change(
                    participant,
                    resolution,
                    f"store path or anchor changed during federation resolution: {error}",
                )
                continue
            if current_location.real_root != optimistic.location.real_root:
                self._record_change(
                    participant,
                    resolution,
                    "store path changed during federation resolution",
                )
                continue
            try:
                current = self._reader.load_local(current_location, headers_only=headers_only)
            except (OSError, ValueError) as error:
                self._record_change(
                    participant,
                    resolution,
                    f"store anchor or registry changed during federation resolution: {error}",
                )
                continue
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
