from importlib.resources import files
from pathlib import Path

from untaped.api import SkillAsset, ToolSpec, run_tool

from untaped_orchestration.cli import app
from untaped_orchestration.settings import OrchestrationSettings

ORCHESTRATION_SKILL = SkillAsset(
    name="untaped-orchestration",
    source=Path(str(files("untaped_orchestration").joinpath("skills", "untaped-orchestration"))),
    description="Use typed repository orchestration stores safely.",
)

SPEC = ToolSpec(
    command="untaped-orchestration",
    distribution="untaped-orchestration",
    section="orchestration",
    profile_model=OrchestrationSettings,
    skills=(ORCHESTRATION_SKILL,),
)


def main() -> object:
    return run_tool(app, SPEC)
