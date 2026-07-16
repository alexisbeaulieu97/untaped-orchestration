from __future__ import annotations

import pytest
from cyclopts.exceptions import CycloptsError

from untaped_orchestration.cli.options import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    OutputFormat,
    validate_format,
    validate_limit,
)


def test_orchestration_formats_exclude_yaml_and_limits_are_bounded() -> None:
    assert OutputFormat.__args__ == ("table", "json", "pipe", "raw")
    assert validate_format("json", allowed=("table", "json")) == "json"
    with pytest.raises(ValueError, match="yaml"):
        validate_format("yaml", allowed=("table", "json"))
    assert validate_limit(DEFAULT_LIMIT) == 50
    assert validate_limit(MAX_LIMIT) == 200
    for invalid in (0, 201):
        with pytest.raises(ValueError, match=r"1\.\.200"):
            validate_limit(invalid)


def test_shared_options_are_leaf_only_and_yaml_is_a_usage_error() -> None:
    from untaped_orchestration.cli import app

    with pytest.raises(CycloptsError):
        app.parse_args(["--store", ".", "list"], exit_on_error=False, print_error=False)
    with pytest.raises(CycloptsError):
        app.parse_args(["list", "--format", "yaml"], exit_on_error=False, print_error=False)
    app.parse_args(
        ["list", "--store", ".", "--format", "json"],
        exit_on_error=False,
        print_error=False,
    )


@pytest.mark.parametrize(
    "argv",
    (
        ("id", "new", "task", "--format", "pipe"),
        ("brief", "--format", "raw"),
        ("trace", "tsk_019f0000000070008000000000000010", "--format", "pipe"),
        (
            "task",
            "create",
            "--id",
            "tsk_019f0000000070008000000000000010",
            "--title",
            "x",
            "--if-store-revision",
            f"sha256:{'a' * 64}",
            "--format",
            "raw",
        ),
        ("check", "--format", "pipe"),
    ),
)
def test_compound_and_nonrow_commands_reject_unlisted_formats(
    argv: tuple[str, ...],
) -> None:
    from untaped_orchestration.cli import app

    with pytest.raises(SystemExit) as raised:
        app(argv, exit_on_error=False)
    assert raised.value.code == 2
