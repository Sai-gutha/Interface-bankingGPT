"""Deterministic compilation from a neutral discovery recording."""

from datetime import UTC, datetime

import pytest

from computer_use.artifacts.compiler import (
    ArtifactCompiler,
    CompilationError,
    CompilerSpec,
    RuntimeBinding,
)
from computer_use.artifacts.models import (
    AttributeLocator,
    BusinessOutcome,
    CoordinateLocator,
    CssLocator,
    InputParameter,
    LabelLocator,
    OutputParameter,
    ReuseMetadata,
    RiskLevel,
    RoleLocator,
    Target,
    TargetApplication,
    TextCondition,
    TypeAction,
    ValueType,
)
from computer_use.discovery.engine import (
    AgentDecision,
    ClickDecision,
    DiscoveryResult,
    ExtractDecision,
    RecordedAction,
    TypeDecision,
)
from computer_use.surfaces.base import Observation, SurfaceState

FIXED_TIME = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)


def observation(fingerprint: str, location: str, title: str) -> Observation:
    return Observation(
        state=SurfaceState(surface_kind="web", location=location, title=title),
        semantic_tree=title,
        visible_text=title,
        state_fingerprint=fingerprint,
    )


def target(description: str) -> Target:
    return Target(
        description=description,
        candidates=[RoleLocator(role="button", name=description)],
    )


def recorded(
    sequence: int,
    action: ClickDecision | TypeDecision | ExtractDecision,
    before: Observation,
    after: Observation,
    *,
    extracted: str | None = None,
) -> RecordedAction:
    return RecordedAction(
        sequence=sequence,
        decision=AgentDecision(
            action=action,
            operational_reason="Concise runtime note that is not persisted",
            confidence=0.9,
        ),
        before=before,
        after=after,
        extracted_value=extracted,
    )


def successful_discovery() -> DiscoveryResult:
    login = observation("a", "http://demo.test/", "Login")
    search = observation("b", "http://demo.test/members", "Member Search")
    messages = observation("x", "http://demo.test/messages", "Messages")
    entered = observation("c", "http://demo.test/members", "Member Search")
    details = observation("d", "http://demo.test/members/12345", "Member Details")
    member_field = Target(
        description="Member ID field",
        candidates=[
            CssLocator(selector="input[name='member_number']"),
            LabelLocator(label="Member ID"),
            LabelLocator(label="Member ID"),
            AttributeLocator(name="name", value="member_number"),
            CoordinateLocator(
                x=510,
                y=330,
                viewport_width=1440,
                viewport_height=900,
            ),
        ],
    )
    balance = Target(
        description="Savings balance",
        candidates=[CssLocator(selector="table.data-grid tr:nth-child(2) td:nth-child(4)")],
    )
    return DiscoveryResult(
        run_id="discovery_test",
        status="success",
        stop_reason="goal_complete",
        outputs={"savings_balance": "$9,125.40"},
        recording=[
            recorded(1, ClickDecision(target=target("Begin Session")), login, search),
            recorded(2, ClickDecision(target=target("System Messages")), search, messages),
            recorded(3, ClickDecision(target=target("Member Search")), messages, search),
            recorded(4, TypeDecision(target=member_field, value="12345"), search, entered),
            recorded(5, ClickDecision(target=target("Find Member")), entered, details),
            recorded(
                6,
                ExtractDecision(target=balance, output_name="savings_balance"),
                details,
                details,
                extracted="$9,125.40",
            ),
        ],
        final_observation=details,
    )


def compiler_spec() -> CompilerSpec:
    return CompilerSpec(
        capability_id="lookup_savings_balance",
        name="Look up savings balance",
        capability_version="1.0.0",
        description="Return a synthetic member's savings balance.",
        target_application=TargetApplication(
            vendor_id="demo_vendor",
            product_id="banking_operations",
            display_name="Demo Banking",
            surface_kind="legacy_web",
            entry_point="http://demo.test/",
            allowed_origins=["http://demo.test/"],
        ),
        bindings=[
            RuntimeBinding(
                parameter=InputParameter(
                    name="member_id",
                    value_type=ValueType.STRING,
                    description="Synthetic member identifier",
                    sensitive=True,
                ),
                runtime_value="12345",
            )
        ],
        outputs=[
            OutputParameter(
                name="savings_balance",
                value_type=ValueType.NUMBER,
                description="Current savings balance",
                sensitive=True,
            )
        ],
        business_outcomes=[
            BusinessOutcome(
                code="MEMBER_NOT_FOUND",
                description="No member matched the identifier.",
                when=[TextCondition(expected="Record not found")],
                output_values={"savings_balance": None},
            )
        ],
        reuse=ReuseMetadata(vendor_capability_key="demo.lookup_savings_balance"),
        checkpoint_overrides={
            6: [TextCondition(expected="Savings")],
        },
        risk_overrides={5: RiskLevel.READ},
    )


def test_compiler_removes_cycles_parameterizes_and_normalizes() -> None:
    compiler = ArtifactCompiler(clock=lambda: FIXED_TIME)

    artifact = compiler.compile(successful_discovery(), compiler_spec())
    serialized = artifact.model_dump_json()

    assert [step.step_id for step in artifact.steps] == [
        "step_01_click",
        "step_04_type",
        "step_05_click",
        "step_06_extract",
    ]
    typed = artifact.steps[1].action
    assert isinstance(typed, TypeAction)
    assert typed.value == "{{member_id}}"
    assert [item.strategy for item in typed.target.candidates] == [
        "label",
        "attribute",
        "css",
        "coordinates",
    ]
    assert "12345" not in serialized
    assert "Concise runtime note" not in serialized
    assert artifact.provenance.created_at == FIXED_TIME
    assert artifact.provenance.discovery_run_id == "discovery_test"
    assert artifact.outputs[0].name == "savings_balance"
    assert artifact.business_outcomes[0].code == "MEMBER_NOT_FOUND"


def test_same_inputs_compile_to_identical_artifacts() -> None:
    compiler = ArtifactCompiler(clock=lambda: FIXED_TIME)
    first = compiler.compile(successful_discovery(), compiler_spec())
    second = compiler.compile(successful_discovery(), compiler_spec())
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_failed_discovery_cannot_be_compiled() -> None:
    discovery = successful_discovery().model_copy(
        update={"status": "stopped", "stop_reason": "max_steps"}
    )
    with pytest.raises(CompilationError, match="goal-complete"):
        ArtifactCompiler().compile(discovery, compiler_spec())
