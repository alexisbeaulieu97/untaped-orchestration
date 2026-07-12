from __future__ import annotations

import json
import os
import site
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE_ID = "sto_019f0000000070008000000000000000"


@dataclass(frozen=True)
class InstalledWheel:
    console: Path
    python: Path
    wheel: Path


def _run(*args: str | Path, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "UV_CACHE_DIR": str(REPO_ROOT / ".uv-cache")},
    )


@pytest.fixture(scope="module")
def installed_wheel(tmp_path_factory: pytest.TempPathFactory) -> InstalledWheel:
    root = tmp_path_factory.mktemp("installed-wheel")
    artifacts = root / "artifacts"
    artifacts.mkdir()
    assert artifacts != REPO_ROOT / "dist"
    assert not list(artifacts.iterdir())

    _run("uv", "build", "--wheel", "--out-dir", artifacts, "--no-sources", cwd=REPO_ROOT)
    wheels = list(artifacts.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    assert wheel.parent == artifacts

    venv = root / "venv"
    _run("uv", "venv", "--python", "3.14", venv)
    python = venv / "bin" / "python"
    _run("uv", "pip", "install", "--no-deps", "--python", python, wheel)

    # The sandbox has no registry access. Reuse only the already-synced runtime
    # dependencies; the package under test still comes from the exact fresh wheel.
    fresh_site = Path(
        _run(python, "-c", "import site; print(site.getsitepackages()[0])").stdout.decode().strip()
    )
    dependency_site = Path(site.getsitepackages()[0]).resolve()
    (fresh_site / "orchestration-test-dependencies.pth").write_text(
        f"{dependency_site}\n", encoding="utf-8"
    )
    console = venv / "bin" / "untaped-orchestration"
    assert console.is_file()
    assert venv in console.parents
    return InstalledWheel(console, python, wheel)


def test_installed_wheel_console_and_store_smoke(
    installed_wheel: InstalledWheel, tmp_path: Path
) -> None:
    help_result = _run(installed_wheel.console, "--help")
    assert help_result.stdout.startswith(b"Usage: untaped-orchestration")
    assert _run(installed_wheel.console, "--version").stdout == b"0.1.0\n"

    repository = tmp_path / "repository"
    repository.mkdir()
    initialized = _run(
        installed_wheel.console,
        "init",
        repository,
        "--store-id",
        STORE_ID,
        "--name",
        "Installed wheel acceptance",
        "--timezone",
        "UTC",
        "--format",
        "json",
    )
    assert json.loads(initialized.stdout)["data"]["applied"] is True

    for command in (
        ("check", "--local", "--format", "json"),
        ("fmt", "--check", "--local", "--format", "json"),
        ("render", "--check", "--format", "json"),
    ):
        result = _run(installed_wheel.console, *command, cwd=repository)
        assert json.loads(result.stdout)["complete"] is True


def test_installed_wheel_contains_skill_and_typing_marker_only(
    installed_wheel: InstalledWheel,
) -> None:
    script = f"""
from importlib.resources import files
from pathlib import Path
import sys

import untaped_orchestration

package = files("untaped_orchestration")
module_path = Path(untaped_orchestration.__file__).resolve()
assert Path(sys.prefix).resolve() in module_path.parents
assert Path({str(REPO_ROOT)!r}).resolve() not in module_path.parents
assert {str(REPO_ROOT / ".venv")!r} not in str(module_path)
skill = package.joinpath("skills", "untaped-orchestration", "SKILL.md")
assert skill.is_file()
assert "brief --format json" in skill.read_text(encoding="utf-8")
assert package.joinpath("py.typed").is_file()
"""
    _run(installed_wheel.python, "-c", script)

    with zipfile.ZipFile(installed_wheel.wheel) as archive:
        names = archive.namelist()
    assert not any(".untaped/" in name for name in names)
    assert any(name.endswith("/py.typed") for name in names)
    assert any(name.endswith("/skills/untaped-orchestration/SKILL.md") for name in names)
