"""LLM-driven observe-decide-validate-act discovery loop."""

import asyncio
from time import perf_counter
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, PositiveInt

from computer_use.artifacts.models import RiskLevel, Target
from computer_use.handoff.coordinator import InMemoryHandoffCoordinator
from computer_use.observability.events import EventSink, RunEvent
from computer_use.observability.evidence import EvidenceStore
from computer_use.safety.policy import (
    PolicyAction,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    SafetyPolicy,
    classify_action,
)
from computer_use.surfaces.base import Observation, SurfaceAdapter


class NavigateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["navigate"] = "navigate"
    url: str


class ClickDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["click"] = "click"
    target: Target


class TypeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["type"] = "type"
    target: Target
    value: str


class ExtractDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["extract"] = "extract"
    target: Target
    output_name: str


class WaitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["wait"] = "wait"
    target: Target
    timeout_ms: PositiveInt = 10_000


class FinishDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["finish"] = "finish"
    outputs: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class RequestHumanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["request_human"] = "request_human"
    message: str = Field(min_length=1, max_length=500)
    target: Target | None = None


DecisionAction = Annotated[
    NavigateDecision
    | ClickDecision
    | TypeDecision
    | ExtractDecision
    | WaitDecision
    | FinishDecision
    | RequestHumanDecision,
    Field(discriminator="kind"),
]


class AgentDecision(BaseModel):
    """One schema-validated model response with no chain-of-thought field."""

    model_config = ConfigDict(extra="forbid")
    action: DecisionAction
    operational_reason: str = Field(min_length=1, max_length=240)
    confidence: float = Field(ge=0.0, le=1.0)


class DiscoveryRequest(BaseModel):
    """Goal, entry point, and safety envelope for one discovery run."""

    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=2_000)
    starting_url: AnyHttpUrl
    safety_policy: SafetyPolicy
    max_steps: PositiveInt = 20
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    repeated_state_limit: int = Field(default=3, ge=2, le=10)


class DecisionContext(BaseModel):
    """Bounded context sent to the model; raw transcripts are never accumulated."""

    model_config = ConfigDict(extra="forbid")
    goal: str
    observation: Observation
    recent_actions: list[str] = Field(default_factory=list, max_length=8)
    extracted_values: dict[str, str] = Field(default_factory=dict)
    allowed_actions: set[str]


class DecisionProvider(Protocol):
    """Provider that must return a Pydantic-validated structured decision."""

    async def decide(self, context: DecisionContext) -> AgentDecision: ...


class RecordedAction(BaseModel):
    """Neutral compiler input containing observable facts and a concise reason."""

    model_config = ConfigDict(extra="forbid")
    sequence: PositiveInt
    decision: AgentDecision
    before: Observation
    after: Observation
    extracted_value: str | None = None


StopReason = Literal[
    "goal_complete",
    "max_steps",
    "timeout",
    "repeated_state",
    "policy_violation",
    "unrecoverable_error",
    "human_requested",
]


class DiscoveryResult(BaseModel):
    """Complete discovery outcome and neutral recording for later compilation."""

    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: Literal["success", "stopped", "human_required"]
    stop_reason: StopReason
    outputs: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    recording: list[RecordedAction] = Field(default_factory=list)
    final_observation: Observation | None = None
    error: str | None = None
    evidence_path: str | None = None


class DiscoveryEngine:
    """Own stopping semantics while providers choose only one structured action at a time."""

    def __init__(
        self,
        surface: SurfaceAdapter,
        decisions: DecisionProvider,
        policy: PolicyEngine,
        events: EventSink,
        evidence: EvidenceStore | None = None,
        handoff: InMemoryHandoffCoordinator | None = None,
    ) -> None:
        self._surface = surface
        self._decisions = decisions
        self._policy = policy
        self._events = events
        self._evidence = evidence
        self._handoff = handoff

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        """Run discovery until success, a safe stopping condition, or human escalation."""

        run_id = f"discovery_{uuid4().hex}"
        if self._handoff is not None:
            await self._handoff.register(
                run_id,
                self._surface,
                mode="discovery",
                goal=request.goal,
            )
        try:
            async with asyncio.timeout(request.timeout_seconds):
                return await self._run(run_id, request)
        except TimeoutError:
            await self._emit(run_id, "run_stopped", {"reason": "timeout"})
            return DiscoveryResult(
                run_id=run_id,
                status="stopped",
                stop_reason="timeout",
                error="discovery exceeded its configured wall-clock timeout",
            )

    async def _run(self, run_id: str, request: DiscoveryRequest) -> DiscoveryResult:
        await self._emit(
            run_id,
            "run_started",
            {
                "goal": request.goal,
                "starting_url": str(request.starting_url),
                "max_steps": request.max_steps,
            },
        )
        initial_navigation = PolicyAction(
            action_type="navigate", url=str(request.starting_url), risk=RiskLevel.READ
        )
        initial_policy = await self._authorize(run_id, initial_navigation, request)
        if not initial_policy.allowed:
            return await self._policy_stop(run_id, initial_policy.reason)

        try:
            await self._surface.navigate(str(request.starting_url))
            observation = await self._surface.observe()
        except Exception as error:
            return await self._error_stop(run_id, error, None)
        await self._record_observation(run_id, observation, sequence=0)

        recording: list[RecordedAction] = []
        outputs: dict[str, str | int | float | bool | None] = {}
        state_visits = {observation.state_fingerprint: 1}
        recent_actions: list[str] = []

        for sequence in range(1, request.max_steps + 1):
            context = DecisionContext(
                goal=request.goal,
                observation=observation,
                recent_actions=recent_actions[-8:],
                extracted_values={key: str(value) for key, value in outputs.items()},
                allowed_actions=set(request.safety_policy.allowed_actions)
                | {"finish", "request_human"},
            )
            try:
                decision = await self._decisions.decide(context)
            except Exception as error:
                return await self._error_stop(run_id, error, observation, recording)

            await self._emit(
                run_id,
                "decision_made",
                {
                    "sequence": sequence,
                    "action": decision.action.kind,
                    "operational_reason": decision.operational_reason,
                    "confidence": decision.confidence,
                },
                step_id=str(sequence),
            )

            if isinstance(decision.action, FinishDecision):
                outputs.update(decision.action.outputs)
                await self._emit(run_id, "run_completed", {"reason": "goal_complete"})
                return DiscoveryResult(
                    run_id=run_id,
                    status="success",
                    stop_reason="goal_complete",
                    outputs=outputs,
                    recording=recording,
                    final_observation=observation,
                )
            if isinstance(decision.action, RequestHumanDecision):
                if self._handoff is not None:
                    await self._handoff.request(
                        run_id,
                        current_step=str(sequence),
                        reason_code="HUMAN_REQUESTED",
                        reason=decision.action.message,
                    )
                    await self._handoff.wait_for_resume(run_id)
                    observation = await self._surface.observe()
                    await self._record_observation(run_id, observation, sequence=sequence)
                    continue
                await self._emit(
                    run_id,
                    "human_requested",
                    {"message": decision.action.message, "sequence": sequence},
                )
                return DiscoveryResult(
                    run_id=run_id,
                    status="human_required",
                    stop_reason="human_requested",
                    outputs=outputs,
                    recording=recording,
                    final_observation=observation,
                )

            policy_action = self._to_policy_action(decision)
            policy_decision = await self._authorize(run_id, policy_action, request)
            if not policy_decision.allowed:
                return await self._policy_stop(
                    run_id, policy_decision.reason, observation, recording
                )

            before = observation
            started = perf_counter()
            try:
                extracted = await self._act(decision)
                if isinstance(decision.action, ExtractDecision):
                    outputs[decision.action.output_name] = extracted
                observation = await self._surface.observe()
            except Exception as error:
                return await self._error_stop(run_id, error, before, recording)

            recording.append(
                RecordedAction(
                    sequence=sequence,
                    decision=decision,
                    before=before,
                    after=observation,
                    extracted_value=extracted,
                )
            )
            recent_actions.append(f"{decision.action.kind}: {decision.operational_reason[:120]}")
            await self._record_observation(run_id, observation, sequence=sequence)
            await self._emit(
                run_id,
                "action_completed",
                {"sequence": sequence, "action": decision.action.kind},
                step_id=str(sequence),
                action=decision.action.kind,
                locator_strategy=self._last_locator_strategy(),
                status="succeeded",
                duration_ms=(perf_counter() - started) * 1000,
            )

            visits = state_visits.get(observation.state_fingerprint, 0) + 1
            state_visits[observation.state_fingerprint] = visits
            if visits >= request.repeated_state_limit:
                await self._emit(run_id, "run_stopped", {"reason": "repeated_state"})
                return DiscoveryResult(
                    run_id=run_id,
                    status="stopped",
                    stop_reason="repeated_state",
                    outputs=outputs,
                    recording=recording,
                    final_observation=observation,
                )

        await self._emit(run_id, "run_stopped", {"reason": "max_steps"})
        return DiscoveryResult(
            run_id=run_id,
            status="stopped",
            stop_reason="max_steps",
            outputs=outputs,
            recording=recording,
            final_observation=observation,
        )

    async def _act(self, decision: AgentDecision) -> str | None:
        action = decision.action
        if isinstance(action, NavigateDecision):
            await self._surface.navigate(action.url)
        elif isinstance(action, ClickDecision):
            await self._surface.click(action.target)
        elif isinstance(action, TypeDecision):
            await self._surface.type(action.target, action.value)
        elif isinstance(action, ExtractDecision):
            return await self._surface.extract(action.target)
        elif isinstance(action, WaitDecision):
            await self._surface.wait_for(action.target, action.timeout_ms)
        else:  # finish and request_human are handled before policy and execution
            raise TypeError(f"unsupported executable action: {type(action).__name__}")
        return None

    @staticmethod
    def _to_policy_action(decision: AgentDecision) -> PolicyAction:
        action = decision.action
        if isinstance(action, NavigateDecision):
            return PolicyAction(
                action_type="navigate",
                url=action.url,
                risk=classify_action("navigate"),
            )
        if isinstance(action, TypeDecision):
            return PolicyAction(
                action_type="type",
                target=action.target,
                value=action.value,
                risk=classify_action("type"),
            )
        if isinstance(action, ClickDecision):
            return PolicyAction(
                action_type="click", target=action.target, risk=classify_action("click")
            )
        if isinstance(action, ExtractDecision):
            return PolicyAction(
                action_type="extract", target=action.target, risk=classify_action("extract")
            )
        if isinstance(action, WaitDecision):
            return PolicyAction(
                action_type="wait", target=action.target, risk=classify_action("wait")
            )
        raise TypeError(f"terminal action has no policy operation: {type(action).__name__}")

    async def _authorize(
        self,
        run_id: str,
        action: PolicyAction,
        request: DiscoveryRequest,
    ) -> PolicyDecision:
        state = await self._surface.get_current_state()
        decision = await self._policy.authorize(
            action,
            PolicyContext(run_id=run_id, current_url=state.location, mode="discovery"),
            request.safety_policy,
        )
        await self._emit(
            run_id,
            "policy_decision",
            {"action": action.action_type, "allowed": decision.allowed, "reason": decision.reason},
        )
        return decision

    async def _policy_stop(
        self,
        run_id: str,
        reason: str,
        observation: Observation | None = None,
        recording: list[RecordedAction] | None = None,
    ) -> DiscoveryResult:
        evidence_path = await self._save_failure(
            run_id, "policy", observation, "POLICY_VIOLATION", reason
        )
        await self._emit(
            run_id,
            "run_stopped",
            {"reason": "policy_violation", "detail": reason},
            status="failed",
            error_code="POLICY_VIOLATION",
        )
        return DiscoveryResult(
            run_id=run_id,
            status="stopped",
            stop_reason="policy_violation",
            recording=recording or [],
            final_observation=observation,
            error=reason,
            evidence_path=evidence_path,
        )

    async def _error_stop(
        self,
        run_id: str,
        error: Exception,
        observation: Observation | None,
        recording: list[RecordedAction] | None = None,
    ) -> DiscoveryResult:
        detail = f"{type(error).__name__}: {error}"
        evidence_path = await self._save_failure(
            run_id, "runtime", observation, "UNRECOVERABLE_ERROR", detail[:500]
        )
        await self._emit(
            run_id,
            "run_stopped",
            {"reason": "unrecoverable_error", "error_type": type(error).__name__},
            status="failed",
            error_code="UNRECOVERABLE_ERROR",
        )
        return DiscoveryResult(
            run_id=run_id,
            status="stopped",
            stop_reason="unrecoverable_error",
            recording=recording or [],
            final_observation=observation,
            error=detail[:500],
            evidence_path=evidence_path,
        )

    async def _save_failure(
        self,
        run_id: str,
        step_id: str,
        observation: Observation | None,
        code: str,
        message: str,
    ) -> str | None:
        if self._evidence is None:
            return None
        try:
            bundle = await self._evidence.save_failure(
                run_id,
                step_id,
                await self._surface.screenshot(),
                observation,
                {"code": code, "message": message},
            )
            return bundle.directory
        except Exception:
            return None

    async def _record_observation(
        self, run_id: str, observation: Observation, *, sequence: int
    ) -> None:
        await self._emit(
            run_id,
            "observation_captured",
            {
                "sequence": sequence,
                "location": observation.state.location,
                "title": observation.state.title,
                "state_fingerprint": observation.state_fingerprint,
                "evidence": observation.metadata,
            },
            step_id=str(sequence),
        )

    async def _emit(
        self,
        run_id: str,
        event_type: str,
        data: dict[str, object],
        *,
        step_id: str | None = None,
        action: str | None = None,
        locator_strategy: str | None = None,
        status: str = "recorded",
        duration_ms: float | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._events.append(
            RunEvent(
                event_type=event_type,
                run_id=run_id,
                mode="discovery",
                step_id=step_id,
                action=action,
                locator_strategy=locator_strategy,
                status=status,
                duration_ms=duration_ms,
                error_code=error_code,
                data=data,
            )
        )

    def _last_locator_strategy(self) -> str | None:
        records = getattr(self._surface, "resolution_records", ())
        if not isinstance(records, (list, tuple)) or not records:
            return None
        strategy = getattr(records[-1], "strategy", None)
        return strategy if isinstance(strategy, str) else None
