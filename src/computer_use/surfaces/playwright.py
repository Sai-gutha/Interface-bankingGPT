"""Playwright implementation of the framework-neutral surface adapter."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from computer_use.artifacts.models import (
    CoordinateLocator,
    LocatorCandidate,
    Target,
)
from computer_use.locators.playwright import PlaywrightLocatorResolver
from computer_use.locators.strategy import (
    ResolutionPurpose,
    ResolutionRecord,
    TargetNotFoundError,
    UnsupportedTargetError,
)
from computer_use.surfaces.base import Observation, Screenshot, SurfaceState


@dataclass(slots=True)
class _OwnedResources:
    playwright: Playwright
    browser: Browser
    context: BrowserContext


class PlaywrightSurfaceAdapter:
    """Translate generic targets and actions into Playwright operations.

    Inject an existing ``Page`` for handoff/tests, or use ``launch`` to create resources owned by
    this adapter. Closing an injected page remains the caller's responsibility.
    """

    def __init__(self, page: Page, *, owned: _OwnedResources | None = None) -> None:
        self._page = page
        self._owned = owned
        self._resolution_records: list[ResolutionRecord] = []
        self._resolver = PlaywrightLocatorResolver(page, self._record_resolution)

    @property
    def resolution_records(self) -> tuple[ResolutionRecord, ...]:
        """Structured append-only evidence of successful locator resolutions."""

        return tuple(self._resolution_records)

    async def _record_resolution(self, record: ResolutionRecord) -> None:
        self._resolution_records.append(record)

    @classmethod
    async def launch(
        cls,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1440, 900),
    ) -> "PlaywrightSurfaceAdapter":
        """Launch an isolated Chromium context owned by the returned adapter."""

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(headless=headless)
            context = await browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]}
            )
            page = await context.new_page()
        except Exception:
            await playwright.stop()
            raise
        return cls(page, owned=_OwnedResources(playwright, browser, context))

    async def observe(self) -> Observation:
        """Return accessibility-oriented semantics plus visible text."""

        state = await self.get_current_state()
        body = self._page.locator("body")
        visible_text = await body.inner_text()
        try:
            semantic_tree = await body.aria_snapshot()
        except Exception:  # pragma: no cover - compatibility with older browsers
            semantic_tree = visible_text
        source = "\n".join((state.location, state.title or "", semantic_tree))
        return Observation(
            state=state,
            semantic_tree=semantic_tree,
            visible_text=visible_text,
            state_fingerprint=hashlib.sha256(source.encode()).hexdigest(),
            metadata={"adapter": "playwright", "targeting": "semantic-first"},
        )

    async def click(self, target: Target) -> None:
        try:
            await (await self._resolver.resolve(target, ResolutionPurpose.CLICK)).click()
        except TargetNotFoundError:
            coordinate = self._coordinate_candidate(target)
            if coordinate is None:
                raise
            await self._page.mouse.click(coordinate.x, coordinate.y)
            self._resolution_records.append(
                ResolutionRecord(
                    target_description=target.description,
                    strategy="coordinates",
                    candidate_index=len(target.candidates) - 1,
                    priority=5,
                    match_count=1,
                    purpose=ResolutionPurpose.CLICK,
                )
            )

    async def type(self, target: Target, value: str) -> None:
        """Replace the current value, matching deterministic form-fill semantics."""

        try:
            await (await self._resolver.resolve(target, ResolutionPurpose.TYPE)).fill(value)
        except TargetNotFoundError as error:
            if self._coordinate_candidate(target):
                raise UnsupportedTargetError(
                    "Coordinate fallbacks cannot safely receive typed values"
                ) from error
            raise

    async def select(self, target: Target, value: str) -> None:
        try:
            locator = await self._resolver.resolve(target, ResolutionPurpose.SELECT)
        except TargetNotFoundError as error:
            if self._coordinate_candidate(target):
                raise UnsupportedTargetError(
                    "Coordinate fallbacks cannot safely select values"
                ) from error
            raise
        await locator.select_option(value)

    async def navigate(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")

    async def extract(
        self,
        target: Target,
        source: Literal["inner_text", "value", "attribute"] = "inner_text",
        attribute_name: str | None = None,
    ) -> str:
        try:
            locator = await self._resolver.resolve(target, ResolutionPurpose.EXTRACT)
            if source == "value":
                return await locator.input_value()
            if source == "attribute":
                if attribute_name is None:
                    raise ValueError("attribute extraction requires attribute_name")
                return (await locator.get_attribute(attribute_name)) or ""
            return (await locator.inner_text()).strip()
        except TargetNotFoundError as error:
            if self._coordinate_candidate(target):
                raise UnsupportedTargetError(
                    "Coordinate fallbacks do not expose semantic text"
                ) from error
            raise

    async def screenshot(self) -> Screenshot:
        return Screenshot(data=await self._page.screenshot(type="png", full_page=True))

    async def wait_for(
        self,
        target: Target,
        timeout_ms: int = 10_000,
        *,
        state: Literal["visible", "hidden"] = "visible",
    ) -> None:
        try:
            purpose = (
                ResolutionPurpose.WAIT_VISIBLE
                if state == "visible"
                else ResolutionPurpose.WAIT_HIDDEN
            )
            await (await self._resolver.resolve(target, purpose)).wait_for(
                state=state, timeout=timeout_ms
            )
        except TargetNotFoundError as error:
            if self._coordinate_candidate(target):
                raise UnsupportedTargetError(
                    "A coordinate has no observable visibility condition"
                ) from error
            raise

    async def get_current_state(self) -> SurfaceState:
        return SurfaceState(
            surface_kind="web",
            location=self._page.url,
            title=await self._page.title(),
            ready=await self._page.evaluate("document.readyState === 'complete'"),
        )

    async def close(self) -> None:
        if self._owned is None:
            return
        await self._owned.context.close()
        await self._owned.browser.close()
        await self._owned.playwright.stop()
        self._owned = None

    @staticmethod
    def _coordinate_candidate(target: Target) -> CoordinateLocator | None:
        candidates: Sequence[LocatorCandidate] = target.candidates
        if candidates and isinstance(candidates[-1], CoordinateLocator):
            return candidates[-1]
        return None


# Earlier scaffold name retained for existing callers.
PlaywrightSurface = PlaywrightSurfaceAdapter
