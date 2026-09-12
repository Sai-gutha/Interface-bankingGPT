"""Framework-neutral surface abstractions and concrete adapters."""

from computer_use.surfaces.base import Observation, Screenshot, SurfaceAdapter, SurfaceState
from computer_use.surfaces.playwright import PlaywrightSurfaceAdapter

__all__ = [
    "Observation",
    "PlaywrightSurfaceAdapter",
    "Screenshot",
    "SurfaceAdapter",
    "SurfaceState",
]
