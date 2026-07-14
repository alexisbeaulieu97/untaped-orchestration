from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from untaped_orchestration.application.ports import (
    AdministrativeState,
    CanonicalFormatter,
    ExternalFileReader,
    FederatedSnapshot,
    FileDeletion,
    FileReplacement,
    LoadedRecord,
    ProjectedMutation,
    RawRecord,
    RawReference,
    StoreEntry,
    StoreLocation,
    StoreReader,
    StoreSnapshot,
    StoreWriter,
    UnprovableBodyBoundary,
)
from untaped_orchestration.domain.canonical import CanonicalItem
from untaped_orchestration.domain.diagnostics import Diagnostic, DiagnosticError, sort_diagnostics
from untaped_orchestration.domain.limits import BODY_LIMIT, FRONTMATTER_LIMIT, ITEM_FILE_LIMIT
from untaped_orchestration.domain.models import Registry, StoreConfig
from untaped_orchestration.infrastructure.codec import (
    CodecError,
    ItemCodec,
    ItemDocument,
    RegistryCodec,
    StoreConfigCodec,
)
from untaped_orchestration.infrastructure.external_files import FilesystemExternalFileReader
from untaped_orchestration.infrastructure.filesystem import (
    ADMIN_PATHS,
    ITEM_ROOTS,
    VIEW_PATHS,
    AtomicFilesystem,
    PathSafetyError,
    StoreNotFoundError,
    canonical_atomic_temporary_target,
    canonical_input_paths,
    discover_location,
    file_revision,
    prepare_store_root,
    safe_delete_path,
    safe_raw_path,
    safe_read_path,
    safe_write_path,
    store_entries,
    store_revision_from_file_revisions,
)


class FilesystemStoreRepository(StoreReader, StoreWriter, CanonicalFormatter):
    def __init__(
        self,
        *,
        atomic: AtomicFilesystem | None = None,
        item_codec: ItemCodec | None = None,
        store_codec: StoreConfigCodec | None = None,
        registry_codec: RegistryCodec | None = None,
        external_files: ExternalFileReader | None = None,
    ) -> None:
        self._atomic = atomic or AtomicFilesystem()
        self._items = item_codec or ItemCodec()
        self._stores = store_codec or StoreConfigCodec()
        self._registries = registry_codec or RegistryCodec()
        self._external_files = external_files or FilesystemExternalFileReader()

    def read_external(self, path: Path, *, limit: int, field: str) -> bytes:
        return self._external_files.read_external(path, limit=limit, field=field)

    def _read_canonical(
        self,
        location: StoreLocation,
        relative_path: PurePosixPath,
    ) -> bytes:
        limit = ITEM_FILE_LIMIT if _is_item_path(relative_path) else FRONTMATTER_LIMIT
        absolute = location.real_root.joinpath(*relative_path.parts)
        return self._read_bounded(absolute, relative_path, limit=limit)

    def _read_bounded(
        self,
        absolute: Path,
        relative_path: PurePosixPath,
        *,
        limit: int,
    ) -> bytes:
        try:
            return self.read_external(absolute, limit=limit, field="")
        except DiagnosticError as error:
            raise DiagnosticError(
                tuple(
                    diagnostic.model_copy(update={"path": relative_path.as_posix()})
                    for diagnostic in error.diagnostics
                )
            ) from error

    def discover(self, start: Path, override: Path | None = None) -> StoreLocation:
        return discover_location(start, override)

    def load_local(self, location: StoreLocation, *, headers_only: bool) -> StoreSnapshot:
        relative_paths = canonical_input_paths(location)
        diagnostics: list[Diagnostic] = []
        store = None
        store_config_revision = None
        registry = None
        registry_revision = None
        records: list[LoadedRecord] = []
        raw_index: list[RawReference] = []
        file_revisions = {}
        for relative_path in relative_paths:
            raw = self._read_canonical(location, relative_path)
            if _is_item_path(relative_path):
                streamed = self._items.parse_stream(
                    io.BytesIO(raw),
                    relative_path=relative_path,
                    headers_only=headers_only,
                )
                file_revisions[relative_path] = streamed.revision
                raw_index.append(
                    RawReference(
                        path=relative_path,
                        revision=streamed.revision,
                        size=streamed.size,
                    )
                )
                if streamed.diagnostic is not None:
                    diagnostics.append(streamed.diagnostic)
                    continue
                assert streamed.metadata is not None
                records.append(
                    LoadedRecord(
                        path=relative_path,
                        revision=streamed.revision,
                        metadata=streamed.metadata,
                        body=streamed.body,
                    )
                )
                continue

            revision = file_revision(raw)
            file_revisions[relative_path] = revision
            if relative_path == PurePosixPath("store.toml"):
                store_config_revision = revision
                try:
                    store = self._stores.parse(raw)
                except CodecError as error:
                    diagnostics.append(error.diagnostic)
                continue
            if relative_path == PurePosixPath("registry.toml"):
                registry_revision = revision
                try:
                    registry = self._registries.parse(raw)
                except CodecError as error:
                    diagnostics.append(error.diagnostic)
                continue
            continue

        if store_config_revision is None:
            raise StoreNotFoundError(f"no regular store.toml anchor at store root: {location.root}")

        if store is not None and registry is not None and store.id != registry.store_id:
            diagnostics.append(
                Diagnostic(
                    code="ORC003",
                    severity="error",
                    path="registry.toml",
                    field="store_id",
                    message="registry store identity does not match store.toml",
                    hint="Set registry.toml store_id to the immutable local store ID.",
                )
            )

        records.sort(key=lambda record: record.path.as_posix())
        raw_index.sort(
            key=lambda reference: (
                reference.path.name.casefold(),
                reference.path.name,
                reference.path.as_posix(),
            )
        )
        return StoreSnapshot(
            location=location,
            store=store,
            registry=registry,
            records=tuple(records),
            load_diagnostics=sort_diagnostics(diagnostics),
            raw_index=tuple(raw_index),
            store_revision=store_revision_from_file_revisions(file_revisions),
            registry_revision=registry_revision,
            store_config_revision=store_config_revision,
        )

    def read_raw(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord:
        absolute = safe_raw_path(location, relative_path)
        raw = self._read_bounded(absolute, relative_path, limit=ITEM_FILE_LIMIT)
        return RawRecord(
            path=relative_path,
            revision=file_revision(raw),
            size=len(raw),
            content=raw,
        )

    def read_file(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord:
        absolute = safe_read_path(location, relative_path)
        raw = self._read_bounded(
            absolute,
            relative_path,
            limit=_read_file_limit(relative_path),
        )
        return RawRecord(
            path=relative_path,
            revision=file_revision(raw),
            size=len(raw),
            content=raw,
        )

    def read_item_body(self, location: StoreLocation, relative_path: PurePosixPath) -> bytes:
        if not _is_item_path(relative_path):
            raise PathSafetyError(relative_path, "item body reads require an exact item path")
        absolute = safe_read_path(location, relative_path)
        raw = self._read_bounded(absolute, relative_path, limit=ITEM_FILE_LIMIT)
        document = self._items.parse(raw, relative_path=relative_path)
        return document.body

    def list_entries(self, location: StoreLocation) -> tuple[StoreEntry, ...]:
        return store_entries(location)

    def inspect_administrative(self, location: StoreLocation) -> AdministrativeState:
        store_id = None
        registry_revision = None
        try:
            store_raw = self._read_canonical(location, PurePosixPath("store.toml"))
            store_id = self._stores.parse(store_raw).id.root
        except CodecError, FileNotFoundError, PathSafetyError:
            pass
        except DiagnosticError as error:
            if not _is_unavailable_path(error):
                raise
        try:
            registry_raw = self._read_canonical(location, PurePosixPath("registry.toml"))
            registry_revision = file_revision(registry_raw)
        except FileNotFoundError, PathSafetyError:
            pass
        except DiagnosticError as error:
            if not _is_unavailable_path(error):
                raise
        return AdministrativeState(store_id, registry_revision)

    def prepare(self, root: Path) -> StoreLocation:
        return prepare_store_root(root)

    def replace(self, location: StoreLocation, change: FileReplacement) -> None:
        target = safe_write_path(location, change.path)
        self._atomic.replace_bytes(target, change.content, root=location.real_root)

    def delete(self, location: StoreLocation, change: FileDeletion) -> None:
        target = safe_delete_path(location, change.path)
        self._atomic.delete_file(target)

    def store_bytes(self, config: StoreConfig) -> bytes:
        return self._stores.canonical_bytes(config)

    def registry_bytes(self, registry: Registry) -> bytes:
        return self._registries.canonical_bytes(registry)

    def item_bytes(self, metadata: CanonicalItem, body: bytes) -> bytes:
        return self._items.canonical_bytes(ItemDocument(metadata=metadata, body=body, original=b""))

    def parse_item_parts(
        self,
        frontmatter: bytes,
        body: bytes,
        *,
        relative_path: PurePosixPath,
    ) -> tuple[CanonicalItem, bytes]:
        raw = b"+++\n" + frontmatter.rstrip(b"\n") + b"\n+++\n" + body
        document = self._items.parse(raw, relative_path=relative_path)
        return document.metadata, document.body

    def repaired_item_bytes(
        self,
        *,
        relative_path: PurePosixPath,
        current: bytes,
        replacement_frontmatter: bytes,
        replacement_body: bytes | None,
    ) -> bytes:
        if replacement_body is None:
            try:
                replacement_body = self._items.proven_body(
                    current,
                    relative_path=relative_path,
                )
            except CodecError as error:
                raise UnprovableBodyBoundary from error
        return self._items.repaired_bytes(
            relative_path=relative_path,
            current=current,
            replacement_frontmatter=replacement_frontmatter,
            replacement_body=replacement_body,
        )

    def project(
        self,
        current: FederatedSnapshot,
        selected: StoreLocation,
        replacements: Sequence[FileReplacement],
        deletions: Sequence[FileDeletion],
    ) -> ProjectedMutation:
        entry_map = {entry.path: entry for entry in store_entries(selected)}
        contents = {
            path: self._read_canonical(selected, path)
            for path, entry in entry_map.items()
            if entry.kind == "file" and (path in ADMIN_PATHS or _is_item_path(path))
        }
        for replacement in replacements:
            entry_map[replacement.path] = StoreEntry(replacement.path, "file")
            if replacement.path in ADMIN_PATHS or _is_item_path(replacement.path):
                contents[replacement.path] = replacement.content
        for deletion in deletions:
            entry_map.pop(deletion.path, None)
            contents.pop(deletion.path, None)
        entries = tuple(sorted(entry_map.values(), key=lambda value: value.path.as_posix()))
        files = {
            path: raw
            for path, raw in contents.items()
            if path in ADMIN_PATHS or _is_item_path(path)
        }
        projected = self._snapshot_from_bytes(selected, files)
        stores = tuple(
            projected if value.location.real_root == selected.real_root else value
            for value in current.stores
        )
        return ProjectedMutation(
            FederatedSnapshot(projected, stores, current.completeness),
            entries,
            contents,
        )

    def _snapshot_from_bytes(
        self,
        location: StoreLocation,
        files: dict[PurePosixPath, bytes],
    ) -> StoreSnapshot:
        diagnostics: list[Diagnostic] = []
        store = None
        registry = None
        records: list[LoadedRecord] = []
        raw_index: list[RawReference] = []
        revisions = {path: file_revision(raw) for path, raw in files.items()}
        for path, raw in sorted(files.items(), key=lambda value: value[0].as_posix()):
            if _is_item_path(path):
                try:
                    parsed = self._items.parse(raw, relative_path=path)
                except CodecError as error:
                    diagnostics.append(error.diagnostic)
                    continue
                records.append(LoadedRecord(path, revisions[path], parsed.metadata, parsed.body))
                raw_index.append(RawReference(path, revisions[path], len(raw)))
            elif path == PurePosixPath("store.toml"):
                try:
                    store = self._stores.parse(raw)
                except CodecError as error:
                    diagnostics.append(error.diagnostic)
            elif path == PurePosixPath("registry.toml"):
                try:
                    registry = self._registries.parse(raw)
                except CodecError as error:
                    diagnostics.append(error.diagnostic)
        if store is not None and registry is not None and store.id != registry.store_id:
            diagnostics.append(
                Diagnostic(
                    code="ORC003",
                    severity="error",
                    path="registry.toml",
                    field="store_id",
                    message="registry store identity does not match store.toml",
                    hint="Set registry.toml store_id to the immutable local store ID.",
                )
            )
        records.sort(key=lambda value: value.path.as_posix())
        raw_index.sort(
            key=lambda value: (
                value.path.name.casefold(),
                value.path.name,
                value.path.as_posix(),
            )
        )
        store_config_revision = revisions.get(PurePosixPath("store.toml"))
        if store_config_revision is None:
            store_config_revision = current_revision = file_revision(b"")
            diagnostics.append(
                Diagnostic(
                    code="ORC003",
                    severity="error",
                    path="store.toml",
                    field="path",
                    message="store anchor cannot be deleted by a mutation",
                    hint="Keep the immutable store.toml anchor.",
                )
            )
        else:
            current_revision = store_revision_from_file_revisions(revisions)
        return StoreSnapshot(
            location=location,
            store=store,
            registry=registry,
            records=tuple(records),
            load_diagnostics=sort_diagnostics(diagnostics),
            raw_index=tuple(raw_index),
            store_revision=current_revision,
            registry_revision=revisions.get(PurePosixPath("registry.toml")),
            store_config_revision=store_config_revision,
        )


def _is_item_path(relative_path: PurePosixPath) -> bool:
    return relative_path.suffix == ".md" and any(
        relative_path.parts[:-1] == root.parts for root in ITEM_ROOTS
    )


def _read_file_limit(relative_path: PurePosixPath) -> int:
    temporary_target = canonical_atomic_temporary_target(relative_path)
    if temporary_target is not None:
        return _read_file_limit(temporary_target)
    if _is_item_path(relative_path):
        return ITEM_FILE_LIMIT
    if relative_path in VIEW_PATHS:
        return BODY_LIMIT
    if relative_path in {
        PurePosixPath("store.toml"),
        PurePosixPath("registry.toml"),
    }:
        return FRONTMATTER_LIMIT
    if relative_path in {
        PurePosixPath("AGENTS.md"),
        PurePosixPath("CLAUDE.md"),
    }:
        return FRONTMATTER_LIMIT
    raise PathSafetyError(relative_path, "reads are restricted to canonical store paths")


def _is_unavailable_path(error: DiagnosticError) -> bool:
    return bool(error.diagnostics) and all(
        diagnostic.code == "ORC003" for diagnostic in error.diagnostics
    )
