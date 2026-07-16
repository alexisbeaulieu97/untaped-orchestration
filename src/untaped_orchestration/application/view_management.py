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
from untaped_orchestration.application.results import ItemRevision, MutationReceipt, PathComparison
from untaped_orchestration.domain.diagnostics import DiagnosticError

_ACKNOWLEDGED_VIEW_PATHS = "_acknowledged_view_paths"


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
    except DiagnosticError as error:
        setattr(error, _ACKNOWLEDGED_VIEW_PATHS, ())
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
    acknowledged: list[PurePosixPath] = []
    try:
        for path in managed:
            if before[path]:
                continue
            if path in expected:
                writer.replace(location, FileReplacement(path, expected[path]))
            else:
                writer.delete(location, FileDeletion(path))
            acknowledged.append(path)
    except DiagnosticError as error:
        setattr(error, _ACKNOWLEDGED_VIEW_PATHS, tuple(acknowledged))
        raise
    except OSError, ValueError:
        pass

    try:
        after = {
            path: (
                _content(reader, location, path) == expected[path]
                if path in expected
                else _content(reader, location, path) is None
            )
            for path in managed
        }
    except DiagnosticError as error:
        setattr(error, _ACKNOWLEDGED_VIEW_PATHS, tuple(acknowledged))
        raise
    except OSError, ValueError:
        return ViewState(
            intended,
            tuple(acknowledged),
            tuple(PathComparison(path, False) for path in managed),
            False,
        )
    changed = tuple(path for path in intended if not before[path] and after[path])
    comparisons = tuple(PathComparison(path, after[path]) for path in managed)
    return ViewState(intended, changed, comparisons, all(after.values()))


def finalize_views(
    reader: StoreReader,
    writer: StoreWriter,
    location: StoreLocation,
    renderer: ViewRenderer,
    snapshot: StoreSnapshot,
    *,
    canonical_paths: tuple[PurePosixPath, ...],
    replayed: bool = False,
    write: bool = True,
) -> ViewState:
    """Apply views and attach a durable receipt to typed post-canonical failures."""
    try:
        return apply_views(reader, writer, location, renderer, snapshot, write=write)
    except DiagnosticError as error:
        view_paths = getattr(error, _ACKNOWLEDGED_VIEW_PATHS, ())
        acknowledged = (*canonical_paths, *view_paths)
        error.receipt = MutationReceipt(  # type: ignore[attr-defined]
            applied=bool(acknowledged),
            replayed=replayed,
            canonical_applied=bool(canonical_paths),
            views_current=False,
            intended_paths=acknowledged,
            changed_paths=acknowledged,
            item_revisions=tuple(
                ItemRevision(record.path, record.revision) for record in snapshot.records
            ),
            store_revision=snapshot.store_revision,
            registry_revision=snapshot.registry_revision,
        )
        raise
