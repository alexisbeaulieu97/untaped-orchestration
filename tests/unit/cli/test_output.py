from __future__ import annotations

import base64
import json
from pathlib import PurePosixPath

import pytest

from tests.builders import TASK_ID
from untaped_orchestration.application.item_support import RevisionConflict
from untaped_orchestration.application.queries import QueryIncompleteError
from untaped_orchestration.application.results import RawRecord
from untaped_orchestration.cli.output import (
    CommandResult,
    encode_binary_recovery,
    encode_result,
    run_command,
)
from untaped_orchestration.domain.curation import CurationEntry, CurationKind
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.ids import StoreId, TaskId
from untaped_orchestration.domain.models import Revision
from untaped_orchestration.domain.time import CalendarDate

REVISION = Revision(f"sha256:{'a' * 64}")


def test_json_envelope_has_exact_order_and_expected_failure_shape() -> None:
    diagnostic = Diagnostic(
        code="ORC007",
        severity="error",
        path="store.toml",
        field="revision",
        message="stale",
        hint="reread",
    )
    encoded = encode_result(
        CommandResult("next", [], complete=False, truncated=False, diagnostics=(diagnostic,)),
        fmt="json",
    )
    payload = json.loads(encoded.stdout)
    assert list(payload) == [
        "schema",
        "command",
        "complete",
        "truncated",
        "data",
        "diagnostics",
    ]
    assert payload["schema"] == "untaped.orchestration.output/v1"
    assert payload["data"] == []
    assert payload["diagnostics"][0]["code"] == "ORC007"
    assert encoded.stderr == b""


def test_pipe_preserves_exact_sdk_v1_envelope_and_ignores_columns() -> None:
    result = CommandResult(
        "list",
        [{"id": TASK_ID, "title": "Task"}],
        pipe_kind="orchestration.task",
    )
    encoded = encode_result(result, fmt="pipe", columns=("title",))
    assert encoded.stdout == (
        b'{"untaped":"1","kind":"orchestration.task","record":'
        + f'{{"id":"{TASK_ID}","title":"Task"}}}}'.encode()
        + b'\n{"untaped":"1","kind":"orchestration.status","record":'
        + b'{"complete":true,"truncated":false}}\n'
    )


def test_pipe_uses_each_mixed_item_rows_own_kind() -> None:
    encoded = encode_result(
        CommandResult(
            "list",
            [
                {"id": TASK_ID, "kind": "task"},
                {"id": "dec_019f0000000070008000000000000001", "kind": "decision"},
            ],
        ),
        fmt="pipe",
    )
    records = [json.loads(line) for line in encoded.stdout.splitlines()]
    assert [record["kind"] for record in records] == [
        "orchestration.task",
        "orchestration.decision",
        "orchestration.status",
    ]


def test_curation_rows_project_stable_id_and_dynamic_pipe_kind() -> None:
    entry = CurationEntry(
        StoreId("sto_019f0000000070008000000000000000"),
        CurationKind.TASK,
        TaskId(TASK_ID),
        CalendarDate("2026-07-12"),
    )
    raw = encode_result(CommandResult("curate next", [entry]), fmt="raw")
    assert raw.stdout == f"{TASK_ID}\n".encode()
    pipe = encode_result(CommandResult("curate next", [entry]), fmt="pipe")
    assert [json.loads(line)["kind"] for line in pipe.stdout.splitlines()] == [
        "orchestration.task",
        "orchestration.status",
    ]


def test_pipe_emits_data_and_diagnostic_records_for_partial_results() -> None:
    diagnostic = Diagnostic(
        code="ORC005",
        severity="warning",
        path="registry.toml",
        field="children",
        message="child missing",
        hint="restore child",
    )
    encoded = encode_result(
        CommandResult(
            "list",
            [{"id": TASK_ID, "kind": "task"}],
            complete=False,
            diagnostics=(diagnostic,),
        ),
        fmt="pipe",
    )
    records = [json.loads(line) for line in encoded.stdout.splitlines()]
    assert [record["kind"] for record in records] == [
        "orchestration.task",
        "orchestration.diagnostic",
        "orchestration.status",
    ]
    assert records[1]["record"]["code"] == "ORC005"
    Diagnostic.model_validate(records[1]["record"])
    assert (
        encoded.stdout
        == (
            f'{{"untaped":"1","kind":"orchestration.task","record":{{"id":"{TASK_ID}","kind":"task"}}}}\n'
            '{"untaped":"1","kind":"orchestration.diagnostic","record":'
            '{"code":"ORC005","severity":"warning","path":"registry.toml",'
            '"field":"children","message":"child missing","hint":"restore child"}}\n'
            '{"untaped":"1","kind":"orchestration.status","record":'
            '{"complete":false,"truncated":false}}\n'
        ).encode()
    )


@pytest.mark.parametrize(
    ("complete", "truncated"),
    (
        (True, False),
        (True, True),
        (False, False),
        (False, True),
    ),
)
def test_pipe_always_ends_with_exactly_one_status_trailer(complete: bool, truncated: bool) -> None:
    encoded = encode_result(
        CommandResult(
            "list",
            [{"id": TASK_ID, "kind": "task"}],
            complete=complete,
            truncated=truncated,
        ),
        fmt="pipe",
    )
    records = [json.loads(line) for line in encoded.stdout.splitlines()]
    assert [record["kind"] for record in records] == [
        "orchestration.task",
        "orchestration.status",
    ]
    assert records[-1]["record"] == {
        "complete": complete,
        "truncated": truncated,
    }
    assert sum(record["kind"] == "orchestration.status" for record in records) == 1


def test_pipe_expected_failure_is_diagnostic_only_without_traceback(capfd) -> None:
    def fail() -> CommandResult:
        raise ValueError("invalid canonical state")

    with pytest.raises(SystemExit) as raised:
        run_command("list", fail, fmt="pipe", allowed=("pipe",))
    assert raised.value.code == 1
    captured = capfd.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert [record["kind"] for record in records] == [
        "orchestration.diagnostic",
        "orchestration.status",
    ]
    Diagnostic.model_validate(records[0]["record"])
    assert records[1]["record"] == {"complete": False, "truncated": False}
    assert "Traceback" not in captured.err


def test_binary_and_json_recovery_preserve_invalid_utf8_exactly() -> None:
    content = b"body\xff"
    raw = RawRecord(PurePosixPath(f"tasks/{TASK_ID}-broken.md"), REVISION, len(content), content)
    binary = encode_binary_recovery(raw)
    assert binary.stdout == content
    assert binary.stderr == (
        b'{"schema":"untaped.orchestration.raw-meta/v1","path":"tasks/'
        + TASK_ID.encode()
        + b'-broken.md","revision":"sha256:'
        + b"a" * 64
        + b'","size":5}\n'
    )
    structured = encode_result(CommandResult("show", raw), fmt="json")
    payload = json.loads(structured.stdout)
    assert payload["data"]["encoding"] == "base64"
    assert payload["data"]["content"] == base64.b64encode(content).decode()


def test_brief_serialization_never_exceeds_32768_encoded_bytes() -> None:
    data = {
        "store_id": "sto_019f0000000070008000000000000000",
        "store_revision": REVISION.root,
        "pinned_decisions": [
            {"id": f"dec_{index:032x}", "title": '"\\雪' * 2000, "body": "雪" * 5000}
            for index in range(10)
        ],
        "ready": [{"id": TASK_ID, "title": "雪" * 5000}] * 10,
        "diagnostic_count": 1000,
        "missing_store_count": 1000,
    }
    for fmt in ("json", "table"):
        encoded = encode_result(CommandResult("brief", data), fmt=fmt)
        assert len(encoded.stdout) <= 32768
        assert encoded.truncated is True
        if fmt == "json":
            assert json.loads(encoded.stdout)["truncated"] is True


@pytest.mark.parametrize(
    ("error", "exit_code"),
    (
        (ValueError("invalid canonical state"), 1),
        (
            QueryIncompleteError(
                (
                    Diagnostic(
                        code="ORC005",
                        severity="error",
                        path="registry.toml",
                        field="children",
                        message="missing child",
                        hint="restore child",
                    ),
                )
            ),
            3,
        ),
        (RevisionConflict("stale revision"), 4),
        (OSError("disk failed"), 5),
    ),
)
def test_expected_failures_keep_json_envelope_and_stable_exit(
    error: Exception,
    exit_code: int,
    capfd,
) -> None:
    def fail() -> CommandResult:
        raise error

    with pytest.raises(SystemExit) as raised:
        run_command("next", fail, fmt="json", allowed=("json",))
    assert raised.value.code == exit_code
    captured = capfd.readouterr()
    payload = json.loads(captured.out)
    assert payload["command"] == "next"
    assert payload["complete"] is False
    assert payload["data"] == {}
    assert payload["diagnostics"]
    assert captured.err == ""


def test_table_failure_emits_no_data_and_diagnostic_on_stderr(capfd) -> None:
    def fail() -> CommandResult:
        raise ValueError("invalid canonical state")

    with pytest.raises(SystemExit) as raised:
        run_command("check", fail, fmt="table", allowed=("table",))
    assert raised.value.code == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "invalid canonical state" in captured.err


def test_binary_recovery_failure_writes_zero_stdout_bytes(capfd) -> None:
    def fail() -> CommandResult:
        raise ValueError("raw file is ambiguous")

    with pytest.raises(SystemExit) as raised:
        run_command(
            "show",
            fail,
            fmt="raw",
            allowed=("raw", "json"),
            binary_recovery=True,
        )
    assert raised.value.code == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "raw file is ambiguous" in captured.err


def test_raw_projection_preserves_falsey_values() -> None:
    encoded = encode_result(
        CommandResult("list", [{"id": TASK_ID, "rank": 0, "blocked": False}]),
        fmt="raw",
        columns=("rank", "blocked"),
    )
    assert encoded.stdout == f"{TASK_ID}\t0\tFalse\n".encode()


def test_unexpected_trace_is_emitted_only_with_debug(monkeypatch, capfd) -> None:
    def fail() -> CommandResult:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("sys.argv", ["untaped-orchestration", "check"])
    with pytest.raises(SystemExit) as raised:
        run_command("check", fail, fmt="json", allowed=("json",))
    assert raised.value.code == 5
    assert "Traceback" not in capfd.readouterr().err

    monkeypatch.setattr("sys.argv", ["untaped-orchestration", "check", "--debug"])
    with pytest.raises(SystemExit) as raised:
        run_command("check", fail, fmt="json", allowed=("json",))
    assert raised.value.code == 5
    assert "Traceback" in capfd.readouterr().err
