from importlib.resources import files
from pathlib import Path

SKILL_PATH = Path(
    str(files("untaped_orchestration").joinpath("skills", "untaped-orchestration", "SKILL.md"))
)


def test_packaged_skill_contains_all_agent_safety_rules() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    required_rules = (
        "Run `brief --format json`",
        "Use returned IDs instead of scanning files",
        "Allocate one ID before init/create and reuse it through every retry",
        "Load only needed bodies with `show`",
        "Pass revisions on every guarded mutation",
        "Never use `--force-current`",
        "Never read or edit generated views",
        "Run `check` after hand edits or recovery",
        "Verify external evidence before recording it",
        "Never place tasks in a public store",
        "Stop readiness and delivery work on incomplete federation",
    )

    for rule in required_rules:
        assert rule in skill


def test_packaged_skill_operationalizes_safe_retries_and_readiness() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert skill.index("brief --format json") < skill.index("show")
    assert "caller-stable" in skill
    assert "same ID" in skill
    assert "expected revision" in skill
    assert "complete=false" in skill
    assert "truncated=true" in skill
    assert "Do not scan" in skill
    assert "Do not use `--force-current`" in skill
    assert "Do not create or move tasks into a public store" in skill
