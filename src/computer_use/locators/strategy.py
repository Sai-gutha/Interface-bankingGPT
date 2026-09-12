"""Deterministic locator contracts independent of agent or LLM code."""

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from computer_use.artifacts.models import LocatorCandidate, Target

LOCATOR_PRIORITY: dict[str, int] = {
    "role": 0,
    "label": 1,
    "text": 2,
    "relative_text": 2,
    "attribute": 3,
    "accessibility_id": 3,
    "css": 4,
    "xpath": 4,
    "coordinates": 5,
}


class ResolutionPurpose(StrEnum):
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    EXTRACT = "extract"
    WAIT_VISIBLE = "wait_visible"
    WAIT_HIDDEN = "wait_hidden"


class ResolutionRecord(BaseModel):
    """Safe locator telemetry; contains strategy metadata but no extracted values."""

    model_config = ConfigDict(extra="forbid")
    target_description: str
    strategy: str
    candidate_index: int
    priority: int
    match_count: int
    purpose: ResolutionPurpose


class ResolutionRecorder(Protocol):
    async def record(self, resolution: ResolutionRecord) -> None: ...


class SurfaceError(RuntimeError):
    """Base error raised while operating a concrete UI surface."""


class LocatorResolutionError(SurfaceError):
    """Base error for deterministic target resolution."""


class TargetNotFoundError(LocatorResolutionError):
    """No supported candidate produced a plausible element."""


class AmbiguousTargetError(LocatorResolutionError):
    """A candidate produced multiple plausible elements where one was required."""


class UnsupportedTargetError(LocatorResolutionError):
    """No candidate can be interpreted on the active surface."""


class ResolvedTarget(Protocol):
    """Opaque handle returned by a surface-specific locator resolver."""


class LocatorResolver(Protocol):
    """Resolve ordered candidates and reject zero or ambiguous matches."""

    async def resolve(self, target: Target, purpose: ResolutionPurpose) -> ResolvedTarget: ...


def ranked_candidates(target: Target) -> list[tuple[int, LocatorCandidate]]:
    """Return unique candidates in canonical semantic-first order."""

    seen: set[str] = set()
    candidates: list[tuple[int, LocatorCandidate]] = []
    for index, candidate in enumerate(target.candidates):
        key = candidate.model_dump_json()
        if key not in seen:
            candidates.append((index, candidate))
            seen.add(key)
    return sorted(
        candidates,
        key=lambda item: (LOCATOR_PRIORITY[item[1].strategy], item[0]),
    )
