"""API construction smoke test."""

from fastapi.testclient import TestClient

from computer_use.api.app import create_app


def test_health() -> None:
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
