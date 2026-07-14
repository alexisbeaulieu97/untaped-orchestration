from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_implementation_branch_contains_no_adoption_or_fleet_state() -> None:
    assert not (REPO_ROOT / ".untaped" / "orchestration").exists()

    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    forbidden_paths = {
        ".github/workflows/orchestration.yml",
        "orchestration/import-manifest.toml",
        "orchestration/migration-manifest.toml",
        "untaped.yml",
    }
    assert forbidden_paths.isdisjoint(tracked)
    assert not any(path.startswith(".untaped/orchestration/") for path in tracked)
    assert not any("cohort" in Path(path).name.casefold() for path in tracked)
    assert not any(path.startswith("pypi-rollout/") for path in tracked)
    assert not any(path.startswith("untaped-github/") for path in tracked)


def test_package_acceptance_has_no_development_environment_workaround() -> None:
    acceptance = (REPO_ROOT / "tests/integration/test_installed_wheel.py").read_text(
        encoding="utf-8"
    )

    assert "orchestration-test-dependencies.pth" not in acceptance
    assert "--no-deps" not in acceptance
    assert "UNTAPED_ISOLATED_WHEEL_TEST" in acceptance
    assert "tarfile" in acceptance
    assert "RECORD" in acceptance
    assert "Requires-Dist" in acceptance
