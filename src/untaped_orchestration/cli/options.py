from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, cast

from cyclopts import Parameter

from untaped_orchestration.application.ports import ExternalFileReader
from untaped_orchestration.domain.diagnostics import DiagnosticError, expected_diagnostic
from untaped_orchestration.domain.limits import BODY_LIMIT

OutputFormat = Literal["table", "json", "pipe", "raw"]
ColumnsOption = Annotated[
    tuple[str, ...],
    Parameter(
        name=["--columns", "-c"],
        help="Columns to include (repeatable).",
        consume_multiple=False,
    ),
]
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class BodyInputError(DiagnosticError):
    def __init__(self, path: Path, message: str) -> None:
        super().__init__(
            expected_diagnostic(
                "ORC001",
                message,
                path=path.as_posix(),
                field="body",
            )
        )


def usage_value[T](factory: Callable[[], T]) -> T:
    try:
        return factory()
    except (TypeError, ValueError) as error:
        sys.stderr.write(f"error: {error}\n")
        raise SystemExit(2) from error


def read_body_file(reader: ExternalFileReader, path: Path) -> bytes:
    body = reader.read_external(path, limit=BODY_LIMIT, field="body")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BodyInputError(path, "body file must contain valid UTF-8") from error
    return body


def validate_limit(value: int) -> int:
    if not 1 <= value <= MAX_LIMIT:
        raise ValueError("limit must be in range 1..200")
    return value


def validate_format(value: str, *, allowed: tuple[OutputFormat, ...]) -> OutputFormat:
    if value not in {"table", "json", "pipe", "raw"}:
        raise ValueError(f"unsupported output format {value!r}; yaml is not supported")
    result = cast(OutputFormat, value)
    if result not in allowed:
        raise ValueError(f"format {result!r} is not available for this command")
    return result
