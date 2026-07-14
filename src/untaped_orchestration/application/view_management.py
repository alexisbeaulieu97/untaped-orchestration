from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from untaped_orchestration.application.ports import (
    FileDeletion,
    FileReplacement,
    StoreLocation,
    StoreReader,
    StoreSnapshot,
    StoreWriter,
    ViewRenderer,
)
from untaped_orchestration.application.results import PathComparison
from untaped_orchestration.domain.diagnostics import DiagnosticError


@dataclass(frozen=True, slots=True)
class ViewState:
    intended_paths: tuple[PurePosixPath, ...]
    changed_paths: tuple[PurePosixPath, ...]
    comparisons: tuple[PathComparison, ...]
    current: bool


def _content(reader: StoreReader, location: StoreLocation, path: PurePosixPath) -> bytes | None:
    try:
        return reader.read_file(location, path).content
    except FileNotFoundError:
        return None


def view_comparisons(
    reader: StoreReader,
    location: StoreLocation,
    renderer: ViewRenderer,
    snapshot: StoreSnapshot,
) -> tuple[dict[PurePosixPath, bytes], dict[PurePosixPath, bool]]:
    expected = dict(renderer.expected(snapshot))
    comparisons = {
        path: (
            _content(reader, location, path) == expected[path]
            if path in expected
            else _content(reader, location, path) is None
        )
        for path in renderer.managed_paths()
    }
    return expected, comparisons


def apply_views(
    reader: StoreReader,
    writer: StoreWriter,
    location: StoreLocation,
    renderer: ViewRenderer,
    snapshot: StoreSnapshot,
    *,
    write: bool = True,
) -> ViewState:
    managed = renderer.managed_paths()
    try:
        expected, before = view_comparisons(reader, location, renderer, snapshot)
    except DiagnosticError:
        raise
    except OSError, ValueError:
        return ViewState(
            () if not write else managed,
            (),
            tuple(PathComparison(path, False) for path in managed),
            False,
        )
    intended = tuple(path for path in managed if path in expected or not before[path])
    if not write:
        comparisons = tuple(PathComparison(path, before[path]) for path in managed)
        return ViewState((), (), comparisons, all(before.values()))
    try:
        for path in managed:
            if before[path]:
                continue
            if path in expected:
                writer.replace(location, FileReplacement(path, expected[path]))
            else:
                writer.delete(location, FileDeletion(path))
    except DiagnosticError:
        raise
    except OSError, ValueError:
        pass

    after = {
        path: (
            _content(reader, location, path) == expected[path]
            if path in expected
            else _content(reader, location, path) is None
        )
        for path in managed
    }
    changed = tuple(path for path in intended if not before[path] and after[path])
    comparisons = tuple(PathComparison(path, after[path]) for path in managed)
    return ViewState(intended, changed, comparisons, all(after.values()))
