from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from untaped_orchestration.domain.canonical import CanonicalItem
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.models import Registry, Revision, StoreConfig


@dataclass(frozen=True, slots=True)
class StoreLocation:
    """A selected store path together with its normalized real-path identity."""

    root: Path
    real_root: Path


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


@dataclass(frozen=True, slots=True)
class FileReplacement:
    path: PurePosixPath
    content: bytes


@dataclass(frozen=True, slots=True)
class FileDeletion:
    path: PurePosixPath
