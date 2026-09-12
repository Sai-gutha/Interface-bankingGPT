"""Deny-first safety policy and redaction tests."""

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from computer_use.artifacts.models import RiskLevel
from computer_use.observability.events import InMemoryEventSink, RunEvent
from computer_use.replay.engine import ReplayEngine, ReplayRequest, ReplayStatus
from computer_use.safety.policy import (
    ExplicitPolicyEngine,
    PolicyAction,
    PolicyContext,
    SafetyPolicy,
    classify_action,
)
from computer_use.safety.redactor import REDACTED, Redactor, SensitiveDataError
from computer_use.surfaces.base import Observation, Screenshot, SurfaceState
from computer_use.surfaces.playwright import PlaywrightSurfaceAdapter
from tests.unit.test_replay_engine import FakeEvidenceStore, example_artifact


@pytest.mark.asyncio
async def test_explicit_deny_wins_over_action_allowlist() -> None:
    decision = await ExplicitPolicyEngine().authorize(
        PolicyAction(action_type="click"),
        PolicyContext(run_id="run", current_url="https://bank.test/members", mode="replay"),
        SafetyPolicy(
            allowed_domains=["bank.test"],
            allowed_actions={"click"},
            denied_actions={"click"},
        ),
    )

    assert decision.allowed is False
    assert "explicitly denied" in decision.reason


@pytest.mark.asyncio
async def test_domain_and_route_must_both_be_allowed() -> None:
    engine = ExplicitPolicyEngine()
    policy = SafetyPolicy(
        allowed_domains=["bank.test"],
        allowed_routes=["/members/*"],
    )
    context = PolicyContext(
        run_id="run", current_url="https://bank.test/admin/users", mode="replay"
    )

    decision = await engine.authorize(PolicyAction(action_type="extract"), context, policy)

    assert decision.allowed is False
    assert "route" in decision.reason


@pytest.mark.asyncio
async def test_irreversible_action_requires_exact_action_approval() -> None:
    engine = ExplicitPolicyEngine()
    policy = SafetyPolicy(
        allowed_domains=["bank.test"],
        maximum_risk=RiskLevel.IRREVERSIBLE,
    )
    action = PolicyAction(
        action_type="click", action_id="submit_transfer", risk=RiskLevel.IRREVERSIBLE
    )
    context = PolicyContext(
        run_id="run", current_url="https://bank.test/transfers/review", mode="replay"
    )

    denied = await engine.authorize(action, context, policy)
    approved = await engine.authorize(
        action,
        context.model_copy(update={"approved_action_ids": {"submit_transfer"}}),
        policy,
    )

    assert denied.allowed is False
    assert denied.requires_human_approval is True
    assert approved.allowed is True


@pytest.mark.asyncio
async def test_forbidden_replay_action_never_reaches_surface_adapter() -> None:
    surface = AsyncMock(spec=PlaywrightSurfaceAdapter)
    surface.observe.return_value = Observation(
        state=SurfaceState(surface_kind="web", location="about:blank", title="Blank", ready=True),
        semantic_tree="",
        visible_text="",
        state_fingerprint="blank",
    )
    surface.screenshot.return_value = Screenshot(b"synthetic-png")
    artifact = example_artifact()
    result = await ReplayEngine(
        surface,
        ExplicitPolicyEngine(),
        FakeEvidenceStore(),
        InMemoryEventSink(),
    ).replay(
        ReplayRequest(
            artifact=artifact,
            parameters={"member_id": "12345"},
            safety_policy=SafetyPolicy(
                allowed_origins=artifact.target_application.allowed_origins,
                allowed_actions={"navigate"},
                denied_actions={"navigate"},
            ),
        )
    )

    assert result.status == ReplayStatus.HARD_FAILURE
    assert result.error_code == "POLICY_VIOLATION"
    surface.navigate.assert_not_awaited()
    surface.click.assert_not_awaited()
    surface.type.assert_not_awaited()


def test_action_classification_defaults_are_conservative() -> None:
    assert classify_action("extract") == RiskLevel.READ_ONLY
    assert classify_action("type") == RiskLevel.REVERSIBLE_WRITE
    assert classify_action("click", final_submission=True) == RiskLevel.IRREVERSIBLE


@pytest.mark.asyncio
async def test_event_sink_redacts_sensitive_keys_and_secret_patterns() -> None:
    sink = InMemoryEventSink(Redactor([SecretStr("member-secret-123")]))

    await sink.append(
        RunEvent(
            event_type="test",
            run_id="run",
            mode="replay",
            status="recorded",
            data={
                "password": "letmein",
                "note": "token member-secret-123 and Bearer abc.def.ghi",
            },
        )
    )

    assert sink.events[0].data["password"] == REDACTED
    assert "member-secret-123" not in sink.events[0].data["note"]
    assert "abc.def.ghi" not in sink.events[0].data["note"]


def test_redactor_rejects_secret_before_artifact_persistence() -> None:
    redactor = Redactor(["member-secret-123"])

    with pytest.raises(SensitiveDataError):
        redactor.assert_safe({"description": "contains member-secret-123"})
