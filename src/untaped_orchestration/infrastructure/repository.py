from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from untaped_orchestration.application.ports import (
    CanonicalFormatter,
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
)
from untaped_orchestration.domain.canonical import CanonicalItem
from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics
from untaped_orchestration.domain.models import Registry, StoreConfig
from untaped_orchestration.infrastructure.codec import (
    CodecError,
    ItemCodec,
    ItemDocument,
    RegistryCodec,
    StoreConfigCodec,
)
from untaped_orchestration.infrastructure.filesystem import (
    ADMIN_PATHS,
    AtomicFilesystem,
    StoreNotFoundError,
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
    ) -> None:
        self._atomic = atomic or AtomicFilesystem()
        self._items = item_codec or ItemCodec()
        self._stores = store_codec or StoreConfigCodec()
        self._registries = registry_codec or RegistryCodec()

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
            absolute = location.real_root.joinpath(*relative_path.parts)
            if _is_item_path(relative_path):
                with absolute.open("rb") as stream:
                    streamed = self._items.parse_stream(
                        stream,
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

            raw = absolute.read_bytes()
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
        raw = safe_raw_path(location, relative_path).read_bytes()
        return RawRecord(
            path=relative_path,
            revision=file_revision(raw),
            size=len(raw),
            content=raw,
        )

    def read_file(self, location: StoreLocation, relative_path: PurePosixPath) -> RawRecord:
        raw = safe_read_path(location, relative_path).read_bytes()
        return RawRecord(
            path=relative_path,
            revision=file_revision(raw),
            size=len(raw),
            content=raw,
        )

    def list_entries(self, location: StoreLocation) -> tuple[StoreEntry, ...]:
        return store_entries(location)

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

    def project(
        self,
        current: FederatedSnapshot,
        selected: StoreLocation,
        replacements: Sequence[FileReplacement],
        deletions: Sequence[FileDeletion],
    ) -> ProjectedMutation:
        entry_map = {entry.path: entry for entry in store_entries(selected)}
        contents = {
            path: selected.real_root.joinpath(*path.parts).read_bytes()
            for path, entry in entry_map.items()
            if entry.kind == "file"
        }
        for replacement in replacements:
            entry_map[replacement.path] = StoreEntry(replacement.path, "file")
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
    return relative_path.parts[:-1] in {
        ("tasks",),
        ("decisions",),
        ("archive", "tasks"),
    }
