from __future__ import annotations

from typing import Literal, cast

OutputFormat = Literal["table", "json", "pipe", "raw"]
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


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
