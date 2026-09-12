"""Unit tests for the deterministic, provider-free replay interpreter."""

import json
from pathlib import Path
from typing import Literal

import pytest

from computer_use.artifacts.models import CapabilityArtifact, RiskLevel, Target
from computer_use.errors import AutomationCode, ErrorCategory, EvidenceKind
from computer_use.observability.events import InMemoryEventSink
from computer_use.observability.evidence import FailureEvidence
from computer_use.replay.engine import ReplayEngine, ReplayRequest, ReplayStatus
from computer_use.safety.policy import AllowlistPolicyEngine
from computer_use.surfaces.base import Observation, Screenshot, SurfaceState


def example_artifact() -> CapabilityArtifact:
    source = json.loads(Path("artifacts/examples/lookup_savings_balance.v1.json").read_text())
    return CapabilityArtifact.model_validate(source)


class FakeEvidenceStore:
    def __init__(self) -> None:
        self.saved: list[str] = []

    async def save_screenshot(self, run_id: str, name: str, screenshot: Screenshot) -> str:
        path = f"/evidence/{run_id}/screenshots/{name}.png"
        self.saved.append(path)
        return path

    async def save_failure(
        self,
        run_id: str,
        step_id: str,
        screenshot: Screenshot,
        observation: Observation | None,
        error: dict[str, object],
    ) -> FailureEvidence:
        directory = f"/evidence/{run_id}/failures/{step_id}"
        self.saved.append(directory)
        return FailureEvidence(
            directory=directory,
            screenshot_path=f"{directory}/screenshot.png",
            page_snapshot_path=f"{directory}/page.json",
            error_path=f"{directory}/error.json",
        )


class ReplaySurface:
    """Stateful fake that models the artifact's UI, not replay decisions."""

    def __init__(self, *, member_found: bool = True, fail_find_once: bool = False) -> None:
        self.page = "blank"
        self.member_found = member_found
        self.fail_find_once = fail_find_once
        self.find_attempts = 0
        self.member_value = ""
        self.actions: list[str] = []

    async def observe(self) -> Observation:
        location, title, text = {
            "blank": ("about:blank", "", ""),
            "login": (
                "http://127.0.0.1:8001/",
                "Operator Sign In",
                "AI-interface Banking Begin Secure Session",
            ),
            "search": (
                "http://127.0.0.1:8001/members",
                "Member Search",
                "Member Search Member ID Find Member",
            ),
            "details": (
                f"http://127.0.0.1:8001/members/{self.member_value}",
                "Member Details",
                "Member Details Deposit Accounts Savings $9,125.40",
            ),
            "not_found": (
                "http://127.0.0.1:8001/members/not-found",
                "No Matching Member",
                "Record not found This is a normal business outcome",
            ),
        }[self.page]
        return Observation(
            state=SurfaceState(surface_kind="web", location=location, title=title),
            semantic_tree=text,
            visible_text=text,
            state_fingerprint=f"{self.page}:{self.member_value}",
        )

    async def click(self, target: Target) -> None:
        self.actions.append(f"click:{target.description}")
        if "Begin Secure Session" in target.description:
            self.page = "search"
            return
        if "Find Member" in target.description:
            self.find_attempts += 1
            if self.fail_find_once and self.find_attempts == 1:
                raise TimeoutError("transient search timeout")
            self.page = "details" if self.member_found else "not_found"

    async def type(self, target: Target, value: str) -> None:
        self.actions.append(f"type:{target.description}")
        self.member_value = value

    async def select(self, target: Target, value: str) -> None:
        self.actions.append(f"select:{value}")

    async def navigate(self, url: str) -> None:
        self.actions.append(f"navigate:{url}")
        self.page = "login"

    async def extract(
        self,
        target: Target,
        source: Literal["inner_text", "value", "attribute"] = "inner_text",
        attribute_name: str | None = None,
    ) -> str:
        if source == "value":
            return self.member_value
        return "$9,125.40"

    async def screenshot(self) -> Screenshot:
        return Screenshot(b"synthetic-png")

    async def wait_for(
        self,
        target: Target,
        timeout_ms: int = 10_000,
        *,
        state: Literal["visible", "hidden"] = "visible",
    ) -> None:
        return None

    async def get_current_state(self) -> SurfaceState:
        return (await self.observe()).state

    async def close(self) -> None:
        return None


def engine(surface: ReplaySurface, evidence: FakeEvidenceStore | None = None) -> ReplayEngine:
    return ReplayEngine(
        surface,
        AllowlistPolicyEngine(),
        evidence or FakeEvidenceStore(),
        InMemoryEventSink(),
    )


@pytest.mark.asyncio
async def test_success_binds_parameters_executes_order_and_collects_typed_output() -> None:
    surface = ReplaySurface()

    result = await engine(surface).replay(
        ReplayRequest(artifact=example_artifact(), parameters={"member_id": "12345"})
    )

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs == {"savings_balance": 9125.4}
    assert surface.actions == [
        "navigate:http://127.0.0.1:8001/",
        "click:Begin Secure Session button",
        "type:Member ID search field",
        "click:Find Member button",
    ]
    assert result.error_code is None


@pytest.mark.asyncio
async def test_invalid_parameter_is_a_legitimate_business_outcome() -> None:
    surface = ReplaySurface()

    result = await engine(surface).replay(
        ReplayRequest(artifact=example_artifact(), parameters={"member_id": "abc"})
    )

    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome_code == AutomationCode.INVALID_INPUT
    assert result.error_category == ErrorCategory.BUSINESS_OUTCOME
    assert result.legitimate_result is True
    assert result.retry_allowed is False
    assert surface.actions == []


@pytest.mark.asyncio
async def test_business_outcome_is_not_a_failure() -> None:
    surface = ReplaySurface(member_found=False)

    result = await engine(surface).replay(
        ReplayRequest(artifact=example_artifact(), parameters={"member_id": "55555"})
    )

    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome_code == AutomationCode.MEMBER_NOT_FOUND
    assert result.legitimate_result is True
    assert result.evidence_requirements == [EvidenceKind.EVENT, EvidenceKind.OBSERVED_STATE]
    assert result.outputs == {"savings_balance": None}


@pytest.mark.asyncio
async def test_declared_retry_returns_recovered_success() -> None:
    surface = ReplaySurface(fail_find_once=True)

    result = await engine(surface).replay(
        ReplayRequest(artifact=example_artifact(), parameters={"member_id": "12345"})
    )

    assert result.status == ReplayStatus.RECOVERED_SUCCESS
    assert result.recovery_count == 1
    assert surface.find_attempts == 2


@pytest.mark.asyncio
async def test_checkpoint_failure_reports_expected_observed_and_evidence() -> None:
    surface = ReplaySurface()
    artifact = example_artifact()
    artifact.success_conditions[0].pattern = "^https://wrong.example/$"
    evidence = FakeEvidenceStore()

    result = await engine(surface, evidence).replay(
        ReplayRequest(artifact=artifact, parameters={"member_id": "12345"})
    )

    assert result.status == ReplayStatus.HARD_FAILURE
    assert result.error_code == AutomationCode.SUCCESS_CONDITION_FAILED
    assert result.error_category == ErrorCategory.HARD_FAILURE
    assert result.retry_allowed is False
    assert result.expected_state
    assert result.observed_state is not None
    assert result.observed_state.location.endswith("/members/12345")
    assert result.evidence_path == evidence.saved[0]


@pytest.mark.asyncio
async def test_irreversible_step_requests_human_without_executing_it() -> None:
    artifact = example_artifact()
    artifact.steps[0].risk = RiskLevel.IRREVERSIBLE_WRITE
    artifact.risk = RiskLevel.IRREVERSIBLE_WRITE
    surface = ReplaySurface()

    result = await engine(surface).replay(
        ReplayRequest(artifact=artifact, parameters={"member_id": "12345"})
    )

    assert result.status == ReplayStatus.HUMAN_REQUIRED
    assert result.failed_step == artifact.steps[0].step_id
    assert result.error_code == AutomationCode.IRREVERSIBLE_ACTION_REQUIRES_APPROVAL
    assert result.human_recommended is True
    assert surface.actions == []


@pytest.mark.asyncio
async def test_explicit_step_approval_allows_irreversible_action() -> None:
    artifact = example_artifact()
    artifact.steps[0].risk = RiskLevel.IRREVERSIBLE
    artifact.risk = RiskLevel.IRREVERSIBLE
    surface = ReplaySurface()

    result = await engine(surface).replay(
        ReplayRequest(
            artifact=artifact,
            parameters={"member_id": "12345"},
            approved_action_ids={artifact.steps[0].step_id},
        )
    )

    assert result.status == ReplayStatus.SUCCESS
    assert surface.actions[0] == "navigate:http://127.0.0.1:8001/"
