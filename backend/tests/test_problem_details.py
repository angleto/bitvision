"""Tests for the RFC 9457 Problem Details middleware."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from bvphoenix.middleware.problem_details import (
    PROBLEM_CONTENT_TYPE,
    install_problem_details,
    problem,
)


def _app() -> FastAPI:
    app = FastAPI()
    install_problem_details(app)

    router = APIRouter()

    @router.get("/boom")
    def boom() -> None:
        raise HTTPException(status_code=404, detail="not here")

    @router.get("/etag-conflict")
    def etag_conflict() -> None:
        raise problem(
            412,
            "etag_mismatch",
            "current ETag is x",
            extra={"current_etag": "x"},
        )

    class Body(BaseModel):
        n: int

    @router.post("/validate")
    def validate(body: Body) -> dict[str, int]:
        return {"n": body.n}

    app.include_router(router)
    return app


def test_404_emits_problem_details() -> None:
    client = TestClient(_app())
    resp = client.get("/boom")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = resp.json()
    assert body["status"] == 404
    assert body["title"] == "Not found"
    assert body["instance"] == "/boom"
    assert body["type"].endswith("/not_found")
    assert body["detail"] == "not here"


def test_problem_helper_round_trip() -> None:
    client = TestClient(_app())
    resp = client.get("/etag-conflict")
    assert resp.status_code == 412
    body = resp.json()
    assert body["type"].endswith("/etag_mismatch")
    assert body["title"] == "ETag mismatch"
    assert body["current_etag"] == "x"
    assert body["instance"] == "/etag-conflict"


def test_validation_error_uses_problem_details() -> None:
    client = TestClient(_app())
    resp = client.post("/validate", json={"n": "not-a-number"})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = resp.json()
    assert body["status"] == 422
    assert body["type"].endswith("/validation_failed")
    assert "errors" in body
