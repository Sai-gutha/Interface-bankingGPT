"""Strict, versioned schema for reusable deterministic automation capabilities."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    field_validator,
    model_validator,
)

IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
SEMVER_PATTERN = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$"
TEMPLATE_REFERENCE = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}")


class StrictModel(BaseModel):
    """Persisted contracts reject unknown fields and accidental schema drift."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class RiskLevel(StrEnum):
    READ_ONLY = "read"
    READ = "read"  # v1 compatibility alias
    REVERSIBLE_WRITE = "reversible_write"
    IRREVERSIBLE = "irreversible_write"
    IRREVERSIBLE_WRITE = "irreversible_write"  # v1 compatibility alias


class ParameterConstraints(StrictModel):
    """Portable validation rules applied before a UI session starts."""

    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    allowed_values: list[str | int | float | bool] | None = None


class InputParameter(StrictModel):
    """A caller-supplied, typed capability argument."""

    name: str = Field(pattern=IDENTIFIER_PATTERN)
    value_type: ValueType
    description: str
    required: bool = True
    default: str | int | float | bool | None = None
    sensitive: bool = False
    constraints: ParameterConstraints | None = None

    @model_validator(mode="after")
    def validate_default(self) -> "InputParameter":
        if self.required and self.default is not None:
            raise ValueError("required inputs cannot define a default")
        return self


class OutputParameter(StrictModel):
    """A typed value returned only after a terminal condition is verified."""

    name: str = Field(pattern=IDENTIFIER_PATTERN)
    value_type: ValueType
    description: str
    required: bool = True
    sensitive: bool = False


class RoleLocator(StrictModel):
    strategy: Literal["role"] = "role"
    role: str
    name: str | None = None
    exact: bool = True


class LabelLocator(StrictModel):
    strategy: Literal["label"] = "label"
    label: str
    exact: bool = True


class TextLocator(StrictModel):
    strategy: Literal["text"] = "text"
    text: str
    exact: bool = True


class AttributeLocator(StrictModel):
    strategy: Literal["attribute"] = "attribute"
    name: str
    value: str


class RelativeTextLocator(StrictModel):
    """Find a control by stable neighboring text rather than DOM position."""

    strategy: Literal["relative_text"] = "relative_text"
    anchor_text: str
    relation: Literal["within", "following", "preceding", "row_containing"]
    target_role: str | None = None
    target_text: str | None = None
    target_css: str | None = None

    @model_validator(mode="after")
    def require_one_relative_target(self) -> "RelativeTextLocator":
        selectors = [self.target_role, self.target_text, self.target_css]
        if sum(item is not None for item in selectors) > 1:
            raise ValueError("relative_text accepts at most one target selector")
        return self


class CssLocator(StrictModel):
    strategy: Literal["css"] = "css"
    selector: str


class XPathLocator(StrictModel):
    strategy: Literal["xpath"] = "xpath"
    expression: str


class CoordinateLocator(StrictModel):
    """Last-resort point tied to a recorded viewport and coordinate space."""

    strategy: Literal["coordinates"] = "coordinates"
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    viewport_width: PositiveInt
    viewport_height: PositiveInt
    coordinate_space: Literal["viewport", "screen"] = "viewport"


class AccessibilityIdLocator(StrictModel):
    """Platform accessibility identifier for future desktop adapters."""

    strategy: Literal["accessibility_id"] = "accessibility_id"
    identifier: str


LocatorCandidate = Annotated[
    RoleLocator
    | LabelLocator
    | TextLocator
    | AttributeLocator
    | RelativeTextLocator
    | CssLocator
    | XPathLocator
    | CoordinateLocator
    | AccessibilityIdLocator,
    Field(discriminator="strategy"),
]


class Target(StrictModel):
    """Semantic control identity with deterministic, ordered locator fallbacks."""

    description: str
    candidates: list[LocatorCandidate] = Field(min_length=1)
    expected_role: str | None = None
    require_unique: bool = True

    @field_validator("candidates")
    @classmethod
    def coordinates_must_be_last(cls, value: list[LocatorCandidate]) -> list[LocatorCandidate]:
        coordinate_positions = [
            index
            for index, candidate in enumerate(value)
            if isinstance(candidate, CoordinateLocator)
        ]
        if coordinate_positions and coordinate_positions != [len(value) - 1]:
            raise ValueError("a coordinate locator may appear once and only as the final fallback")
        return value


class ExtractionTransform(StrEnum):
    STRIP = "strip"
    COLLAPSE_WHITESPACE = "collapse_whitespace"
    REMOVE_CURRENCY = "remove_currency"
    REMOVE_COMMAS = "remove_commas"
    LOWERCASE = "lowercase"


class ElementCondition(StrictModel):
    kind: Literal["element_visible", "element_hidden"]
    target: Target


class TextCondition(StrictModel):
    kind: Literal["text_present"] = "text_present"
    target: Target | None = None
    expected: str
    match: Literal["exact", "contains", "regex"] = "contains"


class UrlCondition(StrictModel):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str


class ValueCondition(StrictModel):
    kind: Literal["value_equals"] = "value_equals"
    target: Target
    expected: str


Condition = Annotated[
    ElementCondition | TextCondition | UrlCondition | ValueCondition,
    Field(discriminator="kind"),
]


class ExtractionRule(StrictModel):
    """Declare how a surface value becomes a typed capability output."""

    output_name: str = Field(pattern=IDENTIFIER_PATTERN)
    target: Target
    source: Literal["inner_text", "value", "attribute"] = "inner_text"
    attribute_name: str | None = None
    regex: str | None = None
    regex_group: int | str = 0
    transforms: list[ExtractionTransform] = Field(
        default_factory=lambda: [ExtractionTransform.STRIP]
    )

    @model_validator(mode="after")
    def require_attribute_name(self) -> "ExtractionRule":
        if (self.source == "attribute") != (self.attribute_name is not None):
            raise ValueError("attribute_name is required only when source is 'attribute'")
        return self


class NavigateAction(StrictModel):
    kind: Literal["navigate"] = "navigate"
    url: str


class ClickAction(StrictModel):
    kind: Literal["click"] = "click"
    target: Target


class TypeAction(StrictModel):
    kind: Literal["type"] = "type"
    target: Target
    value: str
    clear_existing: bool = True


class SelectAction(StrictModel):
    kind: Literal["select"] = "select"
    target: Target
    value: str


class ExtractAction(StrictModel):
    kind: Literal["extract"] = "extract"
    rule: ExtractionRule


class WaitAction(StrictModel):
    kind: Literal["wait"] = "wait"
    target: Target
    state: Literal["visible", "hidden"] = "visible"


Action = Annotated[
    NavigateAction | ClickAction | TypeAction | SelectAction | ExtractAction | WaitAction,
    Field(discriminator="kind"),
]

# Compatibility aliases for code written against the initial scaffold.
InputDefinition = InputParameter
OutputDefinition = OutputParameter
FillAction = TypeAction
ReadAction = ExtractAction


class RetryPolicy(StrictModel):
    """Bounded retry rules; replay never invents recovery behavior."""

    max_attempts: PositiveInt = 1
    backoff_ms: int = Field(default=0, ge=0, le=60_000)
    retry_on: list[
        Literal["target_not_found", "timeout", "transient_load", "application_unavailable"]
    ] = Field(default_factory=list)


class RecoveryDirective(StrictModel):
    """A predeclared response to one recognized runtime condition."""

    on: Literal[
        "validation_error",
        "permission_denied",
        "known_dialog",
        "unexpected_dialog",
        "session_expired",
        "transient_load",
    ]
    strategy: Literal["retry_step", "dismiss_dialog", "request_handoff", "return_outcome", "stop"]
    max_uses: int = Field(default=1, ge=0, le=3)


class RecoveryPolicy(StrictModel):
    directives: list[RecoveryDirective] = Field(default_factory=list)


class Step(StrictModel):
    """One ordered action with explicit completion and recovery semantics."""

    step_id: str = Field(pattern=IDENTIFIER_PATTERN)
    description: str
    action: Action
    preconditions: list[Condition] = Field(default_factory=list)
    postconditions: list[Condition] = Field(min_length=1)
    timeout_ms: PositiveInt = 10_000
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)
    risk: RiskLevel = RiskLevel.READ


class BusinessOutcome(StrictModel):
    """Expected terminal state returned to the caller rather than raised as a failure."""

    code: Literal["MEMBER_NOT_FOUND", "INVALID_INPUT", "ACCOUNT_NOT_FOUND"]
    description: str
    when: list[Condition] = Field(min_length=1)
    output_values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ApplicationFingerprint(StrictModel):
    """Signals checked before replay to detect an incompatible app variant."""

    name: str
    condition: Condition
    required: bool = True


class TargetApplication(StrictModel):
    vendor_id: str = Field(pattern=IDENTIFIER_PATTERN)
    product_id: str = Field(pattern=IDENTIFIER_PATTERN)
    display_name: str
    surface_kind: Literal["web", "legacy_web", "desktop"]
    entry_point: str
    allowed_origins: list[AnyHttpUrl] = Field(min_length=1)
    supported_versions: list[str] = Field(default_factory=list)
    fingerprints: list[ApplicationFingerprint] = Field(default_factory=list)


class ReuseMetadata(StrictModel):
    """Constrained vendor/tenant specialization metadata, not tenant secrets."""

    vendor_capability_key: str
    tenant_scope: Literal["vendor_default", "tenant_override"] = "vendor_default"
    tenant_id: str | None = None
    base_capability_id: str | None = None
    allowed_override_paths: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_tenant_scope(self) -> "ReuseMetadata":
        if self.tenant_scope == "tenant_override" and not self.tenant_id:
            raise ValueError("tenant_override requires tenant_id")
        if self.tenant_scope == "vendor_default" and self.tenant_id:
            raise ValueError("vendor_default cannot contain tenant_id")
        return self


class Provenance(StrictModel):
    """Audit linkage without raw model prompts, reasoning, transcripts, or observations."""

    created_at: datetime
    created_by: Literal["discovery_compiler", "human_author"]
    discovery_run_id: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None


class CapabilityArtifact(StrictModel):
    """Agent-invocable, human-reviewable, deterministic automation contract."""

    schema_version: Literal["1.0"] = "1.0"
    capability_id: str = Field(pattern=IDENTIFIER_PATTERN)
    name: str
    capability_version: str = Field(pattern=SEMVER_PATTERN)
    description: str
    target_application: TargetApplication
    inputs: list[InputParameter] = Field(default_factory=list)
    outputs: list[OutputParameter] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    success_conditions: list[Condition] = Field(min_length=1)
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    risk: RiskLevel
    reuse: ReuseMetadata
    provenance: Provenance

    @model_validator(mode="after")
    def validate_contract_references(self) -> "CapabilityArtifact":
        input_names = [item.name for item in self.inputs]
        output_names = [item.name for item in self.outputs]
        step_ids = [item.step_id for item in self.steps]
        self._require_unique("input names", input_names)
        self._require_unique("output names", output_names)
        self._require_unique("step IDs", step_ids)

        declared_inputs = set(input_names)
        referenced_inputs = set(_template_references(self.model_dump(mode="json")))
        unknown_inputs = referenced_inputs - declared_inputs
        if unknown_inputs:
            raise ValueError(f"template references undeclared inputs: {sorted(unknown_inputs)}")

        extracted_outputs = {
            step.action.rule.output_name
            for step in self.steps
            if isinstance(step.action, ExtractAction)
        }
        unknown_outputs = extracted_outputs - set(output_names)
        if unknown_outputs:
            raise ValueError(
                f"extraction rules target undeclared outputs: {sorted(unknown_outputs)}"
            )
        return self

    @staticmethod
    def _require_unique(label: str, values: list[str]) -> None:
        duplicates = sorted({item for item in values if values.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate {label}: {duplicates}")


def _template_references(value: object) -> list[str]:
    """Recursively collect safe ``{{input_name}}`` references without evaluating templates."""

    if isinstance(value, str):
        return TEMPLATE_REFERENCE.findall(value)
    if isinstance(value, dict):
        return [reference for item in value.values() for reference in _template_references(item)]
    if isinstance(value, list):
        return [reference for item in value for reference in _template_references(item)]
    return []
