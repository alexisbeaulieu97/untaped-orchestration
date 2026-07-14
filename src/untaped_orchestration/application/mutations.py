from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from untaped_orchestration.application.ports import (
    FileDeletion,
    FileReplacement,
    LockManager,
    MutationProjector,
    StoreLocation,
    StoreReader,
    StoreWriter,
    ViewRenderer,
)
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    IncompleteStore,
    ItemRevision,
    MutationReceipt,
    ProjectedMutation,
)
from untaped_orchestration.application.scaffold import (
    inspect_store_shape,
    validate_store_shape,
)
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.application.view_management import apply_views
from untaped_orchestration.domain.diagnostics import (
    Diagnostic,
    DiagnosticError,
    expected_diagnostic,
)
from untaped_orchestration.domain.models import LinkRelation

DEFAULT_LOCK_TIMEOUT = 10.0


class InvalidMutationState(DiagnosticError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        super().__init__(diagnostics)


class MutationLockSetError(DiagnosticError):
    def __init__(self, message: str) -> None:
        super().__init__(expected_diagnostic("ORC007", message, field="lock_set"))


@dataclass(frozen=True, slots=True)
class IntendedMutation:
    replacements: tuple[FileReplacement, ...] = ()
    deletions: tuple[FileDeletion, ...] = ()
    replayed: bool = False
    finalize_views: bool = True


type SnapshotLoader = Callable[[], FederatedSnapshot]
type MutationGuard = Callable[[FederatedSnapshot], None]
type MutationBuilder = Callable[[FederatedSnapshot], IntendedMutation]
type SnapshotValidator = Callable[[FederatedSnapshot], tuple[Diagnostic, ...]]


def validate_selected_local(snapshot: FederatedSnapshot) -> tuple[Diagnostic, ...]:
    config = snapshot.selected.store
    selected_id = config.id if config is not None else None
    remote_store_ids = tuple(
        sorted(
            {
                link.target_store_id
                for record in snapshot.selected.records
                for link in record.metadata.links
                if selected_id is not None
                and link.target_store_id != selected_id
                and link.relation in {LinkRelation.GOVERNED_BY, LinkRelation.FOLLOW_UP_TO}
            },
            key=lambda value: value.root,
        )
    )
    incompleteness = tuple(
        IncompleteStore(
            expected_store_id=store_id,
            reason="missing",
            diagnostic=Diagnostic(
                code="ORC005",
                severity="warning",
                path=snapshot.selected.location.root.as_posix(),
                field=f"navigation.{store_id.root}",
                message="remote navigation targets are unresolved in selected-local validation",
                hint="Use recursive validation before a cross-store structural operation.",
            ),
        )
        for store_id in remote_store_ids
    )
    selected_only = FederatedSnapshot(
        snapshot.selected,
        (snapshot.selected,),
        Completeness(incompleteness),
    )
    return validate_snapshot(selected_only, require_children=False)


def _valid_or_raise(
    snapshot: FederatedSnapshot,
    validator: SnapshotValidator,
) -> None:
    diagnostics = validator(snapshot)
    if any(value.severity == "error" for value in diagnostics):
        raise InvalidMutationState(diagnostics)


def _location_key(location: StoreLocation) -> str:
    return os.path.normcase(str(location.real_root)).casefold()


def _validate_lock_set(
    locations: Sequence[StoreLocation],
    selected: StoreLocation,
    current: FederatedSnapshot,
) -> None:
    locked = tuple(_location_key(value) for value in locations)
    resolved = tuple(
        _location_key(value.location)
        for value in (current.participants if current.participants else current.stores)
    )
    selected_key = _location_key(selected)
    if (
        len(set(locked)) != len(locked)
        or set(locked) != set(resolved)
        or len(set(resolved)) != len(resolved)
        or selected_key != _location_key(current.selected.location)
        or selected_key not in set(locked)
    ):
        raise MutationLockSetError(
            "locked locations must exactly match resolved participants "
            "and include the selected store"
        )


class MutationExecutor:
    def __init__(
        self,
        reader: StoreReader,
        writer: StoreWriter,
        locks: LockManager,
        views: ViewRenderer,
        *,
        projector: MutationProjector,
        validator: SnapshotValidator | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        if lock_timeout < 0:
            raise ValueError("lock timeout must be nonnegative")
        self._reader = reader
        self._writer = writer
        self._locks = locks
        self._views = views
        self._projector = projector
        self._validator = validator or (
            lambda snapshot: validate_snapshot(snapshot, require_children=True)
        )
        self._lock_timeout = lock_timeout

    def project(
        self,
        current: FederatedSnapshot,
        replacements: Sequence[FileReplacement],
        deletions: Sequence[FileDeletion],
    ) -> ProjectedMutation:
        return self._projector.project(
            current,
            current.selected.location,
            replacements,
            deletions,
        )

    def execute(
        self,
        *,
        locations: Sequence[StoreLocation],
        selected: StoreLocation,
        load: SnapshotLoader,
        guard: MutationGuard,
        build: MutationBuilder,
        replayed: bool = False,
        validator: SnapshotValidator | None = None,
        dry_run: bool = False,
    ) -> MutationReceipt:
        operation_validator = validator or self._validator
        with self._locks.acquire(locations, timeout=self._lock_timeout):
            current_shape = inspect_store_shape(self._reader, selected)
            if current_shape.diagnostics:
                raise InvalidMutationState(current_shape.diagnostics)
            current = load()
            _valid_or_raise(current, operation_validator)
            _validate_lock_set(locations, selected, current)
            guard(current)
            intended = build(current)
            projection = self._projector.project(
                current,
                selected,
                intended.replacements,
                intended.deletions,
            )
            shape_diagnostics = validate_store_shape(projection.entries, projection.contents)
            if shape_diagnostics:
                raise InvalidMutationState(shape_diagnostics)
            projected = projection.snapshot
            _valid_or_raise(projected, operation_validator)

            if dry_run:
                return MutationReceipt(
                    applied=False,
                    replayed=False,
                    canonical_applied=False,
                    views_current=False,
                    intended_paths=tuple(
                        (
                            *(value.path for value in intended.replacements),
                            *(value.path for value in intended.deletions),
                        )
                    ),
                    changed_paths=(),
                    item_revisions=tuple(
                        ItemRevision(record.path, record.revision)
                        for record in current.selected.records
                    ),
                    store_revision=current.selected.store_revision,
                    registry_revision=current.selected.registry_revision,
                )

            changed = []
            for replacement in intended.replacements:
                self._writer.replace(selected, replacement)
                changed.append(replacement.path)
            for deletion in intended.deletions:
                self._writer.delete(selected, deletion)
                changed.append(deletion.path)

            canonical_applied = bool(intended.replacements or intended.deletions)
            after_shape = inspect_store_shape(self._reader, selected)
            if after_shape.diagnostics:
                raise InvalidMutationState(after_shape.diagnostics)
            selected_after = self._reader.load_local(selected, headers_only=False)
            after = FederatedSnapshot(
                selected_after,
                tuple(
                    selected_after
                    if _location_key(value.location) == _location_key(selected)
                    else value
                    for value in projected.stores
                ),
                projected.completeness,
            )
            _valid_or_raise(after, operation_validator)
            if selected_after != projected.selected:
                raise InvalidMutationState(
                    (
                        Diagnostic(
                            code="ORC007",
                            severity="error",
                            path=selected.root.as_posix(),
                            field="store_revision",
                            message="durable mutation result differs from projected intended state",
                            hint="Stop and inspect exact changed paths before retrying.",
                        ),
                    )
                )

            view_state = apply_views(
                self._reader,
                self._writer,
                selected,
                self._views,
                selected_after,
                write=intended.finalize_views,
            )
            changed.extend(view_state.changed_paths)
            intended_paths = tuple(
                (
                    *(value.path for value in intended.replacements),
                    *(value.path for value in intended.deletions),
                    *view_state.intended_paths,
                )
            )

            return MutationReceipt(
                applied=bool(changed),
                replayed=replayed or intended.replayed,
                canonical_applied=canonical_applied,
                views_current=view_state.current,
                intended_paths=intended_paths,
                changed_paths=tuple(changed),
                item_revisions=tuple(
                    ItemRevision(record.path, record.revision) for record in selected_after.records
                ),
                store_revision=selected_after.store_revision,
                registry_revision=selected_after.registry_revision,
            )
