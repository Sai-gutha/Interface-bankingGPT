"""Framework-neutral boundary between automation logic and a computer surface."""

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from computer_use.artifacts.models import Target


class SurfaceState(BaseModel):
    """Small state summary suitable for policy and orchestration decisions."""

    model_config = ConfigDict(extra="forbid")
    surface_kind: Literal["web", "accessibility", "screenshot", "desktop"]
    location: str
    title: str | None = None
    ready: bool = True
    active_dialog: Literal["alert", "beforeunload", "confirm", "prompt"] | None = None


class Observation(BaseModel):
    """Sanitized semantic perception returned to an agent or evidence recorder."""

    model_config = ConfigDict(extra="forbid")
    state: SurfaceState
    semantic_tree: str
    visible_text: str
    state_fingerprint: str
    metadata: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Screenshot:
    """In-memory capture; persistence and redaction belong to observability."""

    data: bytes
    media_type: Literal["image/png", "image/jpeg"] = "image/png"


@runtime_checkable
class SurfaceAdapter(Protocol):
    """Actions shared by web, accessibility-tree, vision, and desktop adapters.

    Targets are serializable semantic descriptions. Implementations translate them into
    framework-native handles internally; callers never receive Playwright pages or locators.
    """

    async def observe(self) -> Observation: ...

    async def click(self, target: Target) -> None: ...

    async def type(self, target: Target, value: str) -> None: ...

    async def select(self, target: Target, value: str) -> None: ...

    async def navigate(self, url: str) -> None: ...

    async def extract(
        self,
        target: Target,
        source: Literal["inner_text", "value", "attribute"] = "inner_text",
        attribute_name: str | None = None,
    ) -> str: ...

    async def screenshot(self) -> Screenshot: ...

    async def wait_for(
        self,
        target: Target,
        timeout_ms: int = 10_000,
        *,
        state: Literal["visible", "hidden"] = "visible",
    ) -> None: ...

    async def get_current_state(self) -> SurfaceState: ...

    async def close(self) -> None: ...


# Compatibility alias for the initial scaffold.
Surface = SurfaceAdapter
