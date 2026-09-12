"""Browser-backed verification of the concrete surface adapter."""

import pytest

from computer_use.artifacts.models import LabelLocator, RoleLocator, Target, TextLocator
from computer_use.surfaces.playwright import PlaywrightSurfaceAdapter


@pytest.mark.asyncio
async def test_playwright_adapter_operates_without_leaking_page_types() -> None:
    adapter = await PlaywrightSurfaceAdapter.launch()
    try:
        await adapter.navigate(
            "data:text/html,<title>Fixture</title><label>Member ID<input></label>"
            "<button onclick=\"document.querySelector('output').textContent='Ready'\">Go</button>"
            "<output>Waiting</output>"
        )
        field = Target(
            description="Member ID field",
            candidates=[LabelLocator(label="Member ID")],
        )
        button = Target(
            description="Go button",
            candidates=[RoleLocator(role="button", name="Go")],
        )
        output = Target(
            description="Ready output",
            candidates=[TextLocator(text="Ready")],
        )

        await adapter.type(field, "12345")
        await adapter.click(button)
        await adapter.wait_for(output)

        observation = await adapter.observe()
        image = await adapter.screenshot()

        assert await adapter.extract(output) == "Ready"
        assert observation.state.surface_kind == "web"
        assert observation.state.title == "Fixture"
        assert "Member ID" in observation.semantic_tree
        assert image.data.startswith(b"\x89PNG")
    finally:
        await adapter.close()
