from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager

from filelock import FileLock, Timeout

from untaped_orchestration.application.ports import LockManager, StoreLocation, StoreLockTimeout
from untaped_orchestration.infrastructure.filesystem import (
    PathSafetyError,
    normalized_real_path_key,
)


class FileLockManager(LockManager):
    def ordered(self, locations: Sequence[StoreLocation]) -> tuple[StoreLocation, ...]:
        by_key: dict[str, StoreLocation] = {}
        for location in locations:
            key = normalized_real_path_key(location)
            previous = by_key.get(key)
            if previous is not None and previous.real_root != location.real_root:
                raise PathSafetyError(
                    location.real_root,
                    f"case-folding store path alias conflicts with {previous.real_root}",
                )
            by_key[key] = location
        return tuple(
            sorted(
                by_key.values(),
                key=lambda location: (
                    normalized_real_path_key(location),
                    str(location.real_root),
                ),
            )
        )

    @contextmanager
    def acquire(
        self,
        locations: Sequence[StoreLocation],
        *,
        timeout: float,
    ) -> Iterator[None]:
        if timeout < 0:
            raise ValueError("lock timeout must be nonnegative")
        deadline = time.monotonic() + timeout
        with ExitStack() as stack:
            for location in self.ordered(locations):
                lock_path = location.real_root / ".lock"
                if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
                    raise PathSafetyError(
                        lock_path,
                        "store lock must be absent or a regular nonsymlink file",
                    )
                lock = FileLock(lock_path)
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    lock.acquire(timeout=remaining)
                except Timeout as error:
                    raise StoreLockTimeout(location) from error
                stack.callback(lock.release)
            yield
