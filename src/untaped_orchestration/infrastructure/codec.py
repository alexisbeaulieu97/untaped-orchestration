import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

import tomli_w
from pydantic import TypeAdapter, ValidationError

from untaped_orchestration.domain.canonical import (
    CanonicalItem,
    CanonicalTable,
    canonical_item_table,
    canonical_registry_table,
    canonical_store_table,
)
from untaped_orchestration.domain.diagnostics import Diagnostic, DiagnosticCode
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    ItemRecord,
    Registry,
    StoreConfig,
)

FRONTMATTER_LIMIT = 64 * 1024
BODY_LIMIT = 1024 * 1024
UTF8_BOM = b"\xef\xbb\xbf"
ITEM_FILENAME_RE = re.compile(r"(?P<id>(?:tsk|dec)_[0-9a-f]{32})-(?P<slug>.*)\.md")
ITEM_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ITEM_ADAPTER: TypeAdapter[ItemRecord] = TypeAdapter(ItemRecord)


class CodecError(ValueError):
    def __init__(self, diagnostic: Diagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


@dataclass(frozen=True, slots=True)
class ItemDocument:
    metadata: ActiveTask | ArchivedTask | Decision
    body: bytes
    original: bytes


def _error(
    code: DiagnosticCode,
    *,
    path: str,
    field: str,
    message: str,
    hint: str,
    line: int | None = None,
    column: int | None = None,
    byte_offset: int | None = None,
) -> CodecError:
    return CodecError(
        Diagnostic(
            code=code,
            severity="error",
            path=path,
            field=field,
            line=line,
            column=column,
            byte_offset=byte_offset,
            message=message,
            hint=hint,
        )
    )


def _decode_utf8(
    raw: bytes,
    *,
    path: str,
    byte_offset_base: int = 0,
    line_offset: int = 0,
    reject_bom: bool = True,
) -> str:
    if reject_bom and raw.startswith(UTF8_BOM):
        raise _error(
            "ORC001",
            path=path,
            field="",
            line=1 + line_offset,
            column=1,
            byte_offset=byte_offset_base,
            message="TOML must not contain a UTF-8 byte-order mark",
            hint="Remove the byte-order mark and encode the content as plain UTF-8.",
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        prefix = raw[: error.start]
        line = prefix.count(b"\n") + 1 + line_offset
        line_prefix = prefix.rsplit(b"\n", maxsplit=1)[-1]
        column = len(line_prefix.decode("utf-8")) + 1
        raise _error(
            "ORC001",
            path=path,
            field="",
            line=line,
            column=column,
            byte_offset=byte_offset_base + error.start,
            message="content is not valid UTF-8",
            hint="Encode the complete item as UTF-8 without a byte-order mark.",
        ) from error


def _toml_mapping(raw: bytes, *, path: str, line_offset: int = 0) -> dict[str, object]:
    text = _decode_utf8(
        raw,
        path=path,
        byte_offset_base=4 if line_offset else 0,
        line_offset=line_offset,
    )
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        byte_offset = len(text[: error.pos].encode("utf-8")) + (4 if line_offset else 0)
        raise _error(
            "ORC001",
            path=path,
            field="",
            line=error.lineno + line_offset,
            column=error.colno,
            byte_offset=byte_offset,
            message=f"invalid TOML: {error.msg}",
            hint="Correct the TOML syntax without changing the Markdown body.",
        ) from error


def _validation_field(error: ValidationError) -> str:
    first = error.errors()[0]
    return ".".join(str(part) for part in first["loc"])


def _validation_error(error: ValidationError, *, path: str) -> CodecError:
    first = error.errors()[0]
    return _error(
        "ORC002",
        path=path,
        field=_validation_field(error),
        message=f"invalid canonical metadata: {first['msg']}",
        hint="Provide the complete typed schema and remove unknown fields.",
    )


def _parse_item_metadata(
    raw_toml: bytes,
    *,
    path: str,
    line_offset: int = 0,
) -> CanonicalItem:
    mapping = _toml_mapping(raw_toml, path=path, line_offset=line_offset)
    try:
        return ITEM_ADAPTER.validate_python(mapping)
    except ValidationError as error:
        raise _validation_error(error, path=path) from error


def _validate_item_path(metadata: CanonicalItem, *, relative_path: PurePosixPath) -> None:
    expected_parent: tuple[str, ...]
    if isinstance(metadata, ActiveTask):
        expected_parent = ("tasks",)
    elif isinstance(metadata, ArchivedTask):
        expected_parent = ("archive", "tasks")
    else:
        expected_parent = ("decisions",)

    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.parts[:-1] != expected_parent
    ):
        raise _error(
            "ORC003",
            path=relative_path.as_posix(),
            field="path",
            message="item path is unsafe or does not match its canonical record placement",
            hint=(
                "Place active tasks under tasks/, archived tasks under archive/tasks/, "
                "and decisions under decisions/."
            ),
        )

    match = ITEM_FILENAME_RE.fullmatch(relative_path.name)
    if match is None:
        raise _error(
            "ORC003",
            path=relative_path.as_posix(),
            field="filename",
            message="item filename does not have the canonical typed-ID and Markdown form",
            hint="Use <typed-id>-<creation-slug>.md with a lowercase typed ID.",
        )
    slug = match.group("slug")
    if len(slug) > 64 or ITEM_SLUG_RE.fullmatch(slug) is None:
        raise _error(
            "ORC003",
            path=relative_path.as_posix(),
            field="slug",
            message="item filename has a noncanonical creation slug",
            hint="Use a lowercase slug of at most 64 characters.",
        )
    if match.group("id") != metadata.id.root:
        raise _error(
            "ORC003",
            path=relative_path.as_posix(),
            field="id",
            message="filename identity does not match validated item metadata",
            hint="Use a filename whose typed ID equals the metadata ID.",
        )


def _split_item(raw: bytes, *, relative_path: PurePosixPath) -> tuple[bytes, bytes]:
    path = relative_path.as_posix()
    if raw.startswith(UTF8_BOM):
        raise _error(
            "ORC001",
            path=path,
            field="",
            line=1,
            column=1,
            byte_offset=0,
            message="item must not begin with a UTF-8 byte-order mark",
            hint="Remove the byte-order mark so the opener begins at byte zero.",
        )
    if not raw.startswith(b"+++\n"):
        raise _error(
            "ORC001",
            path=path,
            field="",
            line=1,
            column=1,
            byte_offset=0,
            message="item is missing the exact byte-zero opening delimiter +++\\n",
            hint="Start the item with an exact +++ line using LF.",
        )

    cursor = 4
    while True:
        if cursor - 4 > FRONTMATTER_LIMIT:
            raise _error(
                "ORC001",
                path=path,
                field="",
                message="item front matter exceeds the 64 KiB limit",
                hint="Reduce metadata before parsing or formatting the item.",
            )
        line_end = raw.find(b"\n", cursor, 4 + FRONTMATTER_LIMIT + 4)
        if line_end == -1:
            if len(raw) - cursor == 3 and raw[cursor:] == b"+++":
                metadata = raw[4:cursor]
                body = b""
                break
            if len(raw) - 4 > FRONTMATTER_LIMIT:
                raise _error(
                    "ORC001",
                    path=path,
                    field="",
                    message="item front matter exceeds the 64 KiB limit",
                    hint="Reduce metadata before parsing or formatting the item.",
                )
            raise _error(
                "ORC001",
                path=path,
                field="",
                message="item is missing an exact closing delimiter line",
                hint="Add a line containing only +++ after the TOML metadata.",
            )
        if raw[cursor:line_end] == b"+++":
            metadata = raw[4:cursor]
            body = raw[line_end + 1 :]
            break
        cursor = line_end + 1

    if len(metadata) > FRONTMATTER_LIMIT:
        raise _error(
            "ORC001",
            path=path,
            field="",
            message="item front matter exceeds the 64 KiB limit",
            hint="Reduce metadata before parsing or formatting the item.",
        )
    if len(body) > BODY_LIMIT:
        raise _error(
            "ORC001",
            path=path,
            field="body",
            message="item Markdown body exceeds the 1 MiB limit",
            hint="Reduce the body to at most 1 MiB.",
        )
    _decode_utf8(raw, path=path)
    return metadata, body


def _dump_with_array_tables(table: CanonicalTable, *array_table_keys: str) -> bytes:
    root = dict(table)
    array_tables: list[tuple[str, list[dict[str, object]]]] = []
    for key in array_table_keys:
        records = cast(list[dict[str, object]], root.pop(key))
        array_tables.append((key, records))

    sections = [tomli_w.dumps(root).rstrip("\n")]
    for key, records in array_tables:
        sections.extend(f"[[{key}]]\n{tomli_w.dumps(record).rstrip(chr(10))}" for record in records)
    return ("\n\n".join(sections) + "\n").encode("utf-8")


class ItemCodec:
    def parse(self, raw: bytes, *, relative_path: PurePosixPath) -> ItemDocument:
        metadata_bytes, body = _split_item(raw, relative_path=relative_path)
        metadata = _parse_item_metadata(
            metadata_bytes,
            path=relative_path.as_posix(),
            line_offset=1,
        )
        _validate_item_path(metadata, relative_path=relative_path)
        return ItemDocument(metadata=metadata, body=body, original=raw)

    def canonical_bytes(self, document: ItemDocument) -> bytes:
        if len(document.body) > BODY_LIMIT:
            raise _error(
                "ORC001",
                path="<memory>",
                field="body",
                message="item Markdown body exceeds the 1 MiB limit",
                hint="Reduce the body to at most 1 MiB.",
            )
        _decode_utf8(document.body, path="<memory>", reject_bom=False)
        metadata_bytes = _dump_with_array_tables(
            canonical_item_table(document.metadata),
            "links",
            "evidence",
        )
        if len(metadata_bytes) > FRONTMATTER_LIMIT:
            raise _error(
                "ORC001",
                path="<memory>",
                field="",
                message="canonical item front matter exceeds the 64 KiB limit",
                hint="Reduce links, evidence, or other metadata.",
            )
        reparsed = _parse_item_metadata(metadata_bytes, path="<memory>")
        if reparsed != document.metadata:
            raise _error(
                "ORC002",
                path="<memory>",
                field="",
                message="canonical item serialization changed validated metadata",
                hint="Refuse the rewrite and report an internal serialization defect.",
            )
        return b"+++\n" + metadata_bytes + b"+++\n" + document.body

    def parse_replacement_frontmatter(
        self,
        raw_toml: bytes,
        *,
        relative_path: PurePosixPath,
    ) -> ActiveTask | ArchivedTask | Decision:
        if len(raw_toml) > FRONTMATTER_LIMIT:
            raise _error(
                "ORC001",
                path=relative_path.as_posix(),
                field="",
                message="replacement front matter exceeds the 64 KiB limit",
                hint="Reduce metadata before attempting repair.",
            )
        metadata = _parse_item_metadata(raw_toml, path=relative_path.as_posix())
        _validate_item_path(metadata, relative_path=relative_path)
        return metadata


class StoreConfigCodec:
    def parse(self, raw: bytes) -> StoreConfig:
        mapping = _toml_mapping(raw, path="store.toml")
        try:
            return StoreConfig.model_validate(mapping)
        except ValidationError as error:
            raise _validation_error(error, path="store.toml") from error

    def canonical_bytes(self, config: StoreConfig) -> bytes:
        raw = tomli_w.dumps(canonical_store_table(config)).encode("utf-8")
        reparsed = self.parse(raw)
        if reparsed != config:
            raise _error(
                "ORC002",
                path="store.toml",
                field="",
                message="canonical store serialization changed validated configuration",
                hint="Refuse the rewrite and report an internal serialization defect.",
            )
        return raw


class RegistryCodec:
    def parse(self, raw: bytes) -> Registry:
        mapping = _toml_mapping(raw, path="registry.toml")
        try:
            return Registry.model_validate(mapping)
        except ValidationError as error:
            raise _validation_error(error, path="registry.toml") from error

    def canonical_bytes(self, registry: Registry) -> bytes:
        raw = _dump_with_array_tables(canonical_registry_table(registry), "children")
        reparsed = self.parse(raw)
        if reparsed != registry:
            raise _error(
                "ORC002",
                path="registry.toml",
                field="",
                message="canonical registry serialization changed validated configuration",
                hint="Refuse the rewrite and report an internal serialization defect.",
            )
        return raw
