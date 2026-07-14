import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_uses_full_sha_pins_and_read_only_checkout() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    action_uses = re.findall(r"^\s*uses:\s*([^\s]+)$", workflow, flags=re.MULTILINE)

    assert action_uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in action_uses)
    assert "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5" in workflow
    assert "persist-credentials: false" in workflow
    assert "permissions:\n  contents: read" in workflow


def test_ci_uses_reviewed_uv_cache_and_frozen_checks() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e" in workflow
    assert 'version: "0.11.19"' in workflow
    assert "enable-cache: true" in workflow
    assert "run: uv sync --frozen" in workflow
    assert "run: uv run pre-commit run --all-files --show-diff-on-failure" in workflow
    assert "run: uv run mypy" in workflow
    assert "run: uv run pytest" in workflow
    assert 'UNTAPED_ISOLATED_WHEEL_TEST: "1"' in workflow


def test_ci_cancels_superseded_branch_runs() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "concurrency:" in workflow
    assert "group: ${{ github.workflow }}-${{ github.ref }}" in workflow
    assert "cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}" in workflow
