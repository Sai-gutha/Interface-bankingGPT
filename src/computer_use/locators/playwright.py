"""Playwright-specific realization of the deterministic locator policy."""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from playwright.async_api import Locator, Page

from computer_use.artifacts.models import (
    AccessibilityIdLocator,
    AttributeLocator,
    CoordinateLocator,
    CssLocator,
    LabelLocator,
    LocatorCandidate,
    RelativeTextLocator,
    RoleLocator,
    Target,
    TextLocator,
    XPathLocator,
)
from computer_use.locators.strategy import (
    AmbiguousTargetError,
    ResolutionPurpose,
    ResolutionRecord,
    TargetNotFoundError,
    UnsupportedTargetError,
    ranked_candidates,
)

RecordCallback = Callable[[ResolutionRecord], Awaitable[None]]


class PlaywrightLocatorResolver:
    """Resolve targets by semantics, rejecting non-plausible and ambiguous matches."""

    def __init__(self, page: Page, recorder: RecordCallback | None = None) -> None:
        self._page = page
        self._recorder = recorder

    async def resolve(self, target: Target, purpose: ResolutionPurpose) -> Locator:
        unsupported: list[str] = []
        for original_index, candidate in ranked_candidates(target):
            locator = self._to_locator(candidate)
            if locator is None:
                unsupported.append(candidate.strategy)
                continue
            plausible = await self._plausible_matches(locator, target, purpose)
            if not plausible:
                continue
            if target.require_unique and len(plausible) != 1:
                raise AmbiguousTargetError(
                    f"Target {target.description!r} matched {len(plausible)} plausible elements "
                    f"using {candidate.strategy}"
                )
            resolved = locator.nth(plausible[0])
            if self._recorder is not None:
                await self._recorder(
                    ResolutionRecord(
                        target_description=target.description,
                        strategy=candidate.strategy,
                        candidate_index=original_index,
                        priority=self._priority(candidate),
                        match_count=len(plausible),
                        purpose=purpose,
                    )
                )
            return resolved
        if unsupported and len(unsupported) == len(ranked_candidates(target)):
            raise UnsupportedTargetError(
                f"Playwright cannot interpret strategies: {', '.join(unsupported)}"
            )
        raise TargetNotFoundError(f"No plausible candidate matched {target.description!r}")

    async def _plausible_matches(
        self, locator: Locator, target: Target, purpose: ResolutionPurpose
    ) -> list[int]:
        plausible: list[int] = []
        for index in range(await locator.count()):
            element = locator.nth(index)
            if purpose != ResolutionPurpose.WAIT_HIDDEN and not await element.is_visible():
                continue
            if target.expected_role and await self._role(element) != target.expected_role:
                continue
            if purpose == ResolutionPurpose.CLICK and not await element.is_enabled():
                continue
            if purpose == ResolutionPurpose.TYPE and not await element.is_editable():
                continue
            if purpose == ResolutionPurpose.SELECT:
                tag = await element.evaluate("element => element.tagName.toLowerCase()")
                if tag != "select" or not await element.is_enabled():
                    continue
            plausible.append(index)
        return plausible

    @staticmethod
    async def _role(locator: Locator) -> str | None:
        return cast(
            str | None,
            await locator.evaluate(
                """element => {
                    const explicit = element.getAttribute('role');
                    if (explicit) return explicit;
                    const tag = element.tagName.toLowerCase();
                    if (tag === 'button') return 'button';
                    if (tag === 'td') return 'cell';
                    if (tag === 'th') return 'columnheader';
                    if (tag === 'tr') return 'row';
                    if (tag === 'select') return 'combobox';
                    if (tag === 'textarea') return 'textbox';
                    if (tag === 'a' && element.hasAttribute('href')) return 'link';
                    if (tag === 'input') {
                        const type = (element.getAttribute('type') || 'text').toLowerCase();
                        if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
                        if (type === 'checkbox') return 'checkbox';
                        if (type === 'radio') return 'radio';
                        return 'textbox';
                    }
                    return null;
                }"""
            ),
        )

    @staticmethod
    def _priority(candidate: LocatorCandidate) -> int:
        from computer_use.locators.strategy import LOCATOR_PRIORITY

        return LOCATOR_PRIORITY[candidate.strategy]

    def _to_locator(self, candidate: LocatorCandidate) -> Locator | None:
        if isinstance(candidate, RoleLocator):
            return self._page.get_by_role(
                cast(Any, candidate.role), name=candidate.name, exact=candidate.exact
            )
        if isinstance(candidate, LabelLocator):
            return self._page.get_by_label(candidate.label, exact=candidate.exact)
        if isinstance(candidate, TextLocator):
            return self._page.get_by_text(candidate.text, exact=candidate.exact)
        if isinstance(candidate, AttributeLocator):
            escaped_name = candidate.name.replace('"', '\\"')
            escaped_value = candidate.value.replace('"', '\\"')
            return self._page.locator(f'[{escaped_name}="{escaped_value}"]')
        if isinstance(candidate, RelativeTextLocator):
            container = self._relative_container(candidate)
            if candidate.target_role:
                return container.get_by_role(cast(Any, candidate.target_role))
            if candidate.target_text:
                return container.get_by_text(candidate.target_text, exact=True)
            if candidate.target_css:
                return container.locator(candidate.target_css)
            return container
        if isinstance(candidate, CssLocator):
            return self._page.locator(candidate.selector)
        if isinstance(candidate, XPathLocator):
            return self._page.locator(f"xpath={candidate.expression}")
        if isinstance(candidate, (CoordinateLocator, AccessibilityIdLocator)):
            return None
        raise TypeError(f"Unhandled locator candidate: {type(candidate).__name__}")

    def _relative_container(self, candidate: RelativeTextLocator) -> Locator:
        anchor = self._page.get_by_text(candidate.anchor_text, exact=True)
        if candidate.relation == "row_containing":
            return anchor.locator("xpath=ancestor::tr[1]")
        if candidate.relation == "within":
            return anchor.locator("xpath=ancestor::*[self::div or self::section or self::td][1]")
        axis = "following" if candidate.relation == "following" else "preceding"
        return anchor.locator(f"xpath={axis}::*[1]")
