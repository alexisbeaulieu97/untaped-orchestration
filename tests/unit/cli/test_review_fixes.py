from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.builders import TASK_ID
from untaped_orchestration.cli import app, maintenance_commands, read_commands, task_commands
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.domain.diagnostics import DiagnosticError


@pytest.mark.parametrize(
    ("module", "argv"),
    (
        (read_commands, ("list", "--format", "raw", "-c", "title")),
        (
            task_commands,
            (
                "task",
                "review",
                TASK_ID,
                "--force-current",
                "-c",
                "applied",
            ),
        ),
        (maintenance_commands, ("check", "-c", "valid")),
    ),
)
def test_columns_short_alias_reaches_representative_command_families(
    monkeypatch, module, argv: tuple[str, ...]
) -> None:
    seen: list[tuple[str, ...]] = []

    def run(command, action, *, columns=(), **kwargs) -> None:
        del command, action, kwargs
        seen.append(columns)

    monkeypatch.setattr(module, "run_command", run)
    with pytest.raises(SystemExit) as raised:
        app(argv, exit_on_error=False)
    assert raised.value.code == 0
    assert seen == [(argv[-1],)]


def test_columns_short_alias_is_repeatable(monkeypatch) -> None:
    seen: list[tuple[str, ...]] = []

    def run(command, action, *, columns=(), **kwargs) -> None:
        del command, action, kwargs
        seen.append(columns)

    monkeypatch.setattr(read_commands, "run_command", run)
    with pytest.raises(SystemExit) as raised:
        app(
            ("list", "--format", "raw", "-c", "title", "-c", "rank"),
            exit_on_error=False,
        )
    assert raised.value.code == 0
    assert seen == [("title", "rank")]


@pytest.mark.parametrize("module_name", ("task_commands", "decision_commands"))
def test_body_file_read_is_bounded_to_limit_plus_one(
    tmp_path: Path,
    monkeypatch,
    module_name: str,
) -> None:
    module = __import__(f"untaped_orchestration.cli.{module_name}", fromlist=["_body"])
    body = tmp_path / "body.md"
    body.write_bytes(b"x")
    original = Path.open
    sizes: list[int] = []

    class TrackingReader:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            self.wrapped.close()

        def read(self, size: int = -1) -> bytes:
            sizes.append(size)
            return self.wrapped.read(size)

    def tracking_open(path: Path, *args, **kwargs):
        return TrackingReader(original(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", tracking_open)
    assert module._body(body) == b"x"
    assert sizes == [1024 * 1024 + 1]


@pytest.mark.parametrize("module_name", ("task_commands", "decision_commands"))
def test_body_file_rejects_oversize_and_invalid_utf8(
    tmp_path: Path,
    module_name: str,
) -> None:
    module = __import__(f"untaped_orchestration.cli.{module_name}", fromlist=["_body"])
    body = tmp_path / "body.md"
    body.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(DiagnosticError, match="1 MiB") as oversized:
        module._body(body)
    assert oversized.value.diagnostics[0].code == "ORC001"
    body.write_bytes(b"\xff")
    with pytest.raises(DiagnosticError, match="UTF-8") as invalid:
        module._body(body)
    assert invalid.value.diagnostics[0].code == "ORC001"


def test_invalid_body_file_uses_typed_command_failure_before_service(
    tmp_path: Path,
    monkeypatch,
    capfd,
) -> None:
    body = tmp_path / "body.md"
    body.write_bytes(b"\xff")

    class Context:
        scope = object()

        def create_task(self):
            raise AssertionError("invalid body input must not call the service")

    monkeypatch.setattr(CliContext, "resolve", classmethod(lambda cls, store: Context()))
    with pytest.raises(SystemExit) as raised:
        app(
            (
                "task",
                "create",
                "--id",
                TASK_ID,
                "--title",
                "Task",
                "--body-file",
                str(body),
                "--if-store-revision",
                f"sha256:{'a' * 64}",
                "--format",
                "json",
            ),
            exit_on_error=False,
        )

    assert raised.value.code == 1
    payload = json.loads(capfd.readouterr().out)
    assert payload["diagnostics"][0]["code"] == "ORC001"
