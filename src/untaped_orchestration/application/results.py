from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from untaped_orchestration.domain.canonical import CanonicalItem
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.ids import StoreId
from untaped_orchestration.domain.models import Registry, Revision, StoreConfig


@dataclass(frozen=True, slots=True)
class StoreLocation:
    """A selected store path together with its normalized real-path identity."""

    root: Path
    real_root: Path


class StoreLockTimeout(TimeoutError):
    def __init__(self, location: StoreLocation) -> None:
        self.location = location
        super().__init__(f"timed out acquiring orchestration store lock: {location.real_root}")


@dataclass(frozen=True, slots=True)
class RawReference:
    path: PurePosixPath
    revision: Revision
    size: int


@dataclass(frozen=True, slots=True)
class RawRecord:
    path: PurePosixPath
    revision: Revision
    size: int
    content: bytes


@dataclass(frozen=True, slots=True)
class LoadedRecord:
    path: PurePosixPath
    revision: Revision
    metadata: CanonicalItem
    body: bytes | None


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    location: StoreLocation
    store: StoreConfig | None
    registry: Registry | None
    records: tuple[LoadedRecord, ...]
    load_diagnostics: tuple[Diagnostic, ...]
    raw_index: tuple[RawReference, ...]
    store_revision: Revision
    registry_revision: Revision | None
    store_config_revision: Revision


type IncompletenessReason = Literal[
    "missing",
    "invalid",
    "identity-mismatch",
    "duplicate",
    "cycle",
    "timeout",
    "changed",
]

type StoreEntryKind = Literal["file", "directory", "symlink", "other"]


@dataclass(frozen=True, slots=True)
class StoreEntry:
    path: PurePosixPath
    kind: StoreEntryKind


@dataclass(frozen=True, slots=True)
class IncompleteStore:
    expected_store_id: StoreId
    reason: IncompletenessReason
    diagnostic: Diagnostic


@dataclass(frozen=True, slots=True)
class Completeness:
    entries: tuple[IncompleteStore, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.entries

    @property
    def missing_store_ids(self) -> tuple[str, ...]:
        return tuple(sorted({entry.expected_store_id.root for entry in self.entries}))

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return tuple(entry.diagnostic for entry in self.entries)


@dataclass(frozen=True, slots=True)
class FederatedSnapshot:
    selected: StoreSnapshot
    stores: tuple[StoreSnapshot, ...]
    completeness: Completeness


@dataclass(frozen=True, slots=True)
class ProjectedMutation:
    snapshot: FederatedSnapshot
    entries: tuple[StoreEntry, ...]
    contents: dict[PurePosixPath, bytes]


@dataclass(frozen=True, slots=True)
class FileReplacement:
    path: PurePosixPath
    content: bytes


@dataclass(frozen=True, slots=True)
class FileDeletion:
    path: PurePosixPath


@dataclass(frozen=True, slots=True)
class ItemRevision:
    path: PurePosixPath
    revision: Revision


@dataclass(frozen=True, slots=True)
class PathComparison:
    path: PurePosixPath
    matches: bool


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    applied: bool
    replayed: bool
    canonical_applied: bool
    views_current: bool
    intended_paths: tuple[PurePosixPath, ...]
    changed_paths: tuple[PurePosixPath, ...]
    item_revisions: tuple[ItemRevision, ...]
    store_revision: Revision
    registry_revision: Revision | None


@dataclass(frozen=True, slots=True)
class CheckResult:
    store_id: str
    store_revision: Revision
    registry_revision: Revision | None
    valid: bool
    views_current: bool
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    receipt: MutationReceipt
    comparisons: tuple[PathComparison, ...]
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def matches(self) -> bool:
        return all(value.matches for value in self.comparisons)

    @property
    def applied(self) -> bool:
        return self.receipt.applied

    @property
    def replayed(self) -> bool:
        return self.receipt.replayed

    @property
    def canonical_applied(self) -> bool:
        return self.receipt.canonical_applied

    @property
    def views_current(self) -> bool:
        return self.receipt.views_current

    @property
    def intended_paths(self) -> tuple[PurePosixPath, ...]:
        return self.receipt.intended_paths

    @property
    def changed_paths(self) -> tuple[PurePosixPath, ...]:
        return self.receipt.changed_paths

    @property
    def store_revision(self) -> Revision:
        return self.receipt.store_revision

    @property
    def registry_revision(self) -> Revision | None:
        return self.receipt.registry_revision
