"""Deterministic, LLM-free interpreter for versioned capability artifacts."""

import asyncio
import re
from enum import StrEnum
from time import perf_counter
from typing import TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from computer_use.artifacts.models import (
    TEMPLATE_REFERENCE,
    Action,
    BusinessOutcome,
    CapabilityArtifact,
    ClickAction,
    Condition,
    ElementCondition,
    ExtractAction,
    ExtractionRule,
    ExtractionTransform,
    InputParameter,
    NavigateAction,
    OutputParameter,
    RiskLevel,
    SelectAction,
    Step,
    TextCondition,
    TypeAction,
    UrlCondition,
    ValueCondition,
    ValueType,
    WaitAction,
)
from computer_use.errors import (
    AutomationCode,
    ErrorCategory,
    EvidenceKind,
    TaxonomyEntry,
    classify,
)
from computer_use.handoff.coordinator import InMemoryHandoffCoordinator
from computer_use.locators.strategy import (
    AmbiguousTargetError,
    SurfaceError,
    TargetNotFoundError,
    UnsupportedTargetError,
)
from computer_use.observability.events import EventSink, RunEvent
from computer_use.observability.evidence import EvidenceStore
from computer_use.safety.policy import PolicyAction, PolicyContext, PolicyEngine, SafetyPolicy
from computer_use.surfaces.base import Observation, SurfaceAdapter

type JsonScalar = str | int | float | bool | None
ModelT = TypeVar("ModelT", bound=BaseModel)
CONDITION_ADAPTER: TypeAdapter[Condition] = TypeAdapter(Condition)


class ReplayStatus(StrEnum):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    RECOVERED_SUCCESS = "RECOVERED_SUCCESS"
    HARD_FAILURE = "HARD_FAILURE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class ObservedState(BaseModel):
    """Bounded state information returned to callers without raw DOM or screenshots."""

    model_config = ConfigDict(extra="forbid")
    location: str
    title: str | None
    state_fingerprint: str
    visible_text_summary: str


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: CapabilityArtifact
    parameters: dict[str, JsonScalar] = Field(default_factory=dict)
    approved_action_ids: set[str] = Field(default_factory=set)
    safety_policy: SafetyPolicy | None = None


class ReplayResult(BaseModel):
    """Typed terminal contract for deterministic capability invocation."""

    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: ReplayStatus
    capability_id: str
    capability_version: str
    outputs: dict[str, JsonScalar] = Field(default_factory=dict)
    business_outcome_code: AutomationCode | None = None
    failed_step: str | None = None
    expected_state: list[str] = Field(default_factory=list)
    observed_state: ObservedState | None = None
    error_code: AutomationCode | None = None
    error_category: ErrorCategory | None = None
    retry_allowed: bool = False
    legitimate_result: bool = False
    human_recommended: bool = False
    evidence_requirements: list[EvidenceKind] = Field(default_factory=list)
    message: str | None = None
    evidence_path: str | None = None
    recovery_count: int = 0


class ParameterValidationError(ValueError):
    pass


class CheckpointError(RuntimeError):
    def __init__(self, failed: list[Condition]) -> None:
        super().__init__("one or more checkpoint conditions were not satisfied")
        self.failed = failed


class ReplayEngine:
    """Execute only validated artifact instructions; no decision provider is accepted."""

    def __init__(
        self,
        surface: SurfaceAdapter,
        policy: PolicyEngine,
        evidence: EvidenceStore,
        events: EventSink | None = None,
        handoff: InMemoryHandoffCoordinator | None = None,
    ) -> None:
        self._surface = surface
        self._policy = policy
        self._evidence = evidence
        self._events = events
        self._handoff = handoff
        self._capability_id: str | None = None

    async def replay(self, request: ReplayRequest) -> ReplayResult:
        run_id = f"replay_{uuid4().hex}"
        artifact = request.artifact
        self._capability_id = artifact.capability_id
        if self._handoff is not None:
            await self._handoff.register(
                run_id,
                self._surface,
                mode="replay",
                capability_id=artifact.capability_id,
            )
        try:
            parameters = self._validate_parameters(artifact.inputs, request.parameters)
        except ParameterValidationError as error:
            taxonomy = classify(AutomationCode.INVALID_INPUT)
            return self._result(
                run_id,
                artifact,
                ReplayStatus.BUSINESS_OUTCOME,
                business_outcome_code=AutomationCode.INVALID_INPUT,
                error_code=AutomationCode.INVALID_INPUT,
                message=str(error),
                **self._taxonomy_fields(taxonomy),
            )

        await self._emit(
            run_id,
            "replay_started",
            {"capability_id": artifact.capability_id, "parameter_names": sorted(parameters)},
        )
        policy = request.safety_policy or SafetyPolicy(
            allowed_origins=artifact.target_application.allowed_origins,
            allowed_actions={step.action.kind for step in artifact.steps},
            maximum_risk=artifact.risk,
            allow_coordinate_targets=False,
        )
        outputs: dict[str, JsonScalar] = {}
        recovery_count = 0
        approvals = set(request.approved_action_ids)
        last_observation: Observation | None = None

        for stored_step in artifact.steps:
            step = self._bind_step(stored_step, parameters)
            try:
                last_observation = await self._surface.observe()
            except Exception as error:
                return await self._hard_failure(
                    run_id,
                    artifact,
                    step,
                    [],
                    None,
                    AutomationCode.OBSERVATION_FAILED,
                    error,
                )

            outcome = await self._match_business_outcome(
                artifact.business_outcomes, parameters, last_observation
            )
            if outcome is not None:
                return await self._business_result(
                    run_id, artifact, outcome, parameters, last_observation, recovery_count
                )

            failed_preconditions = await self._failed_conditions(
                step.preconditions, last_observation, step.timeout_ms
            )
            if failed_preconditions:
                return await self._hard_failure(
                    run_id,
                    artifact,
                    step,
                    failed_preconditions,
                    last_observation,
                    AutomationCode.PRECONDITION_FAILED,
                )

            policy_action = self._policy_action(step.action, step.risk, step.step_id)
            policy_decision = await self._policy.authorize(
                policy_action,
                PolicyContext(
                    run_id=run_id,
                    current_url=last_observation.state.location,
                    mode="replay",
                    approved_action_ids=approvals,
                ),
                policy,
            )
            await self._emit(
                run_id,
                "policy_decision",
                {
                    "step_id": step.step_id,
                    "allowed": policy_decision.allowed,
                    "reason": policy_decision.reason,
                },
                step.step_id,
            )
            if not policy_decision.allowed:
                if policy_decision.requires_human_approval:
                    if self._handoff is None:
                        return await self._human_required(
                            run_id,
                            artifact,
                            step,
                            last_observation,
                            AutomationCode.IRREVERSIBLE_ACTION_REQUIRES_APPROVAL,
                            policy_decision.reason,
                        )
                    await self._handoff.request(
                        run_id,
                        current_step=step.step_id,
                        reason_code=AutomationCode.IRREVERSIBLE_ACTION_REQUIRES_APPROVAL.value,
                        reason=policy_decision.reason,
                    )
                    signal = await self._handoff.wait_for_resume(run_id)
                    resumed_observation = await self._surface.observe()
                    if signal.current_step_completed:
                        failed = await self._failed_conditions(
                            step.postconditions, resumed_observation, step.timeout_ms
                        )
                        if failed:
                            return await self._hard_failure(
                                run_id,
                                artifact,
                                step,
                                failed,
                                resumed_observation,
                                AutomationCode.CHECKPOINT_FAILED,
                                message="human signaled completion but checkpoint did not pass",
                            )
                        last_observation = resumed_observation
                        await self._emit(
                            run_id,
                            "human_step_accepted",
                            {"operator_id": signal.operator_id},
                            step.step_id,
                            action=step.action.kind,
                            status="succeeded",
                        )
                        continue
                    approvals.add(step.step_id)
                    policy_decision = await self._policy.authorize(
                        policy_action,
                        PolicyContext(
                            run_id=run_id,
                            current_url=resumed_observation.state.location,
                            mode="replay",
                            approved_action_ids=approvals,
                        ),
                        policy,
                    )
                    if policy_decision.allowed:
                        last_observation = resumed_observation
                    else:
                        return await self._hard_failure(
                            run_id,
                            artifact,
                            step,
                            step.preconditions,
                            resumed_observation,
                            AutomationCode.POLICY_VIOLATION,
                            message=policy_decision.reason,
                        )
                else:
                    return await self._hard_failure(
                        run_id,
                        artifact,
                        step,
                        step.preconditions,
                        last_observation,
                        AutomationCode.POLICY_VIOLATION,
                        message=policy_decision.reason,
                    )

            attempt = 0
            recovery_uses: dict[str, int] = {}
            while True:
                attempt += 1
                started = perf_counter()
                try:
                    extracted = await self._execute(step.action)
                    current = await self._surface.observe()
                    outcome = await self._match_business_outcome(
                        artifact.business_outcomes, parameters, current
                    )
                    if outcome is not None:
                        return await self._business_result(
                            run_id, artifact, outcome, parameters, current, recovery_count
                        )
                    failed = await self._failed_conditions(
                        step.postconditions, current, step.timeout_ms
                    )
                    if failed:
                        raise CheckpointError(failed)
                    if isinstance(step.action, ExtractAction):
                        outputs[step.action.rule.output_name] = self._coerce_output(
                            self._transform_extraction(extracted or "", step.action.rule),
                            self._output_definition(artifact.outputs, step.action.rule.output_name),
                        )
                    last_observation = current
                    await self._emit(
                        run_id,
                        "step_completed",
                        {"step_id": step.step_id, "attempt": attempt},
                        step.step_id,
                        action=step.action.kind,
                        locator_strategy=self._last_locator_strategy(),
                        status="succeeded",
                        duration_ms=(perf_counter() - started) * 1000,
                    )
                    break
                except Exception as error:
                    current = await self._safe_observe(last_observation)
                    outcome = await self._match_business_outcome(
                        artifact.business_outcomes, parameters, current
                    )
                    if outcome is not None:
                        return await self._business_result(
                            run_id, artifact, outcome, parameters, current, recovery_count
                        )
                    error_code = self._error_code(error, current)
                    taxonomy = classify(error_code)
                    condition_names = self._artifact_condition_names(error_code)
                    await self._emit(
                        run_id,
                        "runtime_condition",
                        {
                            "code": error_code.value,
                            "category": taxonomy.category.value,
                            "retry_allowed": taxonomy.retry_allowed,
                        },
                        step.step_id,
                        action=step.action.kind,
                        locator_strategy=self._last_locator_strategy(),
                        status="failed",
                        duration_ms=(perf_counter() - started) * 1000,
                        error_code=error_code.value,
                    )
                    directive = next(
                        (
                            item
                            for item in step.recovery.directives
                            if item.on in condition_names
                            and recovery_uses.get(item.on, 0) < item.max_uses
                        ),
                        None,
                    )
                    if directive is not None:
                        recovery_uses[directive.on] = recovery_uses.get(directive.on, 0) + 1
                        if directive.strategy == "request_handoff":
                            return await self._human_required(
                                run_id,
                                artifact,
                                step,
                                current,
                                error_code,
                                str(error),
                            )
                        if directive.strategy == "retry_step":
                            if taxonomy.retry_allowed:
                                recovery_count += 1
                                continue
                    retryable = taxonomy.retry_allowed and bool(
                        condition_names & set(step.retry.retry_on)
                    )
                    if retryable and attempt < step.retry.max_attempts:
                        recovery_count += 1
                        if step.retry.backoff_ms:
                            await asyncio.sleep(step.retry.backoff_ms / 1000)
                        continue
                    expected = (
                        error.failed if isinstance(error, CheckpointError) else step.postconditions
                    )
                    return await self._hard_failure(
                        run_id,
                        artifact,
                        step,
                        expected,
                        current,
                        error_code,
                        error,
                    )

        last_observation = await self._safe_observe(last_observation)
        bound_success = [
            self._bind_condition(item, parameters) for item in artifact.success_conditions
        ]
        failed_success = await self._failed_conditions(bound_success, last_observation, 10_000)
        if failed_success:
            return await self._hard_failure(
                run_id,
                artifact,
                None,
                failed_success,
                last_observation,
                AutomationCode.SUCCESS_CONDITION_FAILED,
            )
        missing = [
            item.name for item in artifact.outputs if item.required and item.name not in outputs
        ]
        if missing:
            return await self._hard_failure(
                run_id,
                artifact,
                None,
                [],
                last_observation,
                AutomationCode.MISSING_OUTPUTS,
                message=f"required outputs were not collected: {missing}",
            )
        status = ReplayStatus.RECOVERED_SUCCESS if recovery_count else ReplayStatus.SUCCESS
        await self._emit(run_id, "replay_completed", {"status": status.value})
        return self._result(
            run_id,
            artifact,
            status,
            outputs=outputs,
            observed_state=self._observed(last_observation),
            recovery_count=recovery_count,
        )

    async def _execute(self, action: Action) -> str | None:
        if isinstance(action, NavigateAction):
            await self._surface.navigate(action.url)
        elif isinstance(action, ClickAction):
            await self._surface.click(action.target)
        elif isinstance(action, TypeAction):
            await self._surface.type(action.target, action.value)
        elif isinstance(action, SelectAction):
            await self._surface.select(action.target, action.value)
        elif isinstance(action, WaitAction):
            await self._surface.wait_for(action.target, state=action.state)
        elif isinstance(action, ExtractAction):
            return await self._surface.extract(
                action.rule.target,
                action.rule.source,
                action.rule.attribute_name,
            )
        else:
            raise TypeError(f"unsupported artifact action: {type(action).__name__}")
        return None

    async def _failed_conditions(
        self,
        conditions: list[Condition],
        observation: Observation,
        timeout_ms: int,
    ) -> list[Condition]:
        failed: list[Condition] = []
        for condition in conditions:
            if not await self._condition_matches(condition, observation, timeout_ms):
                failed.append(condition)
        return failed

    async def _condition_matches(
        self,
        condition: Condition,
        observation: Observation,
        timeout_ms: int,
    ) -> bool:
        if isinstance(condition, UrlCondition):
            return re.search(condition.pattern, observation.state.location) is not None
        if isinstance(condition, ElementCondition):
            try:
                await self._surface.wait_for(
                    condition.target,
                    timeout_ms,
                    state="visible" if condition.kind == "element_visible" else "hidden",
                )
                return True
            except Exception:
                return False
        if isinstance(condition, ValueCondition):
            actual = await self._surface.extract(condition.target, "value")
            return actual == condition.expected
        if isinstance(condition, TextCondition):
            actual = (
                await self._surface.extract(condition.target)
                if condition.target
                else observation.visible_text
            )
            if condition.match == "exact":
                return actual == condition.expected
            if condition.match == "regex":
                return re.search(condition.expected, actual) is not None
            return condition.expected in actual
        return False

    async def _match_business_outcome(
        self,
        outcomes: list[BusinessOutcome],
        parameters: dict[str, JsonScalar],
        observation: Observation,
    ) -> BusinessOutcome | None:
        for stored in outcomes:
            outcome = self._bind_model(stored, parameters, BusinessOutcome)
            failed = await self._failed_conditions(outcome.when, observation, 250)
            if not failed:
                return outcome
        return None

    @staticmethod
    def _validate_parameters(
        definitions: list[InputParameter], supplied: dict[str, JsonScalar]
    ) -> dict[str, JsonScalar]:
        declared = {item.name: item for item in definitions}
        unknown = sorted(set(supplied) - set(declared))
        if unknown:
            raise ParameterValidationError(f"unknown parameters: {unknown}")
        values: dict[str, JsonScalar] = {}
        for name, definition in declared.items():
            if name in supplied:
                value = supplied[name]
            elif definition.default is not None:
                value = definition.default
            elif definition.required:
                raise ParameterValidationError(f"missing required parameter: {name}")
            else:
                value = None
            ReplayEngine._validate_parameter_value(definition, value)
            values[name] = value
        return values

    @staticmethod
    def _validate_parameter_value(definition: InputParameter, value: JsonScalar) -> None:
        valid_type = {
            ValueType.STRING: isinstance(value, str),
            ValueType.INTEGER: type(value) is int,
            ValueType.NUMBER: type(value) in {int, float},
            ValueType.BOOLEAN: type(value) is bool,
        }[definition.value_type]
        if value is not None and not valid_type:
            raise ParameterValidationError(
                f"parameter {definition.name} must be {definition.value_type.value}"
            )
        if value is None or definition.constraints is None:
            return
        constraints = definition.constraints
        if constraints.allowed_values is not None and value not in constraints.allowed_values:
            raise ParameterValidationError(f"parameter {definition.name} is not an allowed value")
        if isinstance(value, str):
            if constraints.pattern and re.fullmatch(constraints.pattern, value) is None:
                raise ParameterValidationError(
                    f"parameter {definition.name} does not match pattern"
                )
            if constraints.min_length is not None and len(value) < constraints.min_length:
                raise ParameterValidationError(f"parameter {definition.name} is too short")
            if constraints.max_length is not None and len(value) > constraints.max_length:
                raise ParameterValidationError(f"parameter {definition.name} is too long")
        if type(value) in {int, float}:
            numeric = float(value)
            if constraints.minimum is not None and numeric < constraints.minimum:
                raise ParameterValidationError(f"parameter {definition.name} is below minimum")
            if constraints.maximum is not None and numeric > constraints.maximum:
                raise ParameterValidationError(f"parameter {definition.name} exceeds maximum")

    @staticmethod
    def _bind_step(step: Step, parameters: dict[str, JsonScalar]) -> Step:
        return ReplayEngine._bind_model(step, parameters, Step)

    @staticmethod
    def _bind_condition(condition: Condition, parameters: dict[str, JsonScalar]) -> Condition:
        data = ReplayEngine._bind_value(condition.model_dump(mode="json"), parameters)
        return CONDITION_ADAPTER.validate_python(data)

    @staticmethod
    def _bind_model(
        model: ModelT,
        parameters: dict[str, JsonScalar],
        result_type: type[ModelT],
    ) -> ModelT:
        data = ReplayEngine._bind_value(model.model_dump(mode="json"), parameters)
        return result_type.model_validate(data)

    @staticmethod
    def _bind_value(value: object, parameters: dict[str, JsonScalar]) -> object:
        if isinstance(value, str):
            result = TEMPLATE_REFERENCE.sub(
                lambda match: ReplayEngine._stringify_parameter(parameters[match.group(1)]),
                value,
            )
            if TEMPLATE_REFERENCE.search(result):
                raise ParameterValidationError("an unresolved parameter template remains")
            return result
        if isinstance(value, list):
            return [ReplayEngine._bind_value(item, parameters) for item in value]
        if isinstance(value, dict):
            return {key: ReplayEngine._bind_value(item, parameters) for key, item in value.items()}
        return value

    @staticmethod
    def _stringify_parameter(value: JsonScalar) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @staticmethod
    def _transform_extraction(value: str, rule: ExtractionRule) -> str:
        for transform in rule.transforms:
            if transform == ExtractionTransform.STRIP:
                value = value.strip()
            elif transform == ExtractionTransform.COLLAPSE_WHITESPACE:
                value = " ".join(value.split())
            elif transform == ExtractionTransform.REMOVE_CURRENCY:
                value = value.replace("$", "")
            elif transform == ExtractionTransform.REMOVE_COMMAS:
                value = value.replace(",", "")
            elif transform == ExtractionTransform.LOWERCASE:
                value = value.lower()
        if rule.regex:
            match = re.search(rule.regex, value)
            if match is None:
                raise ValueError("extracted value did not match the declared pattern")
            value = match.group(rule.regex_group)
        return value

    @staticmethod
    def _coerce_output(value: str, definition: OutputParameter) -> JsonScalar:
        if definition.value_type == ValueType.STRING:
            return value
        if definition.value_type == ValueType.INTEGER:
            return int(value)
        if definition.value_type == ValueType.NUMBER:
            return float(value)
        if definition.value_type == ValueType.BOOLEAN:
            lowered = value.lower()
            if lowered not in {"true", "false"}:
                raise ValueError("boolean extraction must be true or false")
            return lowered == "true"
        raise TypeError(definition.value_type)

    @staticmethod
    def _output_definition(outputs: list[OutputParameter], name: str) -> OutputParameter:
        match = next((item for item in outputs if item.name == name), None)
        if match is None:
            raise ValueError(f"artifact does not declare extracted output {name}")
        return match

    @staticmethod
    def _policy_action(action: Action, risk: RiskLevel, action_id: str) -> PolicyAction:
        if isinstance(action, NavigateAction):
            return PolicyAction(
                action_type="navigate", action_id=action_id, url=action.url, risk=risk
            )
        if isinstance(action, TypeAction):
            return PolicyAction(
                action_type="type",
                action_id=action_id,
                target=action.target,
                value=action.value,
                risk=risk,
            )
        if isinstance(action, SelectAction):
            return PolicyAction(
                action_type="select",
                action_id=action_id,
                target=action.target,
                value=action.value,
                risk=risk,
            )
        if isinstance(action, ClickAction):
            return PolicyAction(
                action_type="click", action_id=action_id, target=action.target, risk=risk
            )
        if isinstance(action, ExtractAction):
            return PolicyAction(
                action_type="extract",
                action_id=action_id,
                target=action.rule.target,
                risk=risk,
            )
        if isinstance(action, WaitAction):
            return PolicyAction(
                action_type="wait", action_id=action_id, target=action.target, risk=risk
            )
        raise TypeError(type(action).__name__)

    @staticmethod
    def _error_code(error: Exception, observation: Observation) -> AutomationCode:
        text = observation.visible_text.lower()
        if "session expired" in text:
            return AutomationCode.SESSION_EXPIRED
        if "permission denied" in text or "access denied" in text:
            return AutomationCode.PERMISSION_DENIED
        if "temporarily unavailable" in text or "temporary load failure" in text:
            return AutomationCode.TEMPORARY_LOAD_FAILURE
        if observation.state.active_dialog:
            return AutomationCode.KNOWN_DIALOG
        if isinstance(error, TargetNotFoundError):
            return AutomationCode.TARGET_NOT_FOUND
        if isinstance(error, AmbiguousTargetError):
            return AutomationCode.AMBIGUOUS_TARGET
        if isinstance(error, UnsupportedTargetError):
            return AutomationCode.UNSUPPORTED_TARGET
        if isinstance(error, CheckpointError):
            return AutomationCode.CHECKPOINT_FAILED
        if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            return AutomationCode.TRANSIENT_TIMEOUT
        if isinstance(error, SurfaceError):
            return AutomationCode.SURFACE_ERROR
        return AutomationCode.UNRECOVERABLE_ERROR

    @staticmethod
    def _artifact_condition_names(code: AutomationCode) -> set[str]:
        """Map canonical taxonomy codes to the v1 artifact vocabulary."""

        aliases = {
            AutomationCode.TRANSIENT_TIMEOUT: {"timeout"},
            AutomationCode.KNOWN_DIALOG: {"known_dialog", "unexpected_dialog"},
            AutomationCode.TEMPORARY_LOAD_FAILURE: {
                "transient_load",
                "application_unavailable",
            },
            AutomationCode.SESSION_EXPIRED: {"session_expired"},
            AutomationCode.PERMISSION_DENIED: {"permission_denied"},
            AutomationCode.TARGET_NOT_FOUND: {"target_not_found"},
        }
        return aliases.get(code, {code.value.lower()})

    @staticmethod
    def _taxonomy_fields(entry: TaxonomyEntry) -> dict[str, object]:
        return {
            "error_category": entry.category,
            "retry_allowed": entry.retry_allowed,
            "legitimate_result": entry.legitimate_result,
            "human_recommended": entry.human_recommended,
            "evidence_requirements": list(entry.evidence),
        }

    async def _safe_observe(self, fallback: Observation | None) -> Observation:
        try:
            return await self._surface.observe()
        except Exception:
            if fallback is not None:
                return fallback
            state = await self._surface.get_current_state()
            return Observation(
                state=state,
                semantic_tree="",
                visible_text="",
                state_fingerprint="unavailable",
            )

    async def _business_result(
        self,
        run_id: str,
        artifact: CapabilityArtifact,
        outcome: BusinessOutcome,
        parameters: dict[str, JsonScalar],
        observation: Observation,
        recovery_count: int,
    ) -> ReplayResult:
        outputs = {
            key: cast(JsonScalar, self._bind_value(value, parameters))
            for key, value in outcome.output_values.items()
        }
        await self._emit(run_id, "business_outcome", {"code": outcome.code})
        code = AutomationCode(outcome.code)
        taxonomy = classify(code)
        return self._result(
            run_id,
            artifact,
            ReplayStatus.BUSINESS_OUTCOME,
            outputs=outputs,
            business_outcome_code=code,
            error_code=code,
            observed_state=self._observed(observation),
            recovery_count=recovery_count,
            **self._taxonomy_fields(taxonomy),
        )

    async def _hard_failure(
        self,
        run_id: str,
        artifact: CapabilityArtifact,
        step: Step | None,
        expected: list[Condition],
        observation: Observation | None,
        error_code: AutomationCode,
        error: Exception | None = None,
        *,
        message: str | None = None,
    ) -> ReplayResult:
        taxonomy = classify(error_code)
        evidence_path = None
        if EvidenceKind.SCREENSHOT in taxonomy.evidence:
            evidence_path = await self._capture_failure(
                run_id,
                step.step_id if step else "final",
                observation,
                {
                    "code": error_code.value,
                    "message": message or (str(error) if error else None),
                    "expected_state": [item.model_dump(mode="json") for item in expected],
                },
            )
        await self._emit(
            run_id,
            "replay_failed",
            {
                "error_code": error_code.value,
                "category": taxonomy.category.value,
                "step_id": step.step_id if step else None,
            },
            step.step_id if step else None,
            action=step.action.kind if step else None,
            locator_strategy=self._last_locator_strategy(),
            status="failed",
            error_code=error_code.value,
        )
        return self._result(
            run_id,
            artifact,
            ReplayStatus.HARD_FAILURE,
            failed_step=step.step_id if step else None,
            expected_state=[item.model_dump_json() for item in expected],
            observed_state=self._observed(observation),
            error_code=error_code,
            message=message or (str(error) if error else error_code.replace("_", " ").title()),
            evidence_path=evidence_path,
            **self._taxonomy_fields(taxonomy),
        )

    async def _human_required(
        self,
        run_id: str,
        artifact: CapabilityArtifact,
        step: Step,
        observation: Observation,
        error_code: AutomationCode,
        message: str,
    ) -> ReplayResult:
        taxonomy = classify(error_code)
        evidence_path = await self._capture_failure(
            run_id,
            step.step_id,
            observation,
            {"code": error_code.value, "message": message},
        )
        await self._emit(run_id, "human_required", {"step_id": step.step_id, "code": error_code})
        return self._result(
            run_id,
            artifact,
            ReplayStatus.HUMAN_REQUIRED,
            failed_step=step.step_id,
            expected_state=[item.model_dump_json() for item in step.postconditions],
            observed_state=self._observed(observation),
            error_code=error_code,
            message=message,
            evidence_path=evidence_path,
            **self._taxonomy_fields(taxonomy),
        )

    async def _capture_failure(
        self,
        run_id: str,
        name: str,
        observation: Observation | None,
        error: dict[str, object],
    ) -> str | None:
        try:
            bundle = await self._evidence.save_failure(
                run_id,
                name,
                await self._surface.screenshot(),
                observation,
                error,
            )
            return bundle.directory
        except Exception:
            return None

    @staticmethod
    def _observed(observation: Observation | None) -> ObservedState | None:
        if observation is None:
            return None
        return ObservedState(
            location=observation.state.location,
            title=observation.state.title,
            state_fingerprint=observation.state_fingerprint,
            visible_text_summary=" ".join(observation.visible_text.split())[:500],
        )

    @staticmethod
    def _result(
        run_id: str,
        artifact: CapabilityArtifact,
        status: ReplayStatus,
        **values: object,
    ) -> ReplayResult:
        return ReplayResult.model_validate(
            {
                "run_id": run_id,
                "status": status,
                "capability_id": artifact.capability_id,
                "capability_version": artifact.capability_version,
                **values,
            }
        )

    async def _emit(
        self,
        run_id: str,
        event_type: str,
        data: dict[str, object],
        step_id: str | None = None,
        *,
        action: str | None = None,
        locator_strategy: str | None = None,
        status: str = "recorded",
        duration_ms: float | None = None,
        error_code: str | None = None,
    ) -> None:
        if self._events is None:
            return
        await self._events.append(
            RunEvent(
                event_type=event_type,
                run_id=run_id,
                mode="replay",
                capability_id=self._capability_id,
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
