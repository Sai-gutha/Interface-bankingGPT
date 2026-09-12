"""Browser-backed tests for deterministic locator ranking and ambiguity safety."""

import pytest

from computer_use.artifacts.models import CssLocator, LabelLocator, RoleLocator, Target
from computer_use.locators import AmbiguousTargetError, TargetNotFoundError
from computer_use.surfaces.playwright import PlaywrightSurfaceAdapter


@pytest.mark.asyncio
async def test_resolver_prefers_semantics_and_records_successful_strategy() -> None:
    adapter = await PlaywrightSurfaceAdapter.launch()
    try:
        await adapter.navigate(
            "data:text/html,<button class='fallback' onclick=\"window.chosen='css'\">Wrong</button>"
            "<button onclick=\"window.chosen='role'\">Continue</button>"
        )
        target = Target(
            description="Continue action",
            candidates=[
                CssLocator(selector=".fallback"),
                RoleLocator(role="button", name="Continue"),
            ],
            expected_role="button",
        )

        await adapter.click(target)

        assert adapter.resolution_records[-1].strategy == "role"
        assert adapter.resolution_records[-1].candidate_index == 1
        assert adapter.resolution_records[-1].match_count == 1
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_resolver_skips_implausible_match_and_uses_fallback() -> None:
    adapter = await PlaywrightSurfaceAdapter.launch()
    try:
        await adapter.navigate(
            "data:text/html,<div role='button'>Member ID</div><label>Member ID<input></label>"
        )
        target = Target(
            description="Member ID field",
            candidates=[
                RoleLocator(role="button", name="Member ID"),
                LabelLocator(label="Member ID"),
            ],
            expected_role="textbox",
        )

        await adapter.type(target, "12345")

        assert adapter.resolution_records[-1].strategy == "label"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_resolver_fails_when_element_is_missing() -> None:
    adapter = await PlaywrightSurfaceAdapter.launch()
    try:
        await adapter.navigate("data:text/html,<button>Cancel</button>")
        target = Target(
            description="Continue button",
            candidates=[RoleLocator(role="button", name="Continue")],
        )

        with pytest.raises(TargetNotFoundError):
            await adapter.click(target)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_resolver_fails_on_duplicate_ambiguous_elements() -> None:
    adapter = await PlaywrightSurfaceAdapter.launch()
    try:
        await adapter.navigate("data:text/html,<button>Continue</button><button>Continue</button>")
        target = Target(
            description="Continue button",
            candidates=[RoleLocator(role="button", name="Continue")],
        )

        with pytest.raises(AmbiguousTargetError):
            await adapter.click(target)
    finally:
        await adapter.close()
