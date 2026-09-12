"""Deterministic, sanitized evidence bundles for failed automation runs."""

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from computer_use.safety.redactor import Redactor
from computer_use.surfaces.base import Observation, Screenshot


class FailureEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory: str
    screenshot_path: str
    page_snapshot_path: str
    error_path: str


class EvidenceStore(Protocol):
    async def save_screenshot(self, run_id: str, name: str, screenshot: Screenshot) -> str: ...

    async def save_failure(
        self,
        run_id: str,
        step_id: str,
        screenshot: Screenshot,
        observation: Observation | None,
        error: dict[str, object],
    ) -> FailureEvidence: ...


class FileEvidenceStore:
    """Store deterministic reviewer-oriented run folders below the configured evidence root."""

    def __init__(self, root: Path, redactor: Redactor | None = None) -> None:
        self._root = root.resolve()
        self._redactor = redactor or Redactor()

    async def save_screenshot(self, run_id: str, name: str, screenshot: Screenshot) -> str:
        run_dir = self._run_dir(run_id) / "screenshots"
        run_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".jpg" if screenshot.media_type == "image/jpeg" else ".png"
        path = run_dir / f"{_safe_segment(name)}{suffix}"
        path.write_bytes(screenshot.data)
        return str(path)

    async def save_failure(
        self,
        run_id: str,
        step_id: str,
        screenshot: Screenshot,
        observation: Observation | None,
        error: dict[str, object],
    ) -> FailureEvidence:
        failure_dir = self._run_dir(run_id) / "failures" / _safe_segment(step_id)
        failure_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".jpg" if screenshot.media_type == "image/jpeg" else ".png"
        screenshot_path = failure_dir / f"screenshot{suffix}"
        snapshot_path = failure_dir / "page.json"
        error_path = failure_dir / "error.json"
        screenshot_path.write_bytes(screenshot.data)

        snapshot = self._snapshot(observation)
        snapshot_path.write_text(self._json(snapshot), encoding="utf-8")
        error_path.write_text(self._json(error), encoding="utf-8")
        return FailureEvidence(
            directory=str(failure_dir),
            screenshot_path=str(screenshot_path),
            page_snapshot_path=str(snapshot_path),
            error_path=str(error_path),
        )

    def _run_dir(self, run_id: str) -> Path:
        return self._root / _safe_segment(run_id)

    def _snapshot(self, observation: Observation | None) -> dict[str, object]:
        if observation is None:
            return {"available": False}
        return {
            "available": True,
            "location": observation.state.location,
            "title": observation.state.title,
            "ready": observation.state.ready,
            "active_dialog": observation.state.active_dialog,
            "state_fingerprint": observation.state_fingerprint,
            "semantic_snapshot": observation.semantic_tree[:50_000],
            "metadata": observation.metadata,
            "truncated": len(observation.semantic_tree) > 50_000,
        }

    def _json(self, payload: object) -> str:
        safe = self._redactor.redact(payload)
        return json.dumps(safe, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _safe_segment(value: str) -> str:
    sanitized = "".join(char for char in value if char.isalnum() or char in "-_")
    if not sanitized:
        raise ValueError("evidence path segment is empty after sanitization")
    return sanitized[:100]
