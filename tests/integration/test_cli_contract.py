from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import untaped_orchestration.application.curation as curation_module
from tests.builders import CHILD_STORE_ID, DECISION_ID, STORE_ID, TASK_ID, task_bytes
from tests.cli_fixtures import initialized_repository
from untaped_orchestration.__main__ import main
from untaped_orchestration.application.maintenance import (
    RecursiveCheckResult,
    RecursiveFormatResult,
)
from untaped_orchestration.application.results import StoreLocation
from untaped_orchestration.cli import maintenance_commands
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def test_version_is_exact_without_store_or_profile(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["untaped-orchestration", "--version"])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == "0.1.0\n"
    assert captured.err == ""


def test_id_new_json_uses_stable_envelope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["untaped-orchestration", "id", "new", "decision", "--format", "json"],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert list(payload) == [
        "schema",
        "command",
        "complete",
        "truncated",
        "data",
        "diagnostics",
    ]
    assert payload["data"]["kind"] == "decision"
    assert payload["data"]["id"].startswith("dec_")
    assert captured.err == ""


def _invoke(monkeypatch, capsys, *tokens: str) -> tuple[dict[str, object], str]:
    monkeypatch.setattr("sys.argv", ["untaped-orchestration", *tokens])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 0
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def test_cli_list_is_header_bounded_and_show_reads_body_inside_lock(
    tmp_path, monkeypatch, capsys
) -> None:
    root = initialized_repository(tmp_path)
    task = root / "tasks" / f"{TASK_ID}-bounded.md"
    task.parent.mkdir()
    task.write_bytes(task_bytes().replace(b"Opaque Markdown body.", b"x" * 500_000))
    active = False
    original_acquire = FileLockManager.acquire
    original_read = FilesystemStoreRepository.read_item_body

    @contextmanager
    def tracked_acquire(self, locations, *, timeout):
        nonlocal active
        with original_acquire(self, locations, timeout=timeout):
            active = True
            try:
                yield
            finally:
                active = False

    def guarded_read(self, location, relative_path):
        assert active, "body read escaped federation lock lease"
        return original_read(self, location, relative_path)

    monkeypatch.setattr(FileLockManager, "acquire", tracked_acquire)
    monkeypatch.setattr(FilesystemStoreRepository, "read_item_body", guarded_read)

    listed, _ = _invoke(monkeypatch, capsys, "list", "--store", str(root), "--format", "json")
    assert listed["data"][0]["id"] == TASK_ID
    shown, _ = _invoke(
        monkeypatch,
        capsys,
        "show",
        TASK_ID,
        "--store",
        str(root),
        "--format",
        "json",
    )
    assert shown["data"]["body"].endswith("x" * 100 + "\n")


def test_cli_curate_next_uses_header_only_federation_lease(tmp_path, monkeypatch, capsys) -> None:
    root = initialized_repository(tmp_path)
    active = False
    full_loads: list[bool] = []
    original_acquire = FileLockManager.acquire
    original_load = FilesystemStoreRepository.load_local
    original_queue = curation_module.curation_queue

    @contextmanager
    def tracked_acquire(self, locations, *, timeout):
        nonlocal active
        with original_acquire(self, locations, timeout=timeout):
            active = True
            try:
                yield
            finally:
                active = False

    def tracked_load(self, location, *, headers_only):
        if not headers_only:
            full_loads.append(active)
        return original_load(self, location, headers_only=headers_only)

    def guarded_queue(*args, **kwargs):
        assert active, "curation queue escaped federation lock lease"
        return original_queue(*args, **kwargs)

    monkeypatch.setattr(FileLockManager, "acquire", tracked_acquire)
    monkeypatch.setattr(FilesystemStoreRepository, "load_local", tracked_load)
    monkeypatch.setattr(curation_module, "curation_queue", guarded_queue)

    payload, _ = _invoke(
        monkeypatch,
        capsys,
        "curate",
        "next",
        "--store",
        str(root),
        "--format",
        "json",
    )

    assert payload["complete"] is True
    assert full_loads == []


def test_real_store_read_mutation_and_maintenance_families(tmp_path, monkeypatch, capsys) -> None:
    root = initialized_repository(tmp_path)
    repository = FilesystemStoreRepository()
    revision = repository.load_local(
        location_from_root(root), headers_only=True
    ).store_revision.root

    created, stderr = _invoke(
        monkeypatch,
        capsys,
        "task",
        "create",
        "--id",
        TASK_ID,
        "--title",
        "CLI contract",
        "--if-store-revision",
        revision,
        "--store",
        str(root),
        "--format",
        "json",
    )
    assert created["command"] == "task create"
    assert created["data"]["applied"] is True
    assert stderr == ""

    listed, stderr = _invoke(
        monkeypatch,
        capsys,
        "list",
        "--store",
        str(root),
        "--format",
        "json",
    )
    assert listed["command"] == "list"
    assert listed["data"][0]["id"] == TASK_ID
    assert stderr == ""

    checked, stderr = _invoke(
        monkeypatch,
        capsys,
        "check",
        "--store",
        str(root),
        "--format",
        "json",
    )
    assert checked["command"] == "check"
    assert checked["data"][0]["valid"] is True
    assert stderr == ""


def test_check_invalid_store_keeps_results_and_exits_one(tmp_path, monkeypatch, capsys) -> None:
    root = initialized_repository(tmp_path)
    root.joinpath("views", "decisions.md").unlink()
    monkeypatch.setattr(
        "sys.argv",
        [
            "untaped-orchestration",
            "check",
            "--store",
            str(root),
            "--format",
            "json",
        ],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["data"][0]["valid"] is False
    assert payload["data"][0]["views_current"] is False
    assert captured.err == ""


def test_check_missing_child_warns_unless_children_are_required(
    tmp_path, monkeypatch, capsys
) -> None:
    root = initialized_repository(tmp_path)
    root.joinpath("registry.toml").write_text(
        f'''schema = "untaped.orchestration.registry/v1"
store_id = "{STORE_ID}"

[[children]]
id = "{CHILD_STORE_ID}"
path = "../../missing/.untaped/orchestration"
''',
        encoding="utf-8",
    )
    repository = FilesystemStoreRepository()
    location = location_from_root(root)
    snapshot = repository.load_local(location, headers_only=False)
    for path, content in MarkdownViewRenderer().expected(snapshot).items():
        target = root.joinpath(*path.parts)
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(content)

    warning, _ = _invoke(
        monkeypatch,
        capsys,
        "check",
        "--store",
        str(root),
        "--format",
        "json",
    )
    assert warning["complete"] is False
    assert {value["severity"] for value in warning["diagnostics"]} == {"warning"}

    monkeypatch.setattr(
        "sys.argv",
        [
            "untaped-orchestration",
            "check",
            "--require-children",
            "--store",
            str(root),
            "--format",
            "json",
        ],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 1
    required = json.loads(capsys.readouterr().out)
    assert {value["severity"] for value in required["diagnostics"]} == {"error"}


@pytest.mark.parametrize("fmt", ["json", "table"])
@pytest.mark.parametrize("local", [False, True], ids=["recursive", "local"])
@pytest.mark.parametrize("command", ["check", "fmt-check"])
def test_read_only_maintenance_orc007_result_exits_four_through_actual_cli(
    command: str,
    local: bool,
    fmt: str,
    monkeypatch,
    capsys,
) -> None:
    conflict = Diagnostic(
        code="ORC007",
        severity="error",
        path="registry.toml",
        field="revision",
        message="federation changed",
        hint="retry",
    )

    class Maintenance:
        def check(self, request):
            del request
            return RecursiveCheckResult(False, False, (), (conflict,))

        def fmt_check(self, request):
            del request
            return RecursiveFormatResult((), False, (conflict,))

    context = SimpleNamespace(
        location=StoreLocation(Path("/tmp/store"), Path("/tmp/store")),
        maintenance=lambda: Maintenance(),
    )
    monkeypatch.setattr(maintenance_commands.CliContext, "resolve", lambda store: context)
    tokens = ["check"] if command == "check" else ["fmt", "--check"]
    if local:
        tokens.append("--local")
    tokens.extend(("--format", fmt))
    monkeypatch.setattr("sys.argv", ["untaped-orchestration", *tokens])

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 4
    captured = capsys.readouterr()
    assert "ORC007" in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


def test_cli_binary_recovery_and_json_base64_matrix(tmp_path, monkeypatch, capfdbinary) -> None:
    root = initialized_repository(tmp_path)
    relative = f"decisions/{DECISION_ID}-broken.md"
    content = b"+++\nschema = nope\n+++\nbody\xff"
    target = root.joinpath(relative)
    target.parent.mkdir()
    target.write_bytes(content)

    monkeypatch.setattr(
        "sys.argv",
        [
            "untaped-orchestration",
            "show",
            DECISION_ID,
            "--raw",
            "--store",
            str(root),
        ],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 0
    binary = capfdbinary.readouterr()
    assert binary.out == content
    metadata = json.loads(binary.err)
    assert list(metadata) == ["schema", "path", "revision", "size"]
    assert metadata["path"] == relative
    assert metadata["size"] == len(content)

    monkeypatch.setattr(
        "sys.argv",
        [
            "untaped-orchestration",
            "show",
            DECISION_ID,
            "--raw",
            "--store",
            str(root),
            "--format",
            "json",
        ],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 0
    structured = capfdbinary.readouterr()
    payload = json.loads(structured.out)
    assert payload["data"]["content"] == base64.b64encode(content).decode("ascii")
    assert structured.err == b""

    monkeypatch.setattr(
        "sys.argv",
        [
            "untaped-orchestration",
            "show",
            DECISION_ID,
            "--raw",
            "--store",
            str(root),
            "--format",
            "table",
        ],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    rejected = capfdbinary.readouterr()
    assert rejected.out == b""
    assert b"not available" in rejected.err
