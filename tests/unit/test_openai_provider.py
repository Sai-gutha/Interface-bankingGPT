"""The OpenAI adapter requests structured, non-persisted responses."""

from types import SimpleNamespace
from typing import cast

import pytest
from openai import AsyncOpenAI

from computer_use.discovery.engine import (
    AgentDecision,
    DecisionContext,
    FinishDecision,
)
from computer_use.discovery.openai_provider import OpenAIDecisionProvider
from computer_use.surfaces.base import Observation, SurfaceState


class FakeResponses:
    def __init__(self, parsed: AgentDecision) -> None:
        self.parsed = parsed
        self.kwargs: dict[str, object] = {}

    async def parse(self, **kwargs: object) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(output_parsed=self.parsed)


class FakeClient:
    def __init__(self, parsed: AgentDecision) -> None:
        self.responses = FakeResponses(parsed)


@pytest.mark.asyncio
async def test_provider_uses_pydantic_output_and_disables_storage() -> None:
    parsed = AgentDecision(
        action=FinishDecision(outputs={"balance": 10.0}),
        operational_reason="The requested balance is visible",
        confidence=0.98,
    )
    client = FakeClient(parsed)
    provider = OpenAIDecisionProvider(
        "test-model",
        client=cast(AsyncOpenAI, client),
    )
    context = DecisionContext(
        goal="Read balance",
        observation=Observation(
            state=SurfaceState(surface_kind="web", location="http://demo.test/"),
            semantic_tree="balance: $10.00",
            visible_text="$10.00",
            state_fingerprint="abc",
        ),
        allowed_actions={"extract", "finish"},
    )

    decision = await provider.decide(context)

    assert decision == parsed
    assert client.responses.kwargs["text_format"] is AgentDecision
    assert client.responses.kwargs["store"] is False
