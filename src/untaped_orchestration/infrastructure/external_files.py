from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from untaped_orchestration.domain.diagnostics import (
    DiagnosticCode,
    DiagnosticError,
    expected_diagnostic,
)
from untaped_orchestration.domain.limits import BODY_LIMIT, FRONTMATTER_LIMIT

type ExternalReadEvent = str
type ExternalReadHook = Callable[[ExternalReadEvent, Path], None]
type FileIdentity = tuple[int, int, int]


class ExternalFileReadError(DiagnosticError):
    pass


def _identity(value: os.stat_result) -> FileIdentity:
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


def _failure(
    path: Path,
    field: str,
    code: DiagnosticCode,
    message: str,
) -> ExternalFileReadError:
    return ExternalFileReadError(
        expected_diagnostic(
            code,
            message,
            path=path.as_posix(),
            field=field,
        )
    )


def _unsafe(path: Path, field: str, message: str) -> ExternalFileReadError:
    return _failure(path, field, "ORC003", message)


def _bounded(path: Path, field: str, limit: int) -> ExternalFileReadError:
    label = (
        "the 1 MiB limit"
        if limit == BODY_LIMIT
        else "the 64 KiB limit"
        if limit == FRONTMATTER_LIMIT
        else f"the {limit}-byte limit"
    )
    return _failure(path, field, "ORC001", f"external file exceeds {label}")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _components(path: Path) -> tuple[Path, ...]:
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    values = [current]
    for part in absolute.parts[1:]:
        current /= part
        values.append(current)
    return tuple(values)


def _read_descriptor(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _verify_descriptor(
    before: os.stat_result,
    after: os.stat_result,
    path: Path,
    field: str,
    limit: int,
) -> None:
    if not stat.S_ISREG(after.st_mode) or _identity(after) != _identity(before):
        raise _unsafe(path, field, "external path changed while being read")
    if after.st_size > limit:
        raise _bounded(path, field, limit)


class FilesystemExternalFileReader:
    """Read one bounded immutable snapshot without following path components.

    The fallback is for platforms without descriptor-relative ``O_NOFOLLOW``.
    It relies on cooperative writers and rejects inode/component substitutions
    detected between the pre-open and post-read checks.
    """

    def __init__(
        self,
        *,
        force_fallback: bool = False,
        event_hook: ExternalReadHook | None = None,
    ) -> None:
        self._force_fallback = force_fallback
        self._event_hook = event_hook or (lambda _event, _path: None)

    @staticmethod
    def _supports_no_follow() -> bool:
        return (
            hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY")
            and os.open in os.supports_dir_fd
        )

    def read_external(self, path: Path, *, limit: int, field: str) -> bytes:
        if limit < 0:
            raise ValueError("external file limit must be nonnegative")
        absolute = _absolute(path)
        try:
            raw = (
                self._read_no_follow(absolute, limit, field)
                if self._supports_no_follow() and not self._force_fallback
                else self._read_cooperative(absolute, limit, field)
            )
        except ExternalFileReadError:
            raise
        except OSError as error:
            raise _unsafe(absolute, field, "external path is unsafe or unreadable") from error
        if len(raw) > limit:
            raise _bounded(absolute, field, limit)
        return raw

    def _read_no_follow(self, path: Path, limit: int, field: str) -> bytes:
        components = _components(path)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        opened: list[int] = []
        identities: list[tuple[Path, FileIdentity]] = []
        try:
            directory = os.open(components[0], directory_flags)
            opened.append(directory)
            identities.append((components[0], _identity(os.fstat(directory))))
            for component in components[1:-1]:
                child = os.open(component.name, directory_flags, dir_fd=directory)
                opened.append(child)
                directory = child
                identities.append((component, _identity(os.fstat(child))))
            descriptor = os.open(components[-1].name, file_flags, dir_fd=directory)
            opened.append(descriptor)
            final_stat = os.fstat(descriptor)
            if not stat.S_ISREG(final_stat.st_mode):
                raise _unsafe(path, field, "external path must name a regular file")
            identities.append((components[-1], _identity(final_stat)))
            self._event_hook("after-stat", path)
            self._event_hook("after-open", path)
            raw = _read_descriptor(descriptor, limit)
            self._event_hook("after-read", path)
            _verify_descriptor(final_stat, os.fstat(descriptor), path, field, limit)
            self._verify_components(identities, path, field)
            return raw
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    def _read_cooperative(self, path: Path, limit: int, field: str) -> bytes:
        components = _components(path)
        identities: list[tuple[Path, FileIdentity]] = []
        for index, component in enumerate(components):
            value = os.lstat(component)
            if stat.S_ISLNK(value.st_mode):
                raise _unsafe(path, field, "external path must not contain symlinks")
            if index < len(components) - 1 and not stat.S_ISDIR(value.st_mode):
                raise _unsafe(path, field, "external path parent must be a directory")
            identities.append((component, _identity(value)))
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise _unsafe(path, field, "external path must name a regular file")
        self._event_hook("after-stat", path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            descriptor_stat = os.fstat(descriptor)
            if _identity(descriptor_stat) != identities[-1][1]:
                raise _unsafe(path, field, "external path changed while being read")
            self._event_hook("after-open", path)
            raw = _read_descriptor(descriptor, limit)
            self._event_hook("after-read", path)
            _verify_descriptor(descriptor_stat, os.fstat(descriptor), path, field, limit)
            self._verify_components(identities, path, field)
            return raw
        finally:
            os.close(descriptor)

    @staticmethod
    def _verify_components(
        identities: list[tuple[Path, FileIdentity]],
        path: Path,
        field: str,
    ) -> None:
        try:
            unchanged = all(
                _identity(os.lstat(component)) == identity for component, identity in identities
            )
        except OSError:
            unchanged = False
        if not unchanged:
            raise _unsafe(path, field, "external path changed while being read")
