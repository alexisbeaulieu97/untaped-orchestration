from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from untaped_orchestration.application.ports import (
    FileDeletion,
    FileReplacement,
    LockManager,
    StoreLocation,
    StoreReader,
    StoreWriter,
    ViewRenderer,
)
from untaped_orchestration.application.results import (
    FederatedSnapshot,
    ItemRevision,
    MutationReceipt,
)
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.domain.diagnostics import Diagnostic

DEFAULT_LOCK_TIMEOUT = 10.0


class InvalidMutationState(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("mutation requires a valid complete intended store state")


@dataclass(frozen=True, slots=True)
class IntendedMutation:
    snapshot: FederatedSnapshot
    replacements: tuple[FileReplacement, ...] = ()
    deletions: tuple[FileDeletion, ...] = ()


type SnapshotLoader = Callable[[], FederatedSnapshot]
type MutationGuard = Callable[[FederatedSnapshot], None]
type MutationBuilder = Callable[[FederatedSnapshot], IntendedMutation]
type SnapshotValidator = Callable[[FederatedSnapshot], tuple[Diagnostic, ...]]


def _valid_or_raise(
    snapshot: FederatedSnapshot,
    validator: SnapshotValidator,
) -> None:
    diagnostics = validator(snapshot)
    if any(value.severity == "error" for value in diagnostics):
        raise InvalidMutationState(diagnostics)


class MutationExecutor:
    def __init__(
        self,
        reader: StoreReader,
        writer: StoreWriter,
        locks: LockManager,
        views: ViewRenderer,
        *,
        validator: SnapshotValidator | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        if lock_timeout < 0:
            raise ValueError("lock timeout must be nonnegative")
        self._reader = reader
        self._writer = writer
        self._locks = locks
        self._views = views
        self._validator = validator or (
            lambda snapshot: validate_snapshot(snapshot, require_children=True)
        )
        self._lock_timeout = lock_timeout

    def execute(
        self,
        *,
        locations: Sequence[StoreLocation],
        selected: StoreLocation,
        load: SnapshotLoader,
        guard: MutationGuard,
        build: MutationBuilder,
        replayed: bool = False,
    ) -> MutationReceipt:
        with self._locks.acquire(locations, timeout=self._lock_timeout):
            current = load()
            _valid_or_raise(current, self._validator)
            guard(current)
            intended = build(current)
            _valid_or_raise(intended.snapshot, self._validator)

            changed = []
            for replacement in intended.replacements:
                self._writer.replace(selected, replacement)
                changed.append(replacement.path)
            for deletion in intended.deletions:
                self._writer.delete(selected, deletion)
                changed.append(deletion.path)

            canonical_applied = bool(intended.replacements or intended.deletions)
            selected_after = self._reader.load_local(selected, headers_only=False)
            views_current = True
            try:
                expected_views = self._views.expected(selected_after)
                for path, content in expected_views.items():
                    try:
                        matches = self._reader.read_file(selected, path).content == content
                    except AttributeError, FileNotFoundError:
                        matches = False
                    if matches:
                        continue
                    self._writer.replace(selected, FileReplacement(path, content))
                    changed.append(path)
            except OSError, ValueError:
                views_current = False
                expected_views = {}

            intended_paths = tuple(
                (
                    *(value.path for value in intended.replacements),
                    *(value.path for value in intended.deletions),
                    *expected_views,
                )
            )

            return MutationReceipt(
                applied=bool(changed),
                replayed=replayed,
                canonical_applied=canonical_applied,
                views_current=views_current,
                intended_paths=intended_paths,
                changed_paths=tuple(changed),
                item_revisions=tuple(
                    ItemRevision(record.path, record.revision) for record in selected_after.records
                ),
                store_revision=selected_after.store_revision,
                registry_revision=selected_after.registry_revision,
            )
