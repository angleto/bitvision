"""Smoke test for the liveness endpoint."""

from fastapi.testclient import TestClient

from bvphoenix.main import app

client = TestClient(app)


def test_health_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_root_ok() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "bitvision phoenix"
