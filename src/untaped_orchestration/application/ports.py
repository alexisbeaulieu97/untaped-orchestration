from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from untaped_orchestration.application.results import (
    FileDeletion as FileDeletion,
)
from untaped_orchestration.application.results import (
    FileReplacement as FileReplacement,
)
from untaped_orchestration.application.results import (
    LoadedRecord as LoadedRecord,
)
from untaped_orchestration.application.results import (
    RawRecord as RawRecord,
)
from untaped_orchestration.application.results import (
    RawReference as RawReference,
)
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
    "CanonicalFormatter",
    "Clock",
    "FileDeletion",
    "FileReplacement",
    "IdGenerator",
    "LoadedRecord",
    "LockManager",
    "RawRecord",
    "RawReference",
    "StoreLocation",
    "StoreLockTimeout",
    "StoreReader",
    "StoreSnapshot",
    "StoreWriter",
    "ViewRenderer",
]


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self, kind: Literal["store", "task", "decision"]) -> str: ...


class StoreReader(Protocol):
    def discover(self, start: Path, override: Path | None = None) -> StoreLocation: ...

    def load_local(self, location: StoreLocation, *, headers_only: bool) -> StoreSnapshot: ...

    def read_raw(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord: ...

    def read_file(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord: ...

    def list_files(self, location: StoreLocation) -> tuple[PurePosixPath, ...]: ...


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
    def expected(self, snapshot: StoreSnapshot) -> Mapping[PurePosixPath, bytes]: ...


class CanonicalFormatter(Protocol):
    def store_bytes(self, config: StoreConfig) -> bytes: ...

    def registry_bytes(self, registry: Registry) -> bytes: ...

    def item_bytes(self, metadata: CanonicalItem, body: bytes) -> bytes: ...
