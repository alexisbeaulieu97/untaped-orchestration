from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_self_adoption_contains_no_fleet_or_legacy_migration_state() -> None:
    assert (REPO_ROOT / ".untaped" / "orchestration" / "store.toml").is_file()
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    forbidden_paths = {
        "orchestration/import-manifest.toml",
        "orchestration/migration-manifest.toml",
        "untaped.yml",
    }
    assert forbidden_paths.isdisjoint(tracked)
    assert not any("cohort" in Path(path).name.casefold() for path in tracked)
    assert not any(path.startswith("pypi-rollout/") for path in tracked)
    assert not any(path.startswith("untaped-github/") for path in tracked)
    assert not any(path.startswith("docs/orchestration-migration/") for path in tracked)


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
    assert "_expected_package_bytes" in acceptance
    assert "checkout_bytes" in acceptance


def test_every_cli_mutation_receipt_wrapper_uses_the_shared_result_helper() -> None:
    expected = {
        "task create",
        "task update",
        "task transition",
        "task move",
        "task review",
        "task close",
        "decision create",
        "decision update",
        "decision supersede",
        "decision retire",
        "link add",
        "link remove",
        "evidence add",
        "evidence remove",
        "store child add",
        "store child remove",
        "init",
        "repair frontmatter",
        "repair duplicate",
        "curate acknowledge",
        "curate snooze",
        "store import",
    }
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPO_ROOT / "src/untaped_orchestration/cli").glob("*_commands.py"))
    )
    wrapped = set(re.findall(r'mutation_result\(\s*["\']([^"\']+)', sources))
    dynamic = {
        "task transition",
        "task move",
        "link add",
        "link remove",
        "evidence add",
        "evidence remove",
    }

    assert wrapped == expected - dynamic
    for command in expected:
        assert not re.search(rf'CommandResult\(\s*["\']{re.escape(command)}["\']', sources)
    task_source = (REPO_ROOT / "src/untaped_orchestration/cli/task_commands.py").read_text()
    assert "lambda: mutation_result(" in task_source
    assert 'placement_command(\n            "task transition"' in task_source
    assert 'placement_command(\n            "task move"' in task_source
    relation_source = (REPO_ROOT / "src/untaped_orchestration/cli/relation_commands.py").read_text()
    assert 'mutation_result(f"link {name}"' in relation_source
    assert 'mutation_result(f"evidence {name}"' in relation_source
    assert 'register_link("add"' in relation_source
    assert 'register_link("remove"' in relation_source
    assert 'register_evidence("add"' in relation_source
    assert 'register_evidence("remove"' in relation_source
