"""Same-session human takeover, logging, resume, and operator API tests."""

import asyncio

import httpx
import pytest

from computer_use.api.app import create_app
from computer_use.artifacts.models import LabelLocator, Target
from computer_use.handoff import (
    ControlOwner,
    HumanAction,
    InMemoryHandoffCoordinator,
    ResumeSignal,
)
from computer_use.observability.events import InMemoryEventSink
from computer_use.replay.engine import ReplayEngine, ReplayRequest, ReplayStatus
from computer_use.safety.policy import ExplicitPolicyEngine
from tests.unit.test_replay_engine import FakeEvidenceStore, ReplaySurface, example_artifact


@pytest.mark.asyncio
async def test_takeover_operates_exact_same_surface_and_resumes_automation() -> None:
    events = InMemoryEventSink()
    coordinator = InMemoryHandoffCoordinator(events)
    surface = ReplaySurface()
    original_identity = id(surface)
    await coordinator.register(
        "replay_fixed",
        surface,
        mode="replay",
        capability_id="lookup_savings_balance",
    )

    intervention = await coordinator.request(
        "replay_fixed",
        current_step="enter_member_id",
        reason_code="SESSION_EXPIRED",
        reason="Operator authentication is required",
    )
    assert intervention.screenshot_data_url.startswith("data:image/png;base64,")
    assert intervention.current_url == "about:blank"
    assert intervention.capability_id == "lookup_savings_balance"

    lease = await coordinator.claim("replay_fixed", "operator-7", intervention.epoch)
    target = Target(
        description="Member ID",
        candidates=[LabelLocator(label="Member ID")],
    )
    await coordinator.perform(
        "replay_fixed",
        "operator-7",
        lease.epoch,
        HumanAction(kind="type", target=target, value="12345"),
    )
    waiter = asyncio.create_task(coordinator.wait_for_resume("replay_fixed"))
    await asyncio.sleep(0)
    assert waiter.done() is False

    resumed = await coordinator.resume(
        ResumeSignal(
            run_id="replay_fixed",
            epoch=lease.epoch,
            operator_id="operator-7",
            current_step_completed=True,
        )
    )
    signal = await waiter

    assert id(surface) == original_identity
    assert surface.member_value == "12345"
    assert resumed.owner == ControlOwner.AUTOMATION
    assert signal.current_step_completed is True
    assert {event.event_type for event in events.events} >= {
        "intervention_requested",
        "intervention_claimed",
        "human_action",
        "automation_resumed",
    }
    human_event = next(event for event in events.events if event.event_type == "human_action")
    assert human_event.data["operator_id"] == "operator-7"


@pytest.mark.asyncio
async def test_operator_page_shows_intervention_and_controls() -> None:
    coordinator = InMemoryHandoffCoordinator(InMemoryEventSink())
    await coordinator.register(
        "discovery_fixed",
        ReplaySurface(),
        mode="discovery",
        goal="Look up the member",
    )
    await coordinator.request(
        "discovery_fixed",
        current_step="2",
        reason_code="HUMAN_REQUESTED",
        reason="Resolve the visible dialog",
    )
    transport = httpx.ASGITransport(app=create_app(coordinator))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/operator/discovery_fixed")

    assert response.status_code == 200
    assert "Look up the member" in response.text
    assert "Take control" in response.text
    assert "RESUME" in response.text
    assert "data:image/png;base64" in response.text


@pytest.mark.asyncio
async def test_replay_pauses_then_continues_after_human_completes_current_step() -> None:
    events = InMemoryEventSink()
    coordinator = InMemoryHandoffCoordinator(events)
    surface = ReplaySurface()
    artifact = example_artifact()
    artifact.steps[0].risk = "irreversible_write"
    artifact.risk = "irreversible_write"
    replay = ReplayEngine(
        surface,
        ExplicitPolicyEngine(),
        FakeEvidenceStore(),
        events,
        coordinator,
    )

    task = asyncio.create_task(
        replay.replay(ReplayRequest(artifact=artifact, parameters={"member_id": "12345"}))
    )
    interventions = []
    for _ in range(20):
        interventions = await coordinator.list_active()
        if interventions:
            break
        await asyncio.sleep(0)
    intervention = interventions[0]
    lease = await coordinator.claim(intervention.run_id, "operator-9", intervention.epoch)
    await coordinator.perform(
        intervention.run_id,
        "operator-9",
        lease.epoch,
        HumanAction(kind="navigate", url="http://127.0.0.1:8001/"),
    )
    await coordinator.resume(
        ResumeSignal(
            run_id=intervention.run_id,
            epoch=lease.epoch,
            operator_id="operator-9",
            current_step_completed=True,
        )
    )

    result = await task

    assert result.status == ReplayStatus.SUCCESS
    assert surface.actions[0] == "navigate:http://127.0.0.1:8001/"
    assert any(event.event_type == "human_step_accepted" for event in events.events)
