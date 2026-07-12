from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, cast

from cyclopts import Parameter

from untaped_orchestration.infrastructure.codec import BODY_LIMIT

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


def usage_value[T](factory: Callable[[], T]) -> T:
    try:
        return factory()
    except (TypeError, ValueError) as error:
        sys.stderr.write(f"error: {error}\n")
        raise SystemExit(2) from error


def read_body_file(path: Path) -> bytes:
    with path.open("rb") as stream:
        body = stream.read(BODY_LIMIT + 1)
    if len(body) > BODY_LIMIT:
        raise ValueError("body file exceeds the 1 MiB limit")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("body file must contain valid UTF-8") from error
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
