from untaped_orchestration import app as package_app
from untaped_orchestration.__main__ import SPEC
from untaped_orchestration.cli import app
from untaped_orchestration.settings import OrchestrationSettings


def test_tool_spec_is_exact() -> None:
    assert SPEC.command == "untaped-orchestration"
    assert SPEC.distribution == "untaped-orchestration"
    assert SPEC.section == "orchestration"
    assert SPEC.profile_model is OrchestrationSettings
    assert SPEC.state_model is None
    (skill,) = SPEC.skills
    assert skill.name == "untaped-orchestration"
    assert skill.source.joinpath("SKILL.md").is_file()


def test_settings_are_empty_and_ignore_foreign_keys() -> None:
    assert OrchestrationSettings.model_fields == {}
    assert OrchestrationSettings.model_validate({"future": "ignored"}) == OrchestrationSettings()


def test_package_exports_cli_app_lazily() -> None:
    assert package_app is app
