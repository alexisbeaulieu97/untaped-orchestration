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
    StoreSnapshot as StoreSnapshot,
)

__all__ = [
    "Clock",
    "FileDeletion",
    "FileReplacement",
    "IdGenerator",
    "LoadedRecord",
    "LockManager",
    "RawRecord",
    "RawReference",
    "StoreLocation",
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


class StoreWriter(Protocol):
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
