from __future__ import annotations

from pathlib import Path, PurePosixPath

from untaped_orchestration.application.ports import (
    FileDeletion,
    FileReplacement,
    LoadedRecord,
    RawRecord,
    RawReference,
    StoreLocation,
    StoreReader,
    StoreSnapshot,
    StoreWriter,
)
from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics
from untaped_orchestration.infrastructure.codec import (
    CodecError,
    ItemCodec,
    RegistryCodec,
    StoreConfigCodec,
)
from untaped_orchestration.infrastructure.filesystem import (
    AtomicFilesystem,
    canonical_input_paths,
    discover_location,
    file_revision,
    safe_delete_path,
    safe_raw_path,
    safe_write_path,
    store_revision_from_file_revisions,
)


class FilesystemStoreRepository(StoreReader, StoreWriter):
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

    def replace(self, location: StoreLocation, change: FileReplacement) -> None:
        target = safe_write_path(location, change.path)
        self._atomic.replace_bytes(target, change.content, root=location.real_root)

    def delete(self, location: StoreLocation, change: FileDeletion) -> None:
        target = safe_delete_path(location, change.path)
        self._atomic.delete_file(target)


def _is_item_path(relative_path: PurePosixPath) -> bool:
    return relative_path.parts[:-1] in {
        ("tasks",),
        ("decisions",),
        ("archive", "tasks"),
    }
