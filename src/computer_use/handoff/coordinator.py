"""In-process handoff coordinator retaining one live surface for both owners."""

import asyncio
import base64
from dataclasses import dataclass, field
from time import perf_counter
from typing import Literal

from computer_use.handoff.models import (
    ControlLease,
    ControlOwner,
    HumanAction,
    InterventionRequest,
    InterventionStatus,
    ResumeSignal,
)
from computer_use.observability.events import EventSink, RunEvent
from computer_use.surfaces.base import SurfaceAdapter


class HandoffError(RuntimeError):
    pass


@dataclass(slots=True)
class _LiveSession:
    surface: SurfaceAdapter
    mode: Literal["discovery", "replay"]
    goal: str | None
    capability_id: str | None
    lease: ControlLease
    intervention: InterventionRequest | None = None
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    resume_signal: ResumeSignal | None = None


class InMemoryHandoffCoordinator:
    """Atomically transfer a registered live surface without creating another session."""

    def __init__(self, events: EventSink) -> None:
        self._events = events
        self._sessions: dict[str, _LiveSession] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        run_id: str,
        surface: SurfaceAdapter,
        *,
        mode: Literal["discovery", "replay"],
        goal: str | None = None,
        capability_id: str | None = None,
    ) -> ControlLease:
        async with self._lock:
            if run_id in self._sessions:
                raise HandoffError("run already has a registered live session")
            lease = ControlLease(run_id=run_id, owner=ControlOwner.AUTOMATION, epoch=1)
            self._sessions[run_id] = _LiveSession(
                surface=surface,
                mode=mode,
                goal=goal,
                capability_id=capability_id,
                lease=lease,
            )
            return lease

    async def request(
        self, run_id: str, *, current_step: str | None, reason_code: str, reason: str
    ) -> InterventionRequest:
        session = self._session(run_id)
        async with self._lock:
            if session.lease.owner != ControlOwner.AUTOMATION:
                raise HandoffError("automation does not own this session")
            observation = await session.surface.observe()
            screenshot = await session.surface.screenshot()
            epoch = session.lease.epoch + 1
            session.lease = ControlLease(run_id=run_id, owner=ControlOwner.HUMAN, epoch=epoch)
            session.resume_event.clear()
            session.resume_signal = None
            intervention = InterventionRequest(
                run_id=run_id,
                mode=session.mode,
                goal=session.goal,
                capability_id=session.capability_id,
                current_step=current_step,
                reason_code=reason_code,
                reason=reason,
                screenshot_data_url=(
                    f"data:{screenshot.media_type};base64,"
                    f"{base64.b64encode(screenshot.data).decode('ascii')}"
                ),
                current_url=observation.state.location,
                state_fingerprint=observation.state_fingerprint,
                page_title=observation.state.title,
                epoch=epoch,
            )
            session.intervention = intervention
        await self._event(session, "intervention_requested", "paused", current_step)
        return intervention

    async def claim(self, run_id: str, operator_id: str, expected_epoch: int) -> ControlLease:
        async with self._lock:
            session = self._session(run_id)
            self._check_epoch(session, expected_epoch)
            if session.lease.owner != ControlOwner.HUMAN:
                raise HandoffError("session is not awaiting a human")
            if session.lease.operator_id not in {None, operator_id}:
                raise HandoffError("session is already claimed by another operator")
            session.lease = session.lease.model_copy(update={"operator_id": operator_id})
            if session.intervention:
                session.intervention = session.intervention.model_copy(
                    update={
                        "status": InterventionStatus.CLAIMED,
                        "operator_id": operator_id,
                    }
                )
        await self._event(session, "intervention_claimed", "claimed")
        return session.lease

    async def perform(
        self, run_id: str, operator_id: str, expected_epoch: int, action: HumanAction
    ) -> None:
        session = self._owned_session(run_id, operator_id, expected_epoch)
        started = perf_counter()
        if action.kind == "navigate":
            await session.surface.navigate(action.url or "")
        elif action.kind == "click":
            if action.target is None:
                raise HandoffError("click target missing after validation")
            await session.surface.click(action.target)
        elif action.kind == "type":
            if action.target is None:
                raise HandoffError("type target missing after validation")
            await session.surface.type(action.target, action.value or "")
        elif action.kind == "select":
            if action.target is None:
                raise HandoffError("select target missing after validation")
            await session.surface.select(action.target, action.value or "")
        await self._event(
            session,
            "human_action",
            "succeeded",
            action=action.kind,
            duration_ms=(perf_counter() - started) * 1000,
            data={
                "operator_id": operator_id,
                "target": action.target.description if action.target else None,
            },
        )

    async def resume(self, signal: ResumeSignal) -> ControlLease:
        async with self._lock:
            session = self._owned_session(signal.run_id, signal.operator_id, signal.epoch)
            session.resume_signal = signal
            session.lease = ControlLease(
                run_id=signal.run_id,
                owner=ControlOwner.AUTOMATION,
                epoch=session.lease.epoch + 1,
            )
            if session.intervention:
                session.intervention = session.intervention.model_copy(
                    update={"status": InterventionStatus.RESUMED}
                )
            session.resume_event.set()
        await self._event(session, "automation_resumed", "resumed", data={"note": signal.note})
        return session.lease

    async def wait_for_resume(self, run_id: str) -> ResumeSignal:
        session = self._session(run_id)
        await session.resume_event.wait()
        if session.resume_signal is None:
            raise HandoffError("resume signal missing")
        return session.resume_signal

    async def get(self, run_id: str) -> InterventionRequest:
        intervention = self._session(run_id).intervention
        if intervention is None:
            raise HandoffError("run has no intervention")
        return intervention

    async def list_active(self) -> list[InterventionRequest]:
        return [
            session.intervention
            for session in self._sessions.values()
            if session.intervention and session.intervention.status != InterventionStatus.RESUMED
        ]

    def _session(self, run_id: str) -> _LiveSession:
        try:
            return self._sessions[run_id]
        except KeyError as error:
            raise HandoffError("unknown live run") from error

    def _owned_session(self, run_id: str, operator_id: str, expected_epoch: int) -> _LiveSession:
        session = self._session(run_id)
        self._check_epoch(session, expected_epoch)
        if session.lease.owner != ControlOwner.HUMAN:
            raise HandoffError("human does not own this session")
        if session.lease.operator_id != operator_id:
            raise HandoffError("operator does not own this session")
        return session

    @staticmethod
    def _check_epoch(session: _LiveSession, expected_epoch: int) -> None:
        if session.lease.epoch != expected_epoch:
            raise HandoffError("stale control epoch")

    async def _event(
        self,
        session: _LiveSession,
        event_type: str,
        status: str,
        step_id: str | None = None,
        *,
        action: str | None = None,
        duration_ms: float | None = None,
        data: dict[str, object] | None = None,
    ) -> None:
        await self._events.append(
            RunEvent(
                event_type=event_type,
                run_id=session.lease.run_id,
                mode=session.mode,
                capability_id=session.capability_id,
                step_id=step_id,
                action=action,
                status=status,
                duration_ms=duration_ms,
                data=data or {},
            )
        )
