from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE_ID = "sto_019f0000000070008000000000000000"
ISOLATED_INSTALL_ENV = "UNTAPED_ISOLATED_WHEEL_TEST"
EXPECTED_REQUIREMENTS = (
    "cyclopts>=4.16,<5",
    "filelock>=3.29.7,<4",
    "pydantic>=2.13.3,<3",
    "tomli-w>=1.2,<2",
    "untaped>=3.1.0,<4",
)
EXPECTED_METADATA_FIELDS = {
    "Metadata-Version": ("2.4",),
    "Name": ("untaped-orchestration",),
    "Version": ("0.1.0",),
    "Summary": ("Git-native typed orchestration for repository tasks and decisions.",),
    "Author": ("Alexis Beaulieu",),
    "Author-email": ("Alexis Beaulieu <alexisbeaulieu97@gmail.com>",),
    "License-Expression": ("MIT",),
    "License-File": ("LICENSE",),
    "Requires-Dist": EXPECTED_REQUIREMENTS,
    "Requires-Python": (">=3.14",),
    "Description-Content-Type": ("text/markdown",),
}


@dataclass(frozen=True)
class BuiltArtifacts:
    root: Path
    wheel: Path
    sdist: Path


def _clean_environment(cache: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "UV_FIND_LINKS",
        "UV_NO_INDEX",
        "UV_OFFLINE",
        "UV_PROJECT_ENVIRONMENT",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "COLUMNS": "200",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "UV_CACHE_DIR": str(cache),
        }
    )
    return environment


def _run(
    *args: str | Path,
    cwd: Path,
    cache: Path,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_clean_environment(cache),
    )


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> BuiltArtifacts:
    root = tmp_path_factory.mktemp("package-acceptance")
    artifacts = root / "artifacts"
    artifacts.mkdir()
    assert REPO_ROOT not in artifacts.parents
    assert artifacts != REPO_ROOT / "dist"
    assert not list(artifacts.iterdir())

    _run(
        "uv",
        "build",
        "--offline",
        "--out-dir",
        artifacts,
        "--no-sources",
        cwd=REPO_ROOT,
        cache=REPO_ROOT / ".uv-cache",
    )
    wheels = tuple(artifacts.glob("*.whl"))
    sdists = tuple(artifacts.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    assert set(artifacts.iterdir()) == {wheels[0], sdists[0], artifacts / ".gitignore"}
    assert (artifacts / ".gitignore").read_text(encoding="utf-8") == "*"
    return BuiltArtifacts(root, wheels[0], sdists[0])


def _message(raw: bytes) -> Message:
    return BytesParser(policy=policy.default).parsebytes(raw)


def _expected_package_bytes() -> dict[str, bytes]:
    source = REPO_ROOT / "src"
    return {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in (source / "untaped_orchestration").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _assert_metadata(raw: bytes) -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == EXPECTED_METADATA_FIELDS["Name"][0]
    assert project["version"] == EXPECTED_METADATA_FIELDS["Version"][0]
    assert project["description"] == EXPECTED_METADATA_FIELDS["Summary"][0]
    assert project["authors"] == [
        {
            "name": EXPECTED_METADATA_FIELDS["Author"][0],
            "email": "alexisbeaulieu97@gmail.com",
        }
    ]
    assert project["license"] == EXPECTED_METADATA_FIELDS["License-Expression"][0]
    assert project["license-files"] == [EXPECTED_METADATA_FIELDS["License-File"][0]]
    assert tuple(project["dependencies"]) == EXPECTED_REQUIREMENTS
    assert project["requires-python"] == EXPECTED_METADATA_FIELDS["Requires-Python"][0]
    assert project["readme"] == "README.md"

    metadata = _message(raw)
    actual = {name: tuple(metadata.get_all(name, ())) for name in dict.fromkeys(metadata.keys())}
    assert actual == EXPECTED_METADATA_FIELDS
    assert metadata.get_payload() == (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _assert_repository_state_excluded(names: set[str]) -> None:
    forbidden_parts = {
        ".git",
        ".superpowers",
        ".untaped",
        ".uv-cache",
        ".venv",
        "dist",
    }
    for name in names:
        parts = set(PurePosixPath(name).parts)
        assert forbidden_parts.isdisjoint(parts), name
        assert not name.endswith((".egg-link", ".pth")), name


def test_fresh_wheel_and_sdist_are_built_outside_dist(
    built_artifacts: BuiltArtifacts,
) -> None:
    assert built_artifacts.wheel.parent == built_artifacts.root / "artifacts"
    assert built_artifacts.sdist.parent == built_artifacts.root / "artifacts"
    stale_dist_artifacts = {
        *(REPO_ROOT / "dist").glob("*.whl"),
        *(REPO_ROOT / "dist").glob("*.tar.gz"),
    }
    assert built_artifacts.wheel not in stale_dist_artifacts
    assert built_artifacts.sdist not in stale_dist_artifacts


@pytest.mark.parametrize(
    "original,replacement",
    (
        (
            b"Summary: Git-native typed orchestration for repository tasks and decisions.",
            b"Summary: corrupted summary",
        ),
        (b"# untaped-orchestration", b"# corrupted description"),
    ),
)
def test_metadata_contract_rejects_stable_header_or_body_corruption(
    built_artifacts: BuiltArtifacts,
    original: bytes,
    replacement: bytes,
) -> None:
    with zipfile.ZipFile(built_artifacts.wheel) as archive:
        raw = archive.read("untaped_orchestration-0.1.0.dist-info/METADATA")
    assert original in raw

    with pytest.raises(AssertionError):
        _assert_metadata(raw.replace(original, replacement, 1))


def test_wheel_metadata_record_and_package_contents_are_exact(
    built_artifacts: BuiltArtifacts,
) -> None:
    checkout_bytes = _expected_package_bytes()
    with zipfile.ZipFile(built_artifacts.wheel) as archive:
        files = {value.filename for value in archive.infolist() if not value.is_dir()}
        dist_info = "untaped_orchestration-0.1.0.dist-info"
        metadata_name = f"{dist_info}/METADATA"
        wheel_name = f"{dist_info}/WHEEL"
        entry_points_name = f"{dist_info}/entry_points.txt"
        record_name = f"{dist_info}/RECORD"
        metadata_files = {
            metadata_name,
            wheel_name,
            entry_points_name,
            record_name,
            f"{dist_info}/licenses/LICENSE",
        }
        assert files == set(checkout_bytes) | metadata_files
        _assert_repository_state_excluded(files)
        _assert_metadata(archive.read(metadata_name))
        for path, expected_bytes in checkout_bytes.items():
            assert archive.read(path) == expected_bytes, path
        assert archive.read(f"{dist_info}/licenses/LICENSE") == (REPO_ROOT / "LICENSE").read_bytes()

        wheel_metadata = _message(archive.read(wheel_name))
        assert set(wheel_metadata.keys()) == {
            "Wheel-Version",
            "Generator",
            "Root-Is-Purelib",
            "Tag",
        }
        assert wheel_metadata["Wheel-Version"] == "1.0"
        assert wheel_metadata["Generator"].startswith("uv ")
        assert wheel_metadata["Root-Is-Purelib"] == "true"
        assert wheel_metadata.get_all("Tag") == ["py3-none-any"]
        assert archive.read(entry_points_name) == (
            b"[console_scripts]\nuntaped-orchestration = untaped_orchestration.__main__:main\n\n"
        )

        rows = tuple(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
        assert {row[0] for row in rows} == files
        for path, digest, size in rows:
            if path == record_name:
                assert digest == ""
                assert size == ""
                continue
            raw = archive.read(path)
            expected_digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
            assert digest == f"sha256={expected_digest.decode('ascii')}"
            assert size == str(len(raw))

        skill = archive.read("untaped_orchestration/skills/untaped-orchestration/SKILL.md").decode(
            "utf-8"
        )
        assert "brief --format json" in skill
        assert "untaped_orchestration/py.typed" in files


def test_sdist_metadata_and_package_contents_are_exact(
    built_artifacts: BuiltArtifacts,
) -> None:
    root = "untaped_orchestration-0.1.0"
    checkout_bytes = _expected_package_bytes()
    support_bytes = {
        "LICENSE": (REPO_ROOT / "LICENSE").read_bytes(),
        "README.md": (REPO_ROOT / "README.md").read_bytes(),
        "pyproject.toml": (REPO_ROOT / "pyproject.toml").read_bytes(),
    }
    expected = {
        f"{root}/PKG-INFO",
        *(f"{root}/{path}" for path in support_bytes),
        *(f"{root}/src/{path}" for path in checkout_bytes),
    }
    with tarfile.open(built_artifacts.sdist, mode="r:gz") as archive:
        files = {value.name for value in archive.getmembers() if value.isfile()}
        assert files == expected
        _assert_repository_state_excluded(files)
        package_info = archive.extractfile(f"{root}/PKG-INFO")
        assert package_info is not None
        _assert_metadata(package_info.read())
        for path, expected_bytes in support_bytes.items():
            member = archive.extractfile(f"{root}/{path}")
            assert member is not None
            assert member.read() == expected_bytes, path
        for path, expected_bytes in checkout_bytes.items():
            member = archive.extractfile(f"{root}/src/{path}")
            assert member is not None
            assert member.read() == expected_bytes, path
        skill = archive.extractfile(
            f"{root}/src/untaped_orchestration/skills/untaped-orchestration/SKILL.md"
        )
        assert skill is not None
        assert b"brief --format json" in skill.read()
        assert f"{root}/src/untaped_orchestration/py.typed" in files


@pytest.mark.skipif(
    os.environ.get(ISOLATED_INSTALL_ENV) != "1",
    reason=(
        "dependency-resolving isolated install requires "
        "UNTAPED_ISOLATED_WHEEL_TEST=1 (enabled in PR CI)"
    ),
)
def test_dependency_resolving_isolated_wheel_console_and_store_smoke(
    built_artifacts: BuiltArtifacts,
) -> None:
    root = built_artifacts.root / "isolated"
    root.mkdir()
    cache = root / "uv-cache"
    venv = root / "venv"
    _run("uv", "venv", "--python", "3.14", venv, cwd=root, cache=cache)
    python = venv / "bin" / "python"
    _run("uv", "pip", "install", "--python", python, built_artifacts.wheel, cwd=root, cache=cache)

    console = venv / "bin" / "untaped-orchestration"
    assert console.is_file()
    assert venv in console.parents
    inspection = f"""
from pathlib import Path
import sys
import untaped_orchestration

repo = Path({str(REPO_ROOT)!r}).resolve()
development = (repo / ".venv").resolve()
module = Path(untaped_orchestration.__file__).resolve()
prefix = Path(sys.prefix).resolve()
paths = tuple(Path(value).resolve() for value in sys.path if value)
assert prefix in module.parents
assert repo not in module.parents
assert all(repo != value and repo not in value.parents for value in paths)
assert all(development != value and development not in value.parents for value in paths)
"""
    _run(python, "-c", inspection, cwd=root, cache=cache)
    for pth in venv.rglob("*.pth"):
        assert str(REPO_ROOT) not in pth.read_text(encoding="utf-8", errors="ignore")

    assert _run(console, "--help", cwd=root, cache=cache).stdout.startswith(
        b"Usage: untaped-orchestration"
    )
    assert _run(console, "--version", cwd=root, cache=cache).stdout == b"0.1.0\n"

    repository = root / "repository"
    repository.mkdir()
    initialized = _run(
        console,
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
        cwd=root,
        cache=cache,
    )
    assert json.loads(initialized.stdout)["data"]["applied"] is True

    for command in (
        ("check", "--local", "--format", "json"),
        ("fmt", "--check", "--local", "--format", "json"),
        ("render", "--check", "--format", "json"),
    ):
        result = _run(console, *command, cwd=repository, cache=cache)
        assert json.loads(result.stdout)["complete"] is True
