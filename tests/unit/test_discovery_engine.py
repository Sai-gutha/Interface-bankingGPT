"""Behavioral tests for discovery stopping conditions and neutral recording."""

import asyncio
from collections.abc import Iterable

import pytest

from computer_use.artifacts.models import RoleLocator, Target
from computer_use.discovery.engine import (
    AgentDecision,
    ClickDecision,
    DecisionAction,
    DecisionContext,
    DiscoveryEngine,
    DiscoveryRequest,
    ExtractDecision,
    FinishDecision,
    RequestHumanDecision,
    WaitDecision,
)
from computer_use.observability.events import InMemoryEventSink
from computer_use.safety.policy import AllowlistPolicyEngine, SafetyPolicy
from computer_use.surfaces.base import Observation, Screenshot, SurfaceState

BUTTON = Target(
    description="Continue button",
    candidates=[RoleLocator(role="button", name="Continue")],
)
VALUE = Target(
    description="Savings balance",
    candidates=[RoleLocator(role="cell", name="$10.00")],
)


def observation(fingerprint: str, text: str = "Page") -> Observation:
    return Observation(
        state=SurfaceState(surface_kind="web", location="http://demo.test/", title=text),
        semantic_tree=text,
        visible_text=text,
        state_fingerprint=fingerprint,
    )


class FakeSurface:
    def __init__(self, observations: Iterable[Observation], *, fail_click: bool = False) -> None:
        self._observations = list(observations)
        self._index = 0
        self._fail_click = fail_click

    async def observe(self) -> Observation:
        return self._observations[min(self._index, len(self._observations) - 1)]

    async def click(self, target: Target) -> None:
        if self._fail_click:
            raise RuntimeError("surface failed")
        self._advance()

    async def type(self, target: Target, value: str) -> None:
        self._advance()

    async def navigate(self, url: str) -> None:
        return None

    async def extract(self, target: Target) -> str:
        self._advance()
        return "$10.00"

    async def screenshot(self) -> Screenshot:
        return Screenshot(b"png")

    async def wait_for(self, target: Target, timeout_ms: int = 10_000) -> None:
        self._advance()

    async def get_current_state(self) -> SurfaceState:
        return (await self.observe()).state

    async def close(self) -> None:
        return None

    def _advance(self) -> None:
        self._index = min(self._index + 1, len(self._observations) - 1)


class ScriptedProvider:
    def __init__(self, decisions: Iterable[AgentDecision], *, delay: float = 0) -> None:
        self._decisions = iter(decisions)
        self._delay = delay

    async def decide(self, context: DecisionContext) -> AgentDecision:
        if self._delay:
            await asyncio.sleep(self._delay)
        return next(self._decisions)


def decision(action: DecisionAction) -> AgentDecision:
    return AgentDecision.model_validate(
        {
            "action": action.model_dump(mode="json"),
            "operational_reason": "Advance using the visible control",
            "confidence": 0.9,
        }
    )


def request(*, max_steps: int = 20, timeout_seconds: float = 5) -> DiscoveryRequest:
    return DiscoveryRequest(
        goal="Return the savings balance",
        starting_url="http://demo.test/",
        safety_policy=SafetyPolicy(allowed_origins=["http://demo.test/"]),
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.asyncio
async def test_success_records_actions_observations_and_concise_reasons() -> None:
    events = InMemoryEventSink()
    engine = DiscoveryEngine(
        FakeSurface([observation("one"), observation("two"), observation("three")]),
        ScriptedProvider(
            [
                decision(ClickDecision(target=BUTTON)),
                decision(ExtractDecision(target=VALUE, output_name="savings_balance")),
                decision(FinishDecision(outputs={})),
            ]
        ),
        AllowlistPolicyEngine(),
        events,
    )

    result = await engine.discover(request())

    assert result.status == "success"
    assert result.stop_reason == "goal_complete"
    assert result.outputs == {"savings_balance": "$10.00"}
    assert len(result.recording) == 2
    assert {event.event_type for event in events.events} >= {
        "observation_captured",
        "decision_made",
        "policy_decision",
        "action_completed",
        "run_completed",
    }
    assert all("reasoning" not in event.data for event in events.events)


@pytest.mark.asyncio
async def test_repeated_state_stops_loop() -> None:
    same = observation("same")
    engine = DiscoveryEngine(
        FakeSurface([same]),
        ScriptedProvider([decision(WaitDecision(target=BUTTON))] * 3),
        AllowlistPolicyEngine(),
        InMemoryEventSink(),
    )
    result = await engine.discover(request())
    assert result.stop_reason == "repeated_state"


@pytest.mark.asyncio
async def test_max_steps_stops_loop() -> None:
    engine = DiscoveryEngine(
        FakeSurface([observation("one"), observation("two")]),
        ScriptedProvider([decision(ClickDecision(target=BUTTON))]),
        AllowlistPolicyEngine(),
        InMemoryEventSink(),
    )
    result = await engine.discover(request(max_steps=1))
    assert result.stop_reason == "max_steps"


@pytest.mark.asyncio
async def test_policy_violation_stops_before_action() -> None:
    policy = SafetyPolicy(allowed_origins=["http://demo.test/"], allowed_actions={"extract"})
    engine = DiscoveryEngine(
        FakeSurface([observation("one")]),
        ScriptedProvider([decision(ClickDecision(target=BUTTON))]),
        AllowlistPolicyEngine(),
        InMemoryEventSink(),
    )
    result = await engine.discover(request().model_copy(update={"safety_policy": policy}))
    assert result.stop_reason == "policy_violation"


@pytest.mark.asyncio
async def test_timeout_stops_slow_provider() -> None:
    engine = DiscoveryEngine(
        FakeSurface([observation("one")]),
        ScriptedProvider([decision(FinishDecision())], delay=0.05),
        AllowlistPolicyEngine(),
        InMemoryEventSink(),
    )
    result = await engine.discover(request(timeout_seconds=0.01))
    assert result.stop_reason == "timeout"


@pytest.mark.asyncio
async def test_unrecoverable_surface_error_is_structured() -> None:
    engine = DiscoveryEngine(
        FakeSurface([observation("one")], fail_click=True),
        ScriptedProvider([decision(ClickDecision(target=BUTTON))]),
        AllowlistPolicyEngine(),
        InMemoryEventSink(),
    )
    result = await engine.discover(request())
    assert result.stop_reason == "unrecoverable_error"
    assert "RuntimeError" in (result.error or "")


@pytest.mark.asyncio
async def test_request_human_is_terminal_without_surface_action() -> None:
    engine = DiscoveryEngine(
        FakeSurface([observation("one")]),
        ScriptedProvider([decision(RequestHumanDecision(message="Unexpected approval dialog"))]),
        AllowlistPolicyEngine(),
        InMemoryEventSink(),
    )
    result = await engine.discover(request())
    assert result.status == "human_required"
    assert result.stop_reason == "human_requested"
