import pytest
from pydantic import ValidationError

from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics


def diagnostic(
    code: str,
    *,
    severity: str = "error",
    path: str = "tasks/item.md",
    field: str = "title",
    line: int | None = None,
    column: int | None = None,
    byte_offset: int | None = None,
    message: str = "invalid value",
    hint: str = "fix it",
) -> Diagnostic:
    return Diagnostic.model_validate(
        {
            "code": code,
            "severity": severity,
            "path": path,
            "field": field,
            "line": line,
            "column": column,
            "byte_offset": byte_offset,
            "message": message,
            "hint": hint,
        }
    )


@pytest.mark.parametrize("code", [f"ORC00{number}" for number in range(1, 10)])
def test_diagnostic_accepts_every_stable_code(code: str) -> None:
    assert diagnostic(code).code == code


@pytest.mark.parametrize(
    ("field", "value"),
    [("code", "ORC010"), ("severity", "info"), ("extra", True)],
)
def test_diagnostic_forbids_unknown_codes_severities_and_fields(field: str, value: object) -> None:
    data: dict[str, object] = {
        "code": "ORC001",
        "severity": "error",
        "path": "tasks/item.md",
        "field": "title",
        "message": "invalid value",
        "hint": "fix it",
    }
    data[field] = value

    with pytest.raises(ValidationError):
        Diagnostic.model_validate(data)


def test_diagnostic_is_frozen() -> None:
    value = diagnostic("ORC001")

    with pytest.raises(ValidationError, match="frozen"):
        value.message = "changed"  # type: ignore[misc]


def test_diagnostic_order_is_severity_normalized_path_location_field_then_code() -> None:
    values = [
        diagnostic("ORC009", severity="warning", path="a/item.md", line=1),
        diagnostic("ORC008", path="z/item.md", line=1),
        diagnostic("ORC006", path="a/other.md", field="stage"),
        diagnostic("ORC003", path="./a/dir/../item.md", field="id", line=2, column=4),
        diagnostic("ORC002", path="a/item.md", field="id", line=2, column=4),
        diagnostic("ORC001", path="a/item.md", field="schema", line=2, column=4),
    ]

    ordered = sort_diagnostics(reversed(values))

    assert [value.code for value in ordered] == [
        "ORC002",
        "ORC003",
        "ORC001",
        "ORC006",
        "ORC008",
        "ORC009",
    ]


def test_diagnostic_order_has_stable_message_and_hint_tiebreakers() -> None:
    later = diagnostic("ORC002", message="z message", hint="a hint")
    earlier = diagnostic("ORC002", message="a message", hint="z hint")

    assert sort_diagnostics([later, earlier]) == (earlier, later)
