"""Typed state and commands for same-session human control transfer."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from computer_use.artifacts.models import Target


class ControlOwner(StrEnum):
    AUTOMATION = "AUTOMATION"
    HUMAN = "HUMAN"


class InterventionStatus(StrEnum):
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    CLAIMED = "CLAIMED"
    RESUMED = "RESUMED"


class ControlLease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    owner: ControlOwner
    epoch: PositiveInt
    operator_id: str | None = None


class InterventionRequest(BaseModel):
    """Complete, bounded context shown to the operator at the pause point."""

    model_config = ConfigDict(extra="forbid")
    run_id: str
    mode: Literal["discovery", "replay"]
    goal: str | None = None
    capability_id: str | None = None
    current_step: str | None = None
    reason_code: str
    reason: str
    screenshot_data_url: str
    current_url: str
    state_fingerprint: str
    page_title: str | None = None
    status: InterventionStatus = InterventionStatus.WAITING_FOR_HUMAN
    epoch: PositiveInt
    operator_id: str | None = None


class HumanAction(BaseModel):
    """A typed operator command executed only while the human owns the live session."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["navigate", "click", "type", "select"]
    target: Target | None = None
    value: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def validate_arguments(self) -> "HumanAction":
        if self.kind == "navigate" and not self.url:
            raise ValueError("navigate requires url")
        if self.kind in {"click", "type", "select"} and self.target is None:
            raise ValueError(f"{self.kind} requires target")
        if self.kind in {"type", "select"} and self.value is None:
            raise ValueError(f"{self.kind} requires value")
        return self


class ResumeSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    epoch: PositiveInt
    operator_id: str
    current_step_completed: bool = True
    note: str | None = Field(default=None, max_length=500)
