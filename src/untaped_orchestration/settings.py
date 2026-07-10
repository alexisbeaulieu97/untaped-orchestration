from pydantic import BaseModel, ConfigDict


class OrchestrationSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
