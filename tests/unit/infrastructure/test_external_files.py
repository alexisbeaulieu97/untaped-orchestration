from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from untaped_orchestration.domain.diagnostics import DiagnosticError


def _external_module():
    return importlib.import_module("untaped_orchestration.infrastructure.external_files")


def _reader(**kwargs):
    return _external_module().FilesystemExternalFileReader(**kwargs)


def _diagnostic(error: DiagnosticError):
    return error.diagnostics[0]


def test_external_reader_accepts_exact_limit_and_rejects_limit_plus_one(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    path.write_bytes(b"x" * 16)
    assert _reader().read_external(path, limit=16, field="body") == b"x" * 16

    path.write_bytes(b"x" * 17)
    with pytest.raises(DiagnosticError) as captured:
        _reader().read_external(path, limit=16, field="body")

    assert _diagnostic(captured.value).code == "ORC001"
    assert _diagnostic(captured.value).field == "body"


def test_external_reader_rejects_growth_past_bound_during_read(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    path.write_bytes(b"small")

    def grow(event: str, opened: Path) -> None:
        if event == "after-stat":
            opened.write_bytes(b"x" * 18)

    with pytest.raises(DiagnosticError) as captured:
        _reader(event_hook=grow).read_external(path, limit=16, field="body")

    assert _diagnostic(captured.value).code == "ORC001"


@pytest.mark.parametrize("force_fallback", [False, True])
def test_external_reader_rejects_growth_past_bound_after_eof(
    tmp_path: Path,
    force_fallback: bool,
) -> None:
    path = tmp_path / "body.md"
    path.write_bytes(b"small")

    def grow(event: str, opened: Path) -> None:
        if event == "after-read":
            with opened.open("ab") as stream:
                stream.write(b"x" * 18)

    with pytest.raises(DiagnosticError) as captured:
        _reader(force_fallback=force_fallback, event_hook=grow).read_external(
            path,
            limit=16,
            field="body",
        )

    assert _diagnostic(captured.value).code == "ORC001"


@pytest.mark.parametrize("force_fallback", [False, True])
@pytest.mark.parametrize("descriptor_change", ["identity", "file-type"])
def test_external_reader_rejects_post_read_descriptor_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_fallback: bool,
    descriptor_change: str,
) -> None:
    module = _external_module()
    path = tmp_path / "body.md"
    path.write_bytes(b"body")
    after_read = False
    real_fstat = module.os.fstat

    def mark_after_read(event: str, _opened: Path) -> None:
        nonlocal after_read
        if event == "after-read":
            after_read = True

    def changed_fstat(descriptor: int):
        value = real_fstat(descriptor)
        if not after_read:
            return value
        fields = list(value)
        if descriptor_change == "identity":
            fields[1] += 1
        else:
            fields[0] = (fields[0] & ~0o170000) | 0o010000
        return os.stat_result(fields)

    monkeypatch.setattr(module.os, "fstat", changed_fstat)

    with pytest.raises(DiagnosticError) as captured:
        _reader(force_fallback=force_fallback, event_hook=mark_after_read).read_external(
            path,
            limit=16,
            field="body",
        )

    assert _diagnostic(captured.value).code == "ORC003"


@pytest.mark.parametrize("component", ("final", "ancestor"))
def test_external_reader_rejects_symlink_components(tmp_path: Path, component: str) -> None:
    real = tmp_path / "real"
    real.mkdir()
    source = real / "body.md"
    source.write_bytes(b"body")
    if component == "final":
        candidate = tmp_path / "body.md"
        candidate.symlink_to(source)
    else:
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)
        candidate = linked / "body.md"

    with pytest.raises(DiagnosticError) as captured:
        _reader().read_external(candidate, limit=16, field="body")

    assert _diagnostic(captured.value).code == "ORC003"


def test_external_reader_detects_a_final_path_substitution(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    replacement = tmp_path / "replacement.md"
    path.write_bytes(b"original")
    replacement.write_bytes(b"replacement")

    def swap(event: str, opened: Path) -> None:
        if event == "after-open":
            os.replace(replacement, opened)

    with pytest.raises(DiagnosticError) as captured:
        _reader(event_hook=swap).read_external(path, limit=16, field="body")

    assert _diagnostic(captured.value).code == "ORC003"
    assert "original" not in str(captured.value)
    assert "replacement" not in str(captured.value)


def test_cooperative_fallback_detects_substitution(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    replacement = tmp_path / "replacement.md"
    path.write_bytes(b"original")
    replacement.write_bytes(b"replacement")

    def swap(event: str, opened: Path) -> None:
        if event == "after-open":
            os.replace(replacement, opened)

    with pytest.raises(DiagnosticError, match="changed while being read"):
        _reader(force_fallback=True, event_hook=swap).read_external(
            path,
            limit=16,
            field="body",
        )


def test_external_reader_rejects_nonregular_inputs_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "body.fifo"
    os.mkfifo(fifo)
    with pytest.raises(DiagnosticError) as fifo_error:
        _reader().read_external(fifo, limit=16, field="body")
    assert _diagnostic(fifo_error.value).code == "ORC003"

    device = Path("/dev/null")
    if not device.exists():
        pytest.skip("platform does not expose /dev/null")
    with pytest.raises(DiagnosticError) as device_error:
        _reader().read_external(device, limit=16, field="body")
    assert _diagnostic(device_error.value).code == "ORC003"


def test_external_reader_returns_one_immutable_binary_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    path.write_bytes(b"private-secret\xff")

    snapshot = _reader().read_external(path, limit=32, field="body")
    path.write_bytes(b"changed")

    assert snapshot == b"private-secret\xff"
    assert isinstance(snapshot, bytes)


def test_central_item_limit_includes_both_delimiters() -> None:
    limits = importlib.import_module("untaped_orchestration.domain.limits")

    assert limits.FRONTMATTER_LIMIT == 64 * 1024
    assert limits.BODY_LIMIT == 1024 * 1024
    assert len(b"+++\n") * 2 == limits.DELIMITER_OVERHEAD
    assert limits.ITEM_FILE_LIMIT == (
        limits.FRONTMATTER_LIMIT + limits.BODY_LIMIT + limits.DELIMITER_OVERHEAD
    )
