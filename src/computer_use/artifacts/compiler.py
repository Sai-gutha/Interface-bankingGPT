"""Deterministic compiler from successful discovery recordings to capability artifacts."""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from computer_use.artifacts.models import (
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
    LocatorCandidate,
    NavigateAction,
    OutputParameter,
    Provenance,
    RecoveryPolicy,
    RetryPolicy,
    ReuseMetadata,
    RiskLevel,
    SelectAction,
    Step,
    Target,
    TargetApplication,
    TextCondition,
    TypeAction,
    UrlCondition,
    ValueCondition,
    WaitAction,
)
from computer_use.discovery.engine import (
    ClickDecision,
    DiscoveryResult,
    ExtractDecision,
    NavigateDecision,
    RecordedAction,
    TypeDecision,
    WaitDecision,
)
from computer_use.safety.redactor import Redactor

LOCATOR_RANK = {
    "role": 0,
    "label": 1,
    "text": 2,
    "attribute": 3,
    "relative_text": 4,
    "accessibility_id": 5,
    "css": 6,
    "xpath": 7,
    "coordinates": 8,
}

ModelT = TypeVar("ModelT", bound=BaseModel)
CONDITION_ADAPTER: TypeAdapter[Condition] = TypeAdapter(Condition)


class RuntimeBinding(BaseModel):
    """Ephemeral discovered value paired with its persisted parameter definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    parameter: InputParameter
    runtime_value: SecretStr


class CompilerSpec(BaseModel):
    """Human-reviewable compile choices that cannot be inferred safely from a transcript."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    capability_id: str
    name: str
    capability_version: str
    description: str
    target_application: TargetApplication
    bindings: list[RuntimeBinding] = Field(default_factory=list)
    outputs: list[OutputParameter] = Field(default_factory=list)
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    reuse: ReuseMetadata
    checkpoint_overrides: dict[int, list[Condition]] = Field(default_factory=dict)
    risk_overrides: dict[int, RiskLevel] = Field(default_factory=dict)
    retry_overrides: dict[int, RetryPolicy] = Field(default_factory=dict)
    recovery_overrides: dict[int, RecoveryPolicy] = Field(default_factory=dict)


class CompilationError(ValueError):
    """Discovery data could not be converted into a safe reusable artifact."""


class ArtifactCompiler:
    """Compile neutral observable records without reading prompts or model transcripts."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def compile(self, discovery: DiscoveryResult, spec: CompilerSpec) -> CapabilityArtifact:
        if discovery.status != "success" or discovery.stop_reason != "goal_complete":
            raise CompilationError("only a goal-complete discovery can be compiled")
        if not discovery.recording:
            raise CompilationError("successful discovery contains no recorded actions")

        records = self._remove_exploration(discovery.recording)
        substitutions = self._substitutions(spec.bindings)
        steps = [self._compile_step(record, spec, substitutions) for record in records]
        if not steps:
            raise CompilationError("no reusable actions remained after exploration removal")

        output_names = {output.name for output in spec.outputs}
        discovered_outputs = {
            record.decision.action.output_name
            for record in records
            if isinstance(record.decision.action, ExtractDecision)
        }
        undeclared = discovered_outputs - output_names
        if undeclared:
            raise CompilationError(f"discovery extracted undeclared outputs: {sorted(undeclared)}")

        success_conditions = self._success_conditions(records, spec, substitutions)
        artifact = CapabilityArtifact(
            schema_version="1.0",
            capability_id=spec.capability_id,
            name=spec.name,
            capability_version=spec.capability_version,
            description=spec.description,
            target_application=self._parameterize_model(
                spec.target_application, substitutions, TargetApplication
            ),
            inputs=[binding.parameter for binding in spec.bindings],
            outputs=spec.outputs,
            steps=steps,
            success_conditions=success_conditions,
            business_outcomes=[
                self._parameterize_model(outcome, substitutions, BusinessOutcome)
                for outcome in spec.business_outcomes
            ],
            risk=max((step.risk for step in steps), key=self._risk_rank),
            reuse=spec.reuse,
            provenance=Provenance(
                created_at=self._clock(),
                created_by="discovery_compiler",
                discovery_run_id=discovery.run_id,
            ),
        )
        self._assert_runtime_values_absent(artifact, substitutions)
        Redactor(binding.runtime_value for binding in spec.bindings).assert_safe(
            artifact.model_dump(mode="json")
        )
        return artifact

    @staticmethod
    def _remove_exploration(records: list[RecordedAction]) -> list[RecordedAction]:
        """Erase exact state cycles and non-observing no-op actions deterministically."""

        kept: list[RecordedAction] = []
        states = [records[0].before.state_fingerprint]
        for record in records:
            if record.before.state_fingerprint != states[-1]:
                raise CompilationError("discovery recording is not a contiguous state path")
            if record.before.state_fingerprint == record.after.state_fingerprint:
                if isinstance(record.decision.action, (WaitDecision, ExtractDecision)):
                    kept.append(record)
                    states.append(record.after.state_fingerprint)
                continue
            if record.after.state_fingerprint in states:
                state_index = states.index(record.after.state_fingerprint)
                kept = kept[:state_index]
                states = states[: state_index + 1]
                continue
            kept.append(record)
            states.append(record.after.state_fingerprint)
        return kept

    def _compile_step(
        self,
        record: RecordedAction,
        spec: CompilerSpec,
        substitutions: list[tuple[str, str]],
    ) -> Step:
        action = self._compile_action(record, substitutions, spec.outputs)
        postconditions = spec.checkpoint_overrides.get(record.sequence) or self._checkpoint(
            record, action, substitutions
        )
        return Step(
            step_id=f"step_{record.sequence:02d}_{action.kind}",
            description=self._step_description(action),
            action=action,
            postconditions=[
                self._parameterize_condition(condition, substitutions)
                for condition in postconditions
            ],
            timeout_ms=10_000,
            retry=spec.retry_overrides.get(record.sequence, RetryPolicy()),
            recovery=spec.recovery_overrides.get(record.sequence, RecoveryPolicy()),
            risk=spec.risk_overrides.get(record.sequence, RiskLevel.READ),
        )

    def _compile_action(
        self,
        record: RecordedAction,
        substitutions: list[tuple[str, str]],
        outputs: list[OutputParameter],
    ) -> Action:
        discovered = record.decision.action
        if isinstance(discovered, NavigateDecision):
            return NavigateAction(url=self._replace(discovered.url, substitutions))
        if isinstance(discovered, ClickDecision):
            return ClickAction(target=self._normalize_target(discovered.target, substitutions))
        if isinstance(discovered, TypeDecision):
            return TypeAction(
                target=self._normalize_target(discovered.target, substitutions),
                value=self._replace(discovered.value, substitutions),
            )
        if isinstance(discovered, WaitDecision):
            return WaitAction(target=self._normalize_target(discovered.target, substitutions))
        if isinstance(discovered, ExtractDecision):
            output = next((item for item in outputs if item.name == discovered.output_name), None)
            transforms = [ExtractionTransform.STRIP]
            if output and output.value_type.value == "number":
                transforms += [
                    ExtractionTransform.REMOVE_CURRENCY,
                    ExtractionTransform.REMOVE_COMMAS,
                ]
            return ExtractAction(
                rule=ExtractionRule(
                    output_name=discovered.output_name,
                    target=self._normalize_target(discovered.target, substitutions),
                    transforms=transforms,
                )
            )
        raise CompilationError(f"unsupported recorded action: {type(discovered).__name__}")

    def _normalize_target(self, target: Target, substitutions: list[tuple[str, str]]) -> Target:
        parameterized = self._parameterize_model(target, substitutions, Target)
        unique: dict[str, LocatorCandidate] = {}
        for candidate in parameterized.candidates:
            key = json.dumps(candidate.model_dump(mode="json"), sort_keys=True)
            unique.setdefault(key, candidate)
        candidates = sorted(
            unique.values(),
            key=lambda candidate: LOCATOR_RANK[candidate.strategy],
        )
        return parameterized.model_copy(update={"candidates": candidates})

    def _checkpoint(
        self,
        record: RecordedAction,
        action: Action,
        substitutions: list[tuple[str, str]],
    ) -> list[Condition]:
        before = record.before.state
        after = record.after.state
        if before.location != after.location:
            pattern = self._replace(re.escape(after.location), substitutions)
            return [UrlCondition(pattern=f"^{pattern}$")]
        if isinstance(action, TypeAction):
            return [ValueCondition(target=action.target, expected=action.value)]
        if isinstance(action, (ExtractAction, WaitAction)):
            target = action.rule.target if isinstance(action, ExtractAction) else action.target
            return [ElementCondition(kind="element_visible", target=target)]
        if after.title and after.title != before.title:
            return [TextCondition(expected=after.title, match="contains")]
        action_target = action.target if isinstance(action, (ClickAction, SelectAction)) else None
        if action_target is not None:
            return [ElementCondition(kind="element_visible", target=action_target)]
        raise CompilationError(f"step {record.sequence} has no observable checkpoint")

    def _success_conditions(
        self,
        records: list[RecordedAction],
        spec: CompilerSpec,
        substitutions: list[tuple[str, str]],
    ) -> list[Condition]:
        final = records[-1]
        explicit = spec.checkpoint_overrides.get(final.sequence)
        if explicit:
            return [
                self._parameterize_condition(condition, substitutions) for condition in explicit
            ]
        pattern = self._replace(re.escape(final.after.state.location), substitutions)
        return [UrlCondition(pattern=f"^{pattern}$")]

    @staticmethod
    def _step_description(action: Action) -> str:
        if isinstance(action, NavigateAction):
            return "Navigate to the application entry point"
        if isinstance(action, TypeAction):
            return f"Enter a value in {action.target.description}"
        if isinstance(action, ClickAction):
            return f"Click {action.target.description}"
        if isinstance(action, WaitAction):
            return f"Wait for {action.target.description}"
        if isinstance(action, ExtractAction):
            return f"Extract {action.rule.output_name}"
        if isinstance(action, SelectAction):
            return f"Select a value in {action.target.description}"
        raise TypeError(type(action).__name__)

    @staticmethod
    def _substitutions(bindings: list[RuntimeBinding]) -> list[tuple[str, str]]:
        values = [binding.runtime_value.get_secret_value() for binding in bindings]
        if any(not value for value in values):
            raise CompilationError("runtime binding values cannot be empty")
        if len(values) != len(set(values)):
            raise CompilationError("runtime binding values must be unique")
        pairs = [
            (binding.runtime_value.get_secret_value(), f"{{{{{binding.parameter.name}}}}}")
            for binding in bindings
        ]
        return sorted(pairs, key=lambda pair: (-len(pair[0]), pair[0]))

    @staticmethod
    def _replace(value: str, substitutions: list[tuple[str, str]]) -> str:
        for runtime_value, placeholder in substitutions:
            value = value.replace(runtime_value, placeholder)
        return value

    def _parameterize_model(
        self,
        model: ModelT,
        substitutions: list[tuple[str, str]],
        result_type: type[ModelT],
    ) -> ModelT:
        data = self._parameterize_value(model.model_dump(mode="json"), substitutions)
        return result_type.model_validate(data)

    def _parameterize_condition(
        self,
        condition: Condition,
        substitutions: list[tuple[str, str]],
    ) -> Condition:
        data = self._parameterize_value(condition.model_dump(mode="json"), substitutions)
        return CONDITION_ADAPTER.validate_python(data)

    def _parameterize_value(
        self,
        value: object,
        substitutions: list[tuple[str, str]],
    ) -> object:
        if isinstance(value, str):
            return self._replace(value, substitutions)
        if isinstance(value, list):
            return [self._parameterize_value(item, substitutions) for item in value]
        if isinstance(value, dict):
            return {
                key: self._parameterize_value(item, substitutions) for key, item in value.items()
            }
        return value

    @staticmethod
    def _assert_runtime_values_absent(
        artifact: CapabilityArtifact, substitutions: list[tuple[str, str]]
    ) -> None:
        serialized = artifact.model_dump_json()
        leaked = [value for value, _ in substitutions if value in serialized]
        if leaked:
            raise CompilationError("one or more bound runtime values leaked into the artifact")

    @staticmethod
    def _risk_rank(risk: RiskLevel) -> int:
        return {
            RiskLevel.READ: 0,
            RiskLevel.REVERSIBLE_WRITE: 1,
            RiskLevel.IRREVERSIBLE_WRITE: 2,
        }[risk]
