"""Structured event timelines and deterministic failure evidence bundles."""

import json
from pathlib import Path

import pytest

from computer_use.observability.events import JsonlEventSink, RunEvent
from computer_use.observability.evidence import FileEvidenceStore
from computer_use.safety.redactor import REDACTED, Redactor
from computer_use.surfaces.base import Observation, Screenshot, SurfaceState


@pytest.mark.asyncio
async def test_jsonl_event_contains_all_review_dimensions_and_is_redacted(
    tmp_path: Path,
) -> None:
    sink = JsonlEventSink(tmp_path, Redactor(["secret-value-123"]))
    event = RunEvent(
        event_type="step_completed",
        run_id="replay_fixed-run",
        mode="replay",
        capability_id="lookup_savings_balance",
        step_id="extract_balance",
        action="extract",
        locator_strategy="role",
        status="succeeded",
        duration_ms=12.5,
        data={"value": "secret-value-123"},
    )

    await sink.append(event)

    path = tmp_path / "replay_fixed-run" / "events.jsonl"
    payload = json.loads(path.read_text().strip())
    assert payload["run_id"] == "replay_fixed-run"
    assert payload["mode"] == "replay"
    assert payload["timestamp"]
    assert payload["capability_id"] == "lookup_savings_balance"
    assert payload["step_id"] == "extract_balance"
    assert payload["action"] == "extract"
    assert payload["locator_strategy"] == "role"
    assert payload["status"] == "succeeded"
    assert payload["duration_ms"] == 12.5
    assert payload["error_code"] is None
    assert payload["data"]["value"] == REDACTED


@pytest.mark.asyncio
async def test_failure_bundle_has_predictable_files_and_sanitized_snapshot(
    tmp_path: Path,
) -> None:
    store = FileEvidenceStore(tmp_path, Redactor(["member-secret-123"]))
    observation = Observation(
        state=SurfaceState(
            surface_kind="web", location="https://bank.test/members", title="Member Search"
        ),
        semantic_tree="Member ID member-secret-123",
        visible_text="Member ID member-secret-123",
        state_fingerprint="fixture-state",
        metadata={"authorization": "Bearer abc.def.ghi"},
    )

    bundle = await store.save_failure(
        "replay_fixed-run",
        "find_member",
        Screenshot(b"synthetic-png"),
        observation,
        {"code": "TARGET_NOT_FOUND", "password": "never-write-this"},
    )

    failure_dir = tmp_path / "replay_fixed-run" / "failures" / "find_member"
    assert Path(bundle.directory) == failure_dir
    assert (failure_dir / "screenshot.png").read_bytes() == b"synthetic-png"
    page = json.loads((failure_dir / "page.json").read_text())
    error = json.loads((failure_dir / "error.json").read_text())
    assert page["semantic_snapshot"] == f"Member ID {REDACTED}"
    assert page["metadata"]["authorization"] == REDACTED
    assert error["code"] == "TARGET_NOT_FOUND"
    assert error["password"] == REDACTED
