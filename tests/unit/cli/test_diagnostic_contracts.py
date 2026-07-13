from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import DECISION_ID
from untaped_orchestration.application.bootstrap import InitConflictError
from untaped_orchestration.application.federation import (
    RegistryMutationConflict,
    RegistryRevisionConflict,
    UnidentifiedStoreError,
)
from untaped_orchestration.application.import_operations import ImportConflict
from untaped_orchestration.application.item_support import (
    CreateConflict,
    ItemStateConflict,
    RelationConflict,
    RevisionConflict,
)
from untaped_orchestration.application.maintenance import (
    RevisionConflict as MaintenanceRevisionConflict,
)
from untaped_orchestration.application.mutations import InvalidMutationState
from untaped_orchestration.application.queries import QueryIncompleteError, RawAmbiguityError
from untaped_orchestration.application.repair_operations import RepairConflict
from untaped_orchestration.application.results import StoreLocation
from untaped_orchestration.cli import app
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.cli.output import run_command
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.infrastructure.codec import CodecError
from untaped_orchestration.infrastructure.filesystem import PathSafetyError, StoreNotFoundError

REVISION = f"sha256:{'b' * 64}"


def _diagnostic(code: str, message: str = "expected failure") -> Diagnostic:
    return Diagnostic.model_validate(
        {
            "code": code,
            "severity": "error",
            "path": "store.toml",
            "field": "field",
            "message": message,
            "hint": "correct the input",
        }
    )


def _run_failure(error: Exception, fmt: str, capfd, *, binary: bool = False):
    with pytest.raises(SystemExit) as raised:
        run_command(
            "brief" if isinstance(error, StoreNotFoundError) else "show",
            lambda: (_ for _ in ()).throw(error),
            fmt=fmt,  # type: ignore[arg-type]
            allowed=(fmt,),  # type: ignore[arg-type]
            binary_recovery=binary,
        )
    return raised.value.code, capfd.readouterr()


@pytest.mark.parametrize("fmt", ("json", "table"))
def test_missing_store_brief_failure_uses_default_ceiling(fmt: str, capfd) -> None:
    exit_code, captured = _run_failure(StoreNotFoundError("missing store"), fmt, capfd)
    assert exit_code == 1
    assert "ORC003" in captured.out + captured.err
    assert "invalid max_total_bytes" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("error", "code", "exit_code"),
    (
        (ItemStateConflict("retire_note must be nonempty"), "ORC006", 1),
        (PathSafetyError(Path("unsafe"), "path escapes store"), "ORC003", 1),
        (RevisionConflict("stale revision"), "ORC007", 4),
        (RelationConflict("invalid relation"), "ORC004", 1),
        (RegistryMutationConflict("invalid federation"), "ORC005", 3),
        (InvalidMutationState((_diagnostic("ORC009"),)), "ORC009", 1),
    ),
)
def test_expected_failure_families_keep_orc_meaning(
    error: Exception,
    code: str,
    exit_code: int,
    capfd,
) -> None:
    actual, captured = _run_failure(error, "json", capfd)
    assert actual == exit_code
    payload = json.loads(captured.out)
    assert payload["diagnostics"][0]["code"] == code


@pytest.mark.parametrize("code", ("ORC001", "ORC002"))
def test_codec_error_exposes_its_exact_diagnostic_tuple(code: str) -> None:
    diagnostic = _diagnostic(code)
    error = CodecError(diagnostic)
    assert error.diagnostics == (diagnostic,)
    assert error.diagnostics[0].path == "store.toml"
    assert error.diagnostics[0].field == "field"


def test_view_failure_is_typed_orc008() -> None:
    from untaped_orchestration.infrastructure import views

    error_type = views.ViewError
    error = error_type("view rendering failed")
    assert error.diagnostics[0].code == "ORC008"


@pytest.mark.parametrize(
    ("error", "code"),
    (
        (InitConflictError("unsafe init target"), "ORC003"),
        (CreateConflict("existing item identity conflicts"), "ORC003"),
        (RegistryRevisionConflict("stale registry"), "ORC007"),
        (RegistryMutationConflict("invalid federation"), "ORC005"),
        (
            UnidentifiedStoreError(StoreLocation(Path("store"), Path("store"))),
            "ORC003",
        ),
        (MaintenanceRevisionConflict("stale store"), "ORC007"),
        (QueryIncompleteError((_diagnostic("ORC005"),)), "ORC005"),
        (RawAmbiguityError(("tasks/one.md", "tasks/two.md")), "ORC003"),
    ),
)
def test_caller_reachable_expected_families_are_typed(error: Exception, code: str) -> None:
    assert error.diagnostics
    assert error.diagnostics[0].code == code


@pytest.mark.parametrize("error_type", (ImportConflict, RepairConflict))
def test_recovery_conflicts_accept_exact_diagnostics(error_type) -> None:
    diagnostics = (_diagnostic("ORC001"), _diagnostic("ORC003"))
    error = error_type("contextual recovery failure", diagnostics)
    assert error.diagnostics is diagnostics


def test_query_resolution_failure_is_typed_orc003() -> None:
    from untaped_orchestration.application import queries

    error_type = queries.QueryResolutionError
    error = error_type("item does not resolve uniquely")
    assert error.diagnostics[0].code == "ORC003"


@pytest.mark.parametrize("error", (ValueError("secret value"), OSError("secret path")))
def test_unknown_failures_are_redacted_and_exit_five(error: Exception, capfd) -> None:
    exit_code, captured = _run_failure(error, "json", capfd)
    assert exit_code == 5
    assert "secret" not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_unknown_failure_trace_appears_only_under_debug(monkeypatch, capfd) -> None:
    monkeypatch.setattr(sys, "argv", ["untaped-orchestration", "show", "--debug"])
    exit_code, captured = _run_failure(RuntimeError("debug detail"), "json", capfd)
    assert exit_code == 5
    assert "debug detail" not in captured.out
    assert "Traceback" in captured.err
    assert "debug detail" in captured.err


@pytest.mark.parametrize("fmt", ("table", "json", "pipe"))
def test_expected_failure_shapes_are_preserved(fmt: str, capfd) -> None:
    exit_code, captured = _run_failure(RelationConflict("invalid relation"), fmt, capfd)
    assert exit_code == 1
    if fmt == "json":
        payload = json.loads(captured.out)
        assert payload["data"] == {}
        assert payload["diagnostics"][0]["code"] == "ORC004"
    elif fmt == "pipe":
        records = [json.loads(line) for line in captured.out.splitlines()]
        assert [record["kind"] for record in records] == [
            "orchestration.diagnostic",
            "orchestration.status",
        ]
    else:
        assert captured.out == ""
        assert captured.err.startswith("ORC004:")


def test_binary_recovery_expected_failure_writes_zero_stdout(capfd) -> None:
    exit_code, captured = _run_failure(
        PathSafetyError(PurePosixPath("tasks/bad.md"), "unsafe raw path"),
        "raw",
        capfd,
        binary=True,
    )
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("ORC003:")


@pytest.mark.parametrize("force_current", (False, True))
def test_duplicate_predecessors_are_rejected_before_context_resolution(
    force_current: bool,
    monkeypatch,
    capsys,
) -> None:
    calls = 0

    def unexpected_resolve(cls, store):
        nonlocal calls
        calls += 1
        raise AssertionError("context resolution must not run")

    monkeypatch.setattr(CliContext, "resolve", classmethod(unexpected_resolve))
    argv = [
        "decision",
        "supersede",
        "--id",
        DECISION_ID,
        "--title",
        "Replacement",
        "--predecessor",
        DECISION_ID,
        "--predecessor",
        DECISION_ID,
    ]
    if force_current:
        argv.append("--force-current")
    else:
        argv.extend(
            (
                "--if-store-revision",
                REVISION,
                "--if-predecessor-revision",
                f"{DECISION_ID}={REVISION}",
            )
        )

    with pytest.raises(SystemExit) as raised:
        app(argv, exit_on_error=False)

    assert raised.value.code == 2
    assert calls == 0
    assert "Traceback" not in capsys.readouterr().err


def test_diagnostic_error_carries_an_exact_tuple() -> None:
    from untaped_orchestration.domain import diagnostics

    error_type = diagnostics.DiagnosticError
    values = (_diagnostic("ORC007"), _diagnostic("ORC005"))
    error = error_type(values)
    assert error.diagnostics is values
