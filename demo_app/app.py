"""Local legacy-style banking application used as the automation target."""

import asyncio
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

APP_DIR = Path(__file__).parent
SESSION_COOKIE = "ai_interface_session"
SESSION_VALUE = "synthetic-training-session"

app = FastAPI(title="AI-interface Banking Operations", docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=APP_DIR / "static"), name="assets")
templates = Jinja2Templates(directory=APP_DIR / "templates")


@dataclass(frozen=True)
class Account:
    number: str
    kind: str
    nickname: str
    balance: Decimal
    available: Decimal


@dataclass(frozen=True)
class Member:
    member_id: str
    display_name: str
    relationship_since: str
    status: str
    contact_hint: str
    accounts: tuple[Account, ...]


MEMBERS: dict[str, Member] = {
    "12345": Member(
        "12345",
        "Avery Sample",
        "2018-04-12",
        "Active",
        "Synthetic profile - no real PII",
        (
            Account(
                "CHK-1042", "Checking", "Everyday Checking", Decimal("2480.75"), Decimal("2380.75")
            ),
            Account(
                "SAV-7781", "Savings", "Rainy Day Savings", Decimal("9125.40"), Decimal("9125.40")
            ),
        ),
    ),
    "70007": Member(
        "70007",
        "Morgan Example",
        "2021-09-03",
        "Active",
        "Synthetic slow-load fixture",
        (
            Account(
                "CHK-7001", "Checking", "Primary Checking", Decimal("815.20"), Decimal("815.20")
            ),
            Account(
                "SAV-7002", "Savings", "Reserve Savings", Decimal("3300.00"), Decimal("3300.00")
            ),
        ),
    ),
    "80008": Member(
        "80008",
        "Restricted Training Record",
        "2020-01-08",
        "Restricted",
        "Synthetic permission-denial fixture",
        (),
    ),
    "90009": Member(
        "90009",
        "Expired Session Fixture",
        "2022-11-09",
        "Active",
        "Synthetic session-expiry fixture",
        (),
    ),
}

PENDING_TRANSFERS: dict[str, dict[str, str]] = {}


def render(
    request: Request, template: str, *, status_code: int = 200, **context: object
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=context,
        status_code=status_code,
    )


def require_session(request: Request) -> str | RedirectResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if not secrets.compare_digest(token or "", SESSION_VALUE):
        return RedirectResponse("/session-expired", status_code=303)
    return SESSION_VALUE


def lookup_account(member: Member, account_number: str) -> Account | None:
    return next((item for item in member.accounts if item.number == account_number), None)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return render(request, "login.html", page_title="Operator Sign In")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.post("/session")
async def start_session() -> RedirectResponse:
    response = RedirectResponse("/members", status_code=303)
    response.set_cookie(SESSION_COOKIE, SESSION_VALUE, httponly=True, samesite="strict")
    return response


@app.post("/session/end")
async def end_session(request: Request) -> RedirectResponse:
    response = RedirectResponse("/session-expired", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/session-expired", response_class=HTMLResponse)
async def expired(request: Request) -> HTMLResponse:
    return render(request, "session_expired.html", page_title="Session Expired")


@app.get("/members", response_class=HTMLResponse)
async def member_search(request: Request, message: str | None = None) -> Response:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    return render(request, "member_search.html", message=message, page_title="Member Search")


@app.get("/accounts", response_class=HTMLResponse)
async def account_maintenance(request: Request) -> Response:
    """Show a synthetic work queue without exposing restricted fixture records."""
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    members = [MEMBERS["12345"], MEMBERS["70007"]]
    return render(
        request,
        "account_maintenance.html",
        members=members,
        page_title="Account Maintenance",
    )


@app.get("/reports", response_class=HTMLResponse)
async def daily_reports(request: Request) -> Response:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    reports = (
        ("RPT-101", "Daily deposit totals", "Ready", "06:15 ET"),
        ("RPT-117", "Transfer exception summary", "2 exceptions", "06:22 ET"),
        ("RPT-204", "Dormant-account review", "Processing", "07:00 ET"),
    )
    return render(request, "daily_reports.html", reports=reports, page_title="Daily Reports")


@app.get("/messages", response_class=HTMLResponse)
async def system_messages(request: Request) -> Response:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    messages = (
        ("High", "Planned maintenance", "Training host restarts Saturday at 23:00 ET."),
        ("Normal", "Procedure update", "Review transfer details before final submission."),
        ("Normal", "Synthetic-data reminder", "Do not enter real member information."),
    )
    return render(request, "system_messages.html", messages=messages, page_title="System Messages")


@app.post("/members/search")
async def search_member(
    request: Request, member_number: Annotated[str, Form()]
) -> RedirectResponse:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    member_id = member_number.strip()
    if not member_id.isdigit() or len(member_id) != 5:
        message = quote("Member ID must contain exactly five digits.")
        return RedirectResponse(f"/members?message={message}", status_code=303)
    if member_id == "70007":
        await asyncio.sleep(2.25)
    if member_id == "80008":
        return RedirectResponse("/permission-denied?resource=member+80008", status_code=303)
    if member_id == "90009":
        response = RedirectResponse("/session-expired", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response
    if member_id not in MEMBERS:
        return RedirectResponse(f"/members/not-found?member_number={member_id}", status_code=303)
    return RedirectResponse(f"/members/{member_id}", status_code=303)


@app.get("/members/not-found", response_class=HTMLResponse)
async def member_not_found(request: Request, member_number: str) -> Response:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    return render(
        request,
        "member_not_found.html",
        member_number=member_number,
        page_title="No Matching Member",
    )


@app.get("/permission-denied", response_class=HTMLResponse)
async def permission_denied(request: Request, resource: str = "requested record") -> Response:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    return render(request, "permission_denied.html", resource=resource, page_title="Access Denied")


@app.get("/members/{member_id}", response_class=HTMLResponse)
async def member_detail(request: Request, member_id: str) -> Response:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    member = MEMBERS.get(member_id)
    if member is None:
        return RedirectResponse(f"/members/not-found?member_number={member_id}", status_code=303)
    return render(request, "member_detail.html", member=member, page_title="Member Details")


@app.get("/members/{member_id}/transfer", response_class=HTMLResponse)
async def transfer_form(request: Request, member_id: str) -> Response:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    member = MEMBERS.get(member_id)
    if member is None:
        return RedirectResponse(f"/members/not-found?member_number={member_id}", status_code=303)
    return render(
        request,
        "transfer_form.html",
        member=member,
        values={},
        errors=[],
        page_title="Transfer Funds",
    )


@app.post("/members/{member_id}/transfer/review", response_class=HTMLResponse)
async def review_transfer(
    request: Request,
    member_id: str,
    from_account: Annotated[str, Form()],
    to_account: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    memo: Annotated[str, Form()] = "",
) -> Response:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    member = MEMBERS.get(member_id)
    if member is None:
        return RedirectResponse(f"/members/not-found?member_number={member_id}", status_code=303)

    errors: list[str] = []
    source = lookup_account(member, from_account)
    destination = lookup_account(member, to_account)
    if source is None:
        errors.append("Select a valid source account.")
    if destination is None:
        errors.append("Select a valid destination account.")
    if from_account == to_account:
        errors.append("Source and destination accounts must be different.")
    try:
        parsed_amount = Decimal(amount).quantize(Decimal("0.01"))
        if parsed_amount <= 0:
            errors.append("Transfer amount must be greater than zero.")
        elif source and parsed_amount > source.available:
            errors.append("Transfer amount exceeds the available balance.")
    except InvalidOperation:
        parsed_amount = Decimal("0")
        errors.append("Enter a valid dollar amount.")

    values = {
        "from_account": from_account,
        "to_account": to_account,
        "amount": amount,
        "memo": memo,
    }
    if errors:
        return render(
            request,
            "transfer_form.html",
            member=member,
            values=values,
            errors=errors,
            page_title="Transfer Funds - Validation Error",
            status_code=422,
        )

    transfer_id = secrets.token_urlsafe(12)
    PENDING_TRANSFERS[transfer_id] = {
        "member_id": member_id,
        "from_account": from_account,
        "to_account": to_account,
        "amount": f"{parsed_amount:.2f}",
        "memo": memo.strip(),
    }
    return render(
        request,
        "transfer_review.html",
        member=member,
        source=source,
        destination=destination,
        transfer=PENDING_TRANSFERS[transfer_id],
        transfer_id=transfer_id,
        show_unexpected_dialog=parsed_amount == Decimal("13.37"),
        page_title="Review Transfer",
    )


@app.post("/transfers/{transfer_id}/submit", response_class=HTMLResponse)
async def submit_transfer(request: Request, transfer_id: str) -> Response:
    if isinstance(auth := require_session(request), RedirectResponse):
        return auth
    transfer = PENDING_TRANSFERS.pop(transfer_id, None)
    if transfer is None:
        return render(
            request,
            "transfer_missing.html",
            page_title="Transfer No Longer Available",
            status_code=409,
        )
    return render(
        request,
        "transfer_complete.html",
        transfer=transfer,
        reference=f"TRN-{secrets.token_hex(4).upper()}",
        page_title="Transfer Submitted",
    )
