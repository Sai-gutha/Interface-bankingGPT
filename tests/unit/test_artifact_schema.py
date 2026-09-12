"""Contract tests for the most important persisted model."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from computer_use.artifacts.models import CapabilityArtifact, CoordinateLocator, RoleLocator, Target


def test_capability_schema_is_versioned_and_strict() -> None:
    schema = CapabilityArtifact.model_json_schema()

    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert schema["additionalProperties"] is False


def test_complete_example_validates_and_round_trips() -> None:
    path = Path("artifacts/examples/lookup_savings_balance.v1.json")
    source = json.loads(path.read_text())

    artifact = CapabilityArtifact.model_validate(source)

    assert artifact.capability_id == "lookup_savings_balance"
    assert artifact.inputs[0].sensitive is True
    assert artifact.outputs[0].name == "savings_balance"
    serialized = artifact.model_dump(mode="json")
    assert CapabilityArtifact.model_validate(serialized) == artifact


def test_undeclared_template_reference_is_rejected() -> None:
    path = Path("artifacts/examples/lookup_savings_balance.v1.json")
    source = json.loads(path.read_text())
    source["steps"][2]["action"]["value"] = "{{unknown_input}}"

    with pytest.raises(ValidationError, match="undeclared inputs"):
        CapabilityArtifact.model_validate(source)


def test_coordinate_locator_must_be_last() -> None:
    with pytest.raises(ValidationError, match="final fallback"):
        Target(
            description="Invalid ordering",
            candidates=[
                CoordinateLocator(
                    x=10,
                    y=10,
                    viewport_width=1440,
                    viewport_height=900,
                ),
                RoleLocator(role="button", name="Continue"),
            ],
        )
