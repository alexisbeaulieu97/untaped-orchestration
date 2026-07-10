from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from untaped_orchestration.application.results import FederatedSnapshot as FederatedSnapshot
from untaped_orchestration.application.results import (
    FileDeletion as FileDeletion,
)
from untaped_orchestration.application.results import (
    FileReplacement as FileReplacement,
)
from untaped_orchestration.application.results import (
    LoadedRecord as LoadedRecord,
)
from untaped_orchestration.application.results import ProjectedMutation as ProjectedMutation
from untaped_orchestration.application.results import (
    RawRecord as RawRecord,
)
from untaped_orchestration.application.results import (
    RawReference as RawReference,
)
from untaped_orchestration.application.results import StoreEntry as StoreEntry
from untaped_orchestration.application.results import (
    StoreLocation as StoreLocation,
)
from untaped_orchestration.application.results import (
    StoreLockTimeout as StoreLockTimeout,
)
from untaped_orchestration.application.results import (
    StoreSnapshot as StoreSnapshot,
)
from untaped_orchestration.domain.canonical import CanonicalItem
from untaped_orchestration.domain.models import Registry, StoreConfig

__all__ = [
    "MANAGED_VIEW_PATHS",
    "CanonicalFormatter",
    "Clock",
    "FileDeletion",
    "FileReplacement",
    "IdGenerator",
    "LoadedRecord",
    "LockManager",
    "MutationProjector",
    "ProjectedMutation",
    "RawRecord",
    "RawReference",
    "StoreEntry",
    "StoreLocation",
    "StoreLockTimeout",
    "StoreReader",
    "StoreSnapshot",
    "StoreWriter",
    "ViewRenderer",
]

MANAGED_VIEW_PATHS = (
    PurePosixPath("views/roadmap.md"),
    PurePosixPath("views/backlog.md"),
    PurePosixPath("views/inbox.md"),
    PurePosixPath("views/decisions.md"),
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self, kind: Literal["store", "task", "decision"]) -> str: ...


class StoreReader(Protocol):
    def discover(self, start: Path, override: Path | None = None) -> StoreLocation: ...

    def load_local(self, location: StoreLocation, *, headers_only: bool) -> StoreSnapshot: ...

    def read_raw(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord: ...

    def read_file(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord: ...

    def list_entries(self, location: StoreLocation) -> tuple[StoreEntry, ...]: ...


class StoreWriter(Protocol):
    def prepare(self, root: Path) -> StoreLocation: ...

    def replace(self, location: StoreLocation, change: FileReplacement) -> None: ...

    def delete(self, location: StoreLocation, change: FileDeletion) -> None: ...


class LockManager(Protocol):
    def acquire(
        self,
        locations: Sequence[StoreLocation],
        *,
        timeout: float,
    ) -> AbstractContextManager[None]: ...


class ViewRenderer(Protocol):
    def managed_paths(self) -> tuple[PurePosixPath, ...]: ...

    def expected(
        self, snapshot: StoreSnapshot | FederatedSnapshot
    ) -> Mapping[PurePosixPath, bytes]: ...


class MutationProjector(Protocol):
    def project(
        self,
        current: FederatedSnapshot,
        selected: StoreLocation,
        replacements: Sequence[FileReplacement],
        deletions: Sequence[FileDeletion],
    ) -> ProjectedMutation: ...


class CanonicalFormatter(Protocol):
    def store_bytes(self, config: StoreConfig) -> bytes: ...

    def registry_bytes(self, registry: Registry) -> bytes: ...

    def item_bytes(self, metadata: CanonicalItem, body: bytes) -> bytes: ...
