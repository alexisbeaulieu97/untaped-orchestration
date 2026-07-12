from __future__ import annotations

import base64
import copy
import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path, PurePath
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from untaped_orchestration.application.item_support import ItemMutationResult
from untaped_orchestration.application.query_models import (
    ItemDetail,
    ItemRow,
    NextItem,
    QualifiedItem,
    SearchHit,
)
from untaped_orchestration.application.results import MaintenanceResult, RawRecord
from untaped_orchestration.cli.options import OutputFormat, validate_format
from untaped_orchestration.domain.curation import CurationEntry
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.models import Revision

MAX_BRIEF_BYTES = 32768


class OutputEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True)

    schema_: Literal["untaped.orchestration.output/v1"] = Field(
        default="untaped.orchestration.output/v1",
        alias="schema",
    )
    command: str
    complete: bool
    truncated: bool
    data: object
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    data: object
    complete: bool = True
    truncated: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()
    pipe_kind: str | None = None
    exit_code: int = 0


@dataclass(frozen=True, slots=True)
class EncodedOutput:
    stdout: bytes
    stderr: bytes = b""
    truncated: bool = False


def _value(value: object) -> object:  # noqa: C901
    if isinstance(value, ItemMutationResult):
        return _value(value.receipt)
    if isinstance(value, MaintenanceResult):
        receipt = _value(value.receipt)
        assert isinstance(receipt, dict)
        return {
            **receipt,
            "matches": value.matches,
            "comparisons": _value(value.comparisons),
        }
    if isinstance(value, ItemRow):
        return {
            "id": value.item_id.root,
            "kind": value.kind.value,
            "title": value.title,
            "store_id": value.store_id.root,
            "path": value.path,
            "revision": value.revision.root,
            "stage": None if value.stage is None else value.stage.value,
            "state": None if value.state is None else value.state.value,
            "waiting_on": _value(value.waiting_on),
            "priority": None if value.priority is None else value.priority.value,
            "rank": value.rank,
            "due_on": _value(value.due_on),
        }
    if isinstance(value, ItemDetail):
        metadata = _value(value.metadata)
        row = _value(value.row)
        assert isinstance(metadata, dict) and isinstance(row, dict)
        return {
            **metadata,
            "body": value.body.decode("utf-8"),
            "store_id": row["store_id"],
            "path": row["path"],
            "revision": row["revision"],
            "store_revision": value.store_revision.root,
            "state": row["state"],
            "blocked": value.blocked,
            "blockers": _value(value.blockers),
            "due_on": _value(value.due_on),
            "complete": value.complete,
        }
    if isinstance(value, NextItem):
        row = _value(value.row)
        assert isinstance(row, dict)
        return {
            **row,
            "ancestor_path": _value(value.ancestor_path),
            "unblocks_count": value.unblocks_count,
            "due": value.due,
            "governing_decisions": _value(value.governing_decisions),
            "evidence_summary": _value(value.evidence_summary),
        }
    if isinstance(value, SearchHit):
        row = _value(value.row)
        assert isinstance(row, dict)
        return {**row, "snippet": value.snippet}
    if isinstance(value, QualifiedItem):
        return {"store_id": value.store_id.root, "id": value.item_id.root}
    if isinstance(value, CurationEntry):
        return {
            "id": value.item_id.root,
            "kind": value.kind.value,
            "store_id": value.store_id.root,
            "due_on": value.due_on.root,
        }
    if isinstance(value, Revision):
        return value.root
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, BaseModel):
        return _value(value.model_dump(by_alias=True, exclude_none=True))
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_value(item) for item in value]
    if isinstance(value, PurePath | Path):
        return value.as_posix()
    return value


def _raw_data(raw: RawRecord) -> dict[str, object]:
    return {
        "path": raw.path.as_posix(),
        "revision": raw.revision.root,
        "size": raw.size,
        "encoding": "base64",
        "content": base64.b64encode(raw.content).decode("ascii"),
    }


def _json_bytes(result: CommandResult, *, truncated: bool | None = None) -> bytes:
    data = _raw_data(result.data) if isinstance(result.data, RawRecord) else _value(result.data)
    envelope = {
        "schema": "untaped.orchestration.output/v1",
        "command": result.command,
        "complete": result.complete,
        "truncated": result.truncated if truncated is None else truncated,
        "data": data,
        "diagnostics": _value(result.diagnostics),
    }
    return (json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _rows(data: object) -> list[dict[str, object]]:
    normalized = _value(data)
    if isinstance(normalized, list):
        return [item if isinstance(item, dict) else {"value": item} for item in normalized]
    if isinstance(normalized, dict):
        return [normalized]
    return [{"value": normalized}]


def _lookup(row: dict[str, object], dotted: str) -> object:
    value: object = row
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _table_bytes(result: CommandResult, columns: tuple[str, ...]) -> bytes:
    rows = _rows(result.data)
    if not rows or (not columns and not rows[0]):
        return b""
    selected = columns or tuple(rows[0])
    lines = ["\t".join(selected)]
    for row in rows:
        lines.append(
            "\t".join(
                json.dumps(_lookup(row, name), ensure_ascii=False, separators=(",", ":"))
                if isinstance(_lookup(row, name), dict | list)
                else str(_lookup(row, name) if _lookup(row, name) is not None else "")
                for name in selected
            )
        )
    return ("\n".join(lines) + "\n").encode()


def _raw_bytes(result: CommandResult, columns: tuple[str, ...]) -> bytes:
    rows = _rows(result.data)
    if rows == [{}]:
        return b""
    lines = []
    for row in rows:
        first = "id" if "id" in row else next(iter(row), "value")
        selected = (first, *columns) if columns else (first,)
        values = (_lookup(row, name) for name in selected)
        lines.append("\t".join("" if value is None else str(value) for value in values))
    return (("\n".join(lines) + "\n") if lines else "").encode()


def _pipe_bytes(result: CommandResult) -> bytes:
    encoded = []
    rows = _rows(result.data)
    if rows == [{}]:
        rows = []
    for row in rows:
        row_kind = row.get("kind")
        kind = result.pipe_kind
        if kind is None and row_kind in {"task", "decision"}:
            kind = f"orchestration.{row_kind}"
        if kind is None:
            raise ValueError("pipe output requires a declared SDK Pipe v1 kind")
        encoded.append(
            (
                json.dumps(
                    {"untaped": "1", "kind": kind, "record": row},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
    diagnostics = list(result.diagnostics)
    incomplete_unreported = not result.complete and not any(
        value.code == "ORC005" for value in diagnostics
    )
    truncated_unreported = result.truncated and not any(
        "truncat" in f"{value.message} {value.hint}".casefold() for value in diagnostics
    )
    if incomplete_unreported or truncated_unreported:
        if incomplete_unreported and truncated_unreported:
            status = "incomplete and truncated"
        elif incomplete_unreported:
            status = "incomplete"
        else:
            status = "truncated"
        diagnostics.append(
            Diagnostic(
                code="ORC005",
                severity="warning",
                path="",
                field="",
                message=f"command result is {status}",
                hint="Narrow the query or inspect federation state before relying on omitted data.",
            )
        )
    for diagnostic in diagnostics:
        record = _value(diagnostic)
        assert isinstance(record, dict)
        encoded.append(
            (
                json.dumps(
                    {
                        "untaped": "1",
                        "kind": "orchestration.diagnostic",
                        "record": record,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
    return b"".join(encoded)


def _diagnostic_stderr(diagnostics: tuple[Diagnostic, ...]) -> bytes:
    return b"".join(
        f"{value.code}: {value.path}: {value.message}\n".encode() for value in diagnostics
    )


def _minimal_brief(result: CommandResult) -> CommandResult:
    source = _value(result.data)
    assert isinstance(source, dict)
    keep = {
        key: source[key]
        for key in (
            "store_id",
            "store_revision",
            "registry_revision",
            "ready_count",
            "blocker_count",
            "due_count",
            "diagnostic_count",
            "missing_store_count",
            "inactive_ruling_count",
            "globally_ready",
        )
        if key in source
    }
    return CommandResult(
        result.command,
        keep,
        complete=result.complete,
        truncated=True,
        diagnostics=(),
        pipe_kind=result.pipe_kind,
    )


def _bounded_brief(  # noqa: C901
    result: CommandResult,
    fmt: OutputFormat,
    columns: tuple[str, ...],
) -> EncodedOutput:
    normalized = _value(result.data)
    assert isinstance(normalized, dict)
    data = copy.deepcopy(normalized)
    diagnostics = list(result.diagnostics)
    renderer = _json_bytes if fmt == "json" else lambda value: _table_bytes(value, columns)

    def candidate() -> CommandResult:
        return CommandResult(
            result.command,
            data,
            complete=result.complete,
            truncated=True,
            diagnostics=tuple(diagnostics),
            pipe_kind=result.pipe_kind,
        )

    def rendered() -> bytes:
        return renderer(candidate())

    candidate_initial = CommandResult(
        result.command,
        data,
        complete=result.complete,
        truncated=result.truncated,
        diagnostics=tuple(diagnostics),
        pipe_kind=result.pipe_kind,
    )
    encoded = renderer(candidate_initial)
    if len(encoded) <= MAX_BRIEF_BYTES:
        return EncodedOutput(
            encoded,
            _diagnostic_stderr(tuple(diagnostics)),
            result.truncated,
        )

    dynamic_sections = ("ready", "blockers", "due")
    while any(isinstance(data.get(name), list) and data[name] for name in dynamic_sections) or (
        diagnostics
        or (isinstance(data.get("missing_store_ids"), list) and data["missing_store_ids"])
    ):
        for name in dynamic_sections:
            section = data.get(name)
            if isinstance(section, list) and section:
                section.pop()
                encoded = rendered()
                if len(encoded) <= MAX_BRIEF_BYTES:
                    return EncodedOutput(encoded, _diagnostic_stderr(tuple(diagnostics)), True)
        if diagnostics:
            diagnostics.pop()
            embedded = data.get("diagnostics")
            if isinstance(embedded, list) and embedded:
                embedded.pop()
            encoded = rendered()
            if len(encoded) <= MAX_BRIEF_BYTES:
                return EncodedOutput(encoded, _diagnostic_stderr(tuple(diagnostics)), True)
        missing = data.get("missing_store_ids")
        if isinstance(missing, list) and missing:
            missing.pop()
            encoded = rendered()
            if len(encoded) <= MAX_BRIEF_BYTES:
                return EncodedOutput(encoded, _diagnostic_stderr(tuple(diagnostics)), True)

    pinned = data.get("pinned_decisions")
    if isinstance(pinned, list):
        for decision in reversed(pinned):
            if not isinstance(decision, dict) or not isinstance(decision.get("body"), str):
                continue
            body = decision["body"]
            while body:
                body = body[: len(body) // 2]
                decision["body"] = body
                encoded = rendered()
                if len(encoded) <= MAX_BRIEF_BYTES:
                    return EncodedOutput(encoded, _diagnostic_stderr(tuple(diagnostics)), True)

    human_fields: list[tuple[dict[str, object], str]] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in {
                    "title",
                    "message",
                    "hint",
                    "note",
                    "revisit_when",
                    "snippet",
                } and isinstance(nested, str):
                    human_fields.append((value, key))
                else:
                    collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(data)
    for owner, key in reversed(human_fields):
        text = owner[key]
        assert isinstance(text, str)
        while text:
            text = text[: len(text) // 2]
            owner[key] = text
            encoded = rendered()
            if len(encoded) <= MAX_BRIEF_BYTES:
                return EncodedOutput(encoded, _diagnostic_stderr(tuple(diagnostics)), True)

    for name in ("in_progress", "ready", "blockers", "due"):
        section = data.get(name)
        records = section if isinstance(section, list) else [section]
        for index, record in enumerate(records):
            if isinstance(record, dict):
                summary = {key: record[key] for key in ("id", "revision") if key in record}
                if isinstance(section, list):
                    section[index] = summary
                else:
                    data[name] = summary
        encoded = rendered()
        if len(encoded) <= MAX_BRIEF_BYTES:
            return EncodedOutput(encoded, _diagnostic_stderr(tuple(diagnostics)), True)

    minimal = _minimal_brief(candidate())
    encoded = renderer(minimal)
    if len(encoded) > MAX_BRIEF_BYTES:
        raise ValueError("minimal brief exceeds output byte ceiling")
    return EncodedOutput(encoded, b"", True)


def encode_result(
    result: CommandResult,
    *,
    fmt: OutputFormat,
    columns: tuple[str, ...] = (),
) -> EncodedOutput:
    if result.command == "brief" and fmt in {"table", "json"}:
        return _bounded_brief(result, fmt, columns)
    if fmt == "json":
        return EncodedOutput(_json_bytes(result), b"", result.truncated)
    if fmt == "pipe":
        return EncodedOutput(_pipe_bytes(result), b"", result.truncated)
    if fmt == "raw":
        return EncodedOutput(
            _raw_bytes(result, columns),
            _diagnostic_stderr(result.diagnostics),
            result.truncated,
        )
    return EncodedOutput(
        _table_bytes(result, columns),
        _diagnostic_stderr(result.diagnostics),
        result.truncated,
    )


def encode_binary_recovery(raw: RawRecord) -> EncodedOutput:
    metadata = {
        "schema": "untaped.orchestration.raw-meta/v1",
        "path": raw.path.as_posix(),
        "revision": raw.revision.root,
        "size": raw.size,
    }
    return EncodedOutput(
        raw.content,
        (json.dumps(metadata, separators=(",", ":")) + "\n").encode(),
    )


def emit_encoded(encoded: EncodedOutput) -> None:
    sys.stdout.buffer.write(encoded.stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(encoded.stderr)
    sys.stderr.buffer.flush()


def _error_diagnostics(error: Exception) -> tuple[Diagnostic, ...]:
    diagnostics = getattr(error, "diagnostics", None)
    if isinstance(diagnostics, tuple) and all(
        isinstance(value, Diagnostic) for value in diagnostics
    ):
        return diagnostics
    code = "ORC007" if "revision" in error.__class__.__name__.lower() else "ORC002"
    return (
        Diagnostic(
            code=code,
            severity="error",
            path="",
            field="",
            message=str(error) or error.__class__.__name__,
            hint="Correct the reported condition and retry.",
        ),
    )


def _exit_code(error: Exception) -> int:
    name = error.__class__.__name__.lower()
    diagnostics = getattr(error, "diagnostics", ())
    if "incomplete" in name or any(
        isinstance(value, Diagnostic) and value.code == "ORC005" and value.severity == "error"
        for value in diagnostics
    ):
        return 3
    if "revision" in name or "lock" in name:
        return 4
    if isinstance(error, ValueError):
        return 1
    return 5


def exit_for(error: Exception) -> NoReturn:
    raise SystemExit(_exit_code(error)) from error


def run_command(
    command: str,
    action: Callable[[], CommandResult],
    *,
    fmt: OutputFormat,
    allowed: tuple[OutputFormat, ...],
    columns: tuple[str, ...] = (),
    binary_recovery: bool = False,
) -> None:
    try:
        selected = validate_format(fmt, allowed=allowed)
    except ValueError as error:
        sys.stderr.write(f"error: {error}\n")
        raise SystemExit(2) from error
    try:
        result = action()
        if binary_recovery and selected == "raw":
            if not isinstance(result.data, RawRecord):
                raise TypeError("binary recovery requires a raw record")
            emit_encoded(encode_binary_recovery(result.data))
        else:
            emit_encoded(encode_result(result, fmt=selected, columns=columns))
        if result.exit_code:
            raise SystemExit(result.exit_code)
    except Exception as error:
        failure = CommandResult(
            command,
            {},
            complete=False,
            diagnostics=_error_diagnostics(error),
        )
        emit_encoded(encode_result(failure, fmt=selected, columns=columns))
        if "--debug" in sys.argv[1:] and _exit_code(error) == 5:
            traceback.print_exc()
        exit_for(error)
