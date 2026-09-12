"""OpenAI Responses API adapter for schema-constrained discovery decisions."""

import json

from openai import AsyncOpenAI
from pydantic import SecretStr

from computer_use.discovery.engine import AgentDecision, DecisionContext

INSTRUCTIONS = """You operate a UI one action at a time to achieve the supplied goal.
Return exactly one action through the provided structured schema. Prefer accessibility role/name,
label, visible text, stable attributes, and relative-text targets in that order. Use CSS only as a
fallback and coordinates only when no semantic target exists. Never submit an irreversible action
unless it is explicitly permitted. Request a human when the safe next action is ambiguous.
Use operational_reason only for a short, observable justification such as 'Open member search'.
Do not provide hidden reasoning, analysis, a transcript, credentials, or sensitive values there.
Use finish only when the latest observation visibly proves the goal is complete.
"""


class OpenAIDecisionProvider:
    """Request Pydantic-validated actions while disabling provider-side response storage."""

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
        api_key: SecretStr | None = None,
    ) -> None:
        self._model = model
        self._client = client or AsyncOpenAI(
            api_key=api_key.get_secret_value() if api_key is not None else None
        )

    async def decide(self, context: DecisionContext) -> AgentDecision:
        """Return one structured decision; raw response content is never retained."""

        payload = {
            "goal": context.goal,
            "allowed_actions": sorted(context.allowed_actions),
            "current_state": context.observation.state.model_dump(mode="json"),
            "semantic_tree": context.observation.semantic_tree,
            "visible_text": context.observation.visible_text,
            "recent_actions": context.recent_actions,
            "extracted_values": context.extracted_values,
        }
        response = await self._client.responses.parse(
            model=self._model,
            instructions=INSTRUCTIONS,
            input=json.dumps(payload),
            text_format=AgentDecision,
            store=False,
        )
        if response.output_parsed is None:
            raise ValueError("model returned no structured discovery decision")
        return response.output_parsed
