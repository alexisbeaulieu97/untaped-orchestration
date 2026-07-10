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
class FileReplacement:
    path: PurePosixPath
    content: bytes


@dataclass(frozen=True, slots=True)
class FileDeletion:
    path: PurePosixPath
