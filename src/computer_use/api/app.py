"""HTTP API and minimal operator console for same-session intervention."""

from html import escape
from typing import Annotated, Literal

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from computer_use.artifacts.models import CoordinateLocator, LabelLocator, Target
from computer_use.handoff.coordinator import HandoffError, InMemoryHandoffCoordinator
from computer_use.handoff.models import HumanAction, ResumeSignal


def create_app(coordinator: InMemoryHandoffCoordinator | None = None) -> FastAPI:
    """Create the API; browser ownership remains with the injected coordinator."""

    application = FastAPI(title="Computer-Use Automation System", version="0.1.0")

    def handoff() -> InMemoryHandoffCoordinator:
        if coordinator is None:
            raise HTTPException(status_code=503, detail="operator handoff is not configured")
        return coordinator

    @application.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/operator", response_class=HTMLResponse, tags=["operator"])
    async def interventions() -> str:
        active = await handoff().list_active()
        items = (
            "".join(
                f'<li><a href="/operator/{escape(item.run_id)}">{escape(item.run_id)}</a> '
                f"— {escape(item.reason_code)}</li>"
                for item in active
            )
            or "<li>No active interventions</li>"
        )
        return _page("Interventions", f"<h1>Interventions</h1><ul>{items}</ul>")

    @application.get("/operator/{run_id}", response_class=HTMLResponse, tags=["operator"])
    async def intervention(run_id: str) -> str:
        try:
            item = await handoff().get(run_id)
        except HandoffError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        operator = escape(item.operator_id or "operator-1")
        epoch = item.epoch
        body = f"""
        <h1>Human intervention</h1>
        <dl>
          <dt>Run</dt><dd>{escape(item.run_id)}</dd>
          <dt>Goal / capability</dt><dd>{escape(item.goal or item.capability_id or "—")}</dd>
          <dt>Step</dt><dd>{escape(item.current_step or "—")}</dd>
          <dt>Reason</dt><dd>{escape(item.reason_code)} — {escape(item.reason)}</dd>
          <dt>URL</dt><dd>{escape(item.current_url)}</dd>
          <dt>State</dt><dd>{escape(item.state_fingerprint)}</dd>
          <dt>Status</dt><dd>{escape(item.status.value)}</dd>
        </dl>
        <img src="{item.screenshot_data_url}" alt="Current browser screenshot">
        <h2>Claim</h2>
        <form method="post" action="/operator/{escape(run_id)}/claim">
          <input name="operator_id" value="{operator}">
          <input type="hidden" name="epoch" value="{epoch}"><button>Take control</button>
        </form>
        <h2>Operate the same browser session</h2>
        <form method="post" action="/operator/{escape(run_id)}/action">
          <input type="hidden" name="operator_id" value="{operator}">
          <input type="hidden" name="epoch" value="{epoch}">
          <select name="kind">
            <option>click</option><option>type</option><option>navigate</option>
          </select>
          <label>Label <input name="label"></label>
          <label>Value / URL <input name="value"></label>
          <label>X <input name="x" type="number"></label>
          <label>Y <input name="y" type="number"></label>
          <button>Execute</button>
        </form>
        <h2>Resume automation</h2>
        <form method="post" action="/operator/{escape(run_id)}/resume">
          <input type="hidden" name="operator_id" value="{operator}">
          <input type="hidden" name="epoch" value="{epoch}">
          <label><input type="checkbox" name="completed" value="true" checked>
            Current step completed</label>
          <label>Note <input name="note"></label><button>RESUME</button>
        </form>
        """
        return _page("Human intervention", body)

    @application.post("/operator/{run_id}/claim", tags=["operator"])
    async def claim(
        run_id: str,
        operator_id: Annotated[str, Form()],
        epoch: Annotated[int, Form()],
    ) -> RedirectResponse:
        try:
            await handoff().claim(run_id, operator_id, epoch)
        except HandoffError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return RedirectResponse(f"/operator/{run_id}", status_code=303)

    @application.post("/operator/{run_id}/action", tags=["operator"])
    async def human_action(
        run_id: str,
        operator_id: Annotated[str, Form()],
        epoch: Annotated[int, Form()],
        kind: Annotated[Literal["click", "type", "navigate"], Form()],
        value: Annotated[str, Form()] = "",
        label: Annotated[str, Form()] = "",
        x: Annotated[int | None, Form()] = None,
        y: Annotated[int | None, Form()] = None,
    ) -> RedirectResponse:
        if kind == "navigate":
            action = HumanAction(kind="navigate", url=value)
        else:
            target = _operator_target(label, x, y)
            action = HumanAction(kind=kind, target=target, value=value if kind == "type" else None)
        try:
            await handoff().perform(run_id, operator_id, epoch, action)
        except (HandoffError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return RedirectResponse(f"/operator/{run_id}", status_code=303)

    @application.post("/operator/{run_id}/resume", tags=["operator"])
    async def resume(
        run_id: str,
        operator_id: Annotated[str, Form()],
        epoch: Annotated[int, Form()],
        completed: Annotated[bool, Form()] = False,
        note: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        try:
            await handoff().resume(
                ResumeSignal(
                    run_id=run_id,
                    epoch=epoch,
                    operator_id=operator_id,
                    current_step_completed=completed,
                    note=note or None,
                )
            )
        except HandoffError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return RedirectResponse("/operator", status_code=303)

    return application


def _operator_target(label: str, x: int | None, y: int | None) -> Target:
    if label:
        return Target(
            description=f"Operator-selected {label}", candidates=[LabelLocator(label=label)]
        )
    if x is not None and y is not None:
        return Target(
            description="Operator-selected screen point",
            candidates=[CoordinateLocator(x=x, y=y, viewport_width=1440, viewport_height=900)],
        )
    raise ValueError("click/type requires a label, or click coordinates")


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><title>{escape(title)}</title>
    <style>
      body{{font:14px sans-serif;max-width:1100px;margin:2rem auto}}
      img{{max-width:100%;border:1px solid #999}}
      form{{margin:1rem 0;padding:1rem;background:#eee}} label{{margin:.5rem}}
    </style>
    </head><body>{body}</body></html>"""


app = create_app()
