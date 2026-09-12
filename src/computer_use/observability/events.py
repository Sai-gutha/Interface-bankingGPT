"""Normalized, redacted, append-only telemetry for discovery and replay."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from computer_use.safety.redactor import Redactor


class RunEvent(BaseModel):
    """One reviewable event; absent dimensions are explicit nulls, never missing fields."""

    model_config = ConfigDict(extra="forbid")
    event_type: str
    run_id: str
    mode: Literal["discovery", "replay"]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    capability_id: str | None = None
    step_id: str | None = None
    action: str | None = None
    locator_strategy: str | None = None
    status: str
    duration_ms: float | None = Field(default=None, ge=0)
    error_code: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EventSink(Protocol):
    async def append(self, event: RunEvent) -> None: ...


class InMemoryEventSink:
    """Ordered redacted event sink for tests and embedded use."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        self.events: list[RunEvent] = []
        self._redactor = redactor or Redactor()

    async def append(self, event: RunEvent) -> None:
        self.events.append(_redacted_event(event, self._redactor))


class JsonlEventSink:
    """Write each run to ``evidence/<run_id>/events.jsonl`` for chronological review."""

    def __init__(self, root: Path, redactor: Redactor | None = None) -> None:
        self._root = root.resolve()
        self._redactor = redactor or Redactor()

    async def append(self, event: RunEvent) -> None:
        safe = _redacted_event(event, self._redactor)
        run_dir = self._root / _safe_segment(event.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(safe.model_dump_json() + "\n")


def _redacted_event(event: RunEvent, redactor: Redactor) -> RunEvent:
    payload = redactor.redact(event.model_dump(mode="json"))
    return RunEvent.model_validate(payload)


def _safe_segment(value: str) -> str:
    sanitized = "".join(char for char in value if char.isalnum() or char in "-_")
    if not sanitized:
        raise ValueError("run ID is empty after sanitization")
    return sanitized[:100]
