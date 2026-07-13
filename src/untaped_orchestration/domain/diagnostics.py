import posixpath
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

type DiagnosticCode = Literal[
    "ORC001",
    "ORC002",
    "ORC003",
    "ORC004",
    "ORC005",
    "ORC006",
    "ORC007",
    "ORC008",
    "ORC009",
]
type DiagnosticSeverity = Literal["error", "warning"]
type LocationSortPart = tuple[bool, int]
type DiagnosticSortKey = tuple[
    int,
    str,
    LocationSortPart,
    LocationSortPart,
    LocationSortPart,
    str,
    str,
    str,
    str,
]


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: DiagnosticCode
    severity: DiagnosticSeverity
    path: str
    field: str
    line: int | None = None
    column: int | None = None
    byte_offset: int | None = None
    message: str
    hint: str


class DiagnosticError(ValueError):
    """Caller-recoverable failure represented by exact public diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics or not all(isinstance(value, Diagnostic) for value in diagnostics):
            raise TypeError("diagnostic errors require a nonempty diagnostic tuple")
        self.diagnostics = diagnostics
        super().__init__("; ".join(value.message for value in diagnostics))


def expected_diagnostic(
    code: DiagnosticCode,
    message: str,
    *,
    path: str = "",
    field: str = "",
    hint: str = "Correct the reported condition and retry.",
) -> tuple[Diagnostic, ...]:
    return (
        Diagnostic(
            code=code,
            severity="error",
            path=path,
            field=field,
            message=message,
            hint=hint,
        ),
    )


def validation_diagnostic(
    error: ValidationError,
    code: DiagnosticCode,
    *,
    message_prefix: str,
    path: str = "",
    field: str | None = None,
) -> tuple[Diagnostic, ...]:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return expected_diagnostic(
        code,
        f"{message_prefix}: {first['msg']}",
        path=path,
        field=location if field is None else field,
    )


def _location_part(value: int | None) -> LocationSortPart:
    return (value is None, value if value is not None else 0)


def normalized_diagnostic_path(path: str) -> str:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    return "" if normalized == "." else normalized


def diagnostic_sort_key(diagnostic: Diagnostic) -> DiagnosticSortKey:
    return (
        0 if diagnostic.severity == "error" else 1,
        normalized_diagnostic_path(diagnostic.path),
        _location_part(diagnostic.line),
        _location_part(diagnostic.column),
        _location_part(diagnostic.byte_offset),
        diagnostic.field,
        diagnostic.code,
        diagnostic.message,
        diagnostic.hint,
    )


def sort_diagnostics(diagnostics: Iterable[Diagnostic]) -> tuple[Diagnostic, ...]:
    return tuple(sorted(diagnostics, key=diagnostic_sort_key))
