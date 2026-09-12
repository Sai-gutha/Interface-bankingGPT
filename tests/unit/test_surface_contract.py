"""Contract tests for framework-neutral targets and adapter shape."""

from computer_use.artifacts.models import CoordinateLocator, RoleLocator, Target
from computer_use.surfaces.base import SurfaceAdapter
from computer_use.surfaces.playwright import PlaywrightSurfaceAdapter


def test_target_candidates_are_typed_and_serializable() -> None:
    target = Target(
        description="Find member button",
        candidates=[
            RoleLocator(role="button", name="Find Member"),
            CoordinateLocator(x=640, y=480, viewport_width=1440, viewport_height=900),
        ],
    )

    encoded = target.model_dump(mode="json")

    assert encoded["candidates"][0]["strategy"] == "role"
    assert encoded["candidates"][1] == {
        "strategy": "coordinates",
        "x": 640,
        "y": 480,
        "viewport_width": 1440,
        "viewport_height": 900,
        "coordinate_space": "viewport",
    }


def test_playwright_adapter_satisfies_surface_protocol() -> None:
    assert isinstance(PlaywrightSurfaceAdapter, type)
    assert all(
        hasattr(PlaywrightSurfaceAdapter, method)
        for method in (
            "observe",
            "click",
            "type",
            "select",
            "navigate",
            "extract",
            "screenshot",
            "wait_for",
            "get_current_state",
        )
    )
    assert SurfaceAdapter.__name__ == "SurfaceAdapter"
