"""Server-side integration coverage for deterministic demo fixtures."""

from fastapi.testclient import TestClient

from demo_app.app import app


def session_client() -> TestClient:
    client = TestClient(app)
    assert client.post("/session", follow_redirects=False).status_code == 303
    return client


def test_normal_member_and_accounts_render() -> None:
    response = session_client().post(
        "/members/search", data={"member_number": "12345"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert all(
        text in response.text
        for text in ("Avery Sample", "Everyday Checking", "$2,480.75", "Rainy Day Savings")
    )


def test_operations_menu_routes_render_useful_screens() -> None:
    client = session_client()
    expectations = {
        "/accounts": ("Account Maintenance", "CHK-1042", "View member"),
        "/reports": ("Daily Reports", "RPT-117", "Control totals"),
        "/messages": ("System Messages", "Planned maintenance", "3 unread notices"),
    }
    for route, expected_text in expectations.items():
        response = client.get(route)
        assert response.status_code == 200
        assert all(text in response.text for text in expected_text)


def test_operations_menu_routes_require_session() -> None:
    client = TestClient(app)
    for route in ("/accounts", "/reports", "/messages"):
        response = client.get(route, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/session-expired"


def test_unknown_member_is_business_outcome() -> None:
    response = session_client().post(
        "/members/search", data={"member_number": "55555"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "Record not found" in response.text
    assert "normal business outcome" in response.text


def test_transfer_validation_error() -> None:
    response = session_client().post(
        "/members/12345/transfer/review",
        data={
            "from_account": "CHK-1042",
            "to_account": "CHK-1042",
            "amount": "99999.00",
            "memo": "Synthetic test",
        },
    )
    assert response.status_code == 422
    assert "Source and destination accounts must be different" in response.text
    assert "exceeds the available balance" in response.text


def test_permission_denied_fixture() -> None:
    response = session_client().post(
        "/members/search", data={"member_number": "80008"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "OPS-AUTH-403" in response.text


def test_session_expiry_fixture_invalidates_cookie() -> None:
    response = session_client().post(
        "/members/search", data={"member_number": "90009"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "Session Expired" in response.text


def test_review_precedes_submission_and_can_request_dialog() -> None:
    response = session_client().post(
        "/members/12345/transfer/review",
        data={
            "from_account": "CHK-1042",
            "to_account": "SAV-7781",
            "amount": "13.37",
            "memo": "Dialog fixture",
        },
    )
    assert response.status_code == 200
    assert "Funds have not moved" in response.text
    assert "data-operations-dialog" in response.text
    assert "Submit Transfer" in response.text
