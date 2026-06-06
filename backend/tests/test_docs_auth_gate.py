"""Auth-gated OpenAPI docs (``bvphoenix.api.docs``).

These tests assert the two properties the docs surface must hold after
moving off FastAPI's public defaults:

1. The built-in ``/docs`` / ``/redoc`` / ``/openapi.json`` paths (outside
   ``/api``, which the production ingress never routes to the backend)
   are gone.
2. The re-served ``/api/docs`` / ``/api/redoc`` / ``/api/openapi.json``
   require authentication: anonymous browsers are bounced to the login
   page, the schema endpoint 401s, and authenticated callers get the
   real Swagger / ReDoc / schema.

The DB is stubbed; the anonymous paths never query it (the bearer
resolver short-circuits on missing credentials) and the authenticated
paths inject a fake user via dependency override.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bvphoenix.auth.deps import public_user, require_user
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.main import app


class _StubSession:
    async def execute(self, *_: Any, **__: Any) -> Any:  # pragma: no cover
        raise AssertionError("anonymous docs paths must not query the DB")

    async def close(self) -> None:
        return None


async def _override_get_db() -> AsyncIterator[_StubSession]:
    yield _StubSession()


def _fake_user() -> User:
    return User(
        subject_id=uuid.uuid4(),
        email="docs-tester@example.com",
        is_admin=False,
        is_active=True,
    )


@pytest.fixture(autouse=True)
def _stub_db():
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(public_user, None)
        app.dependency_overrides.pop(require_user, None)


# No ``with`` — constructing the client plainly skips the lifespan /
# startup events (which would reach for a live DB / Redis).
client = TestClient(app)


def test_builtin_doc_paths_are_disabled() -> None:
    """FastAPI's public defaults are off; the old paths 404."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_openapi_schema_requires_auth() -> None:
    """Fetched by Swagger UI's JS — anonymous callers get 401, not a
    redirect (an HTML login page would be meaningless to an XHR)."""
    resp = client.get("/api/openapi.json")
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("path", ["/api/docs", "/api/redoc"])
def test_html_docs_redirect_anonymous_to_login(path: str) -> None:
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    # Absolute URL to the frontend login, carrying an URL-encoded
    # ``next`` that points back at the same docs path.
    assert "/login?" in location
    assert "next=" in location
    assert path.replace("/", "%2F") in location


def test_swagger_served_to_authenticated_user() -> None:
    app.dependency_overrides[public_user] = _fake_user
    resp = client.get("/api/docs")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "swagger-ui" in body.lower()
    # The page must point Swagger at the auth-gated schema route.
    assert "/api/openapi.json" in body


def test_redoc_served_to_authenticated_user() -> None:
    app.dependency_overrides[public_user] = _fake_user
    resp = client.get("/api/redoc")
    assert resp.status_code == 200, resp.text
    assert "redoc" in resp.text.lower()
    assert "/api/openapi.json" in resp.text


def test_openapi_schema_served_to_authenticated_user() -> None:
    app.dependency_overrides[require_user] = _fake_user
    resp = client.get("/api/openapi.json")
    assert resp.status_code == 200, resp.text
    schema = resp.json()
    assert schema.get("openapi", "").startswith("3.")
    # A known route is present; the docs routes themselves are excluded
    # (include_in_schema=False).
    assert "/api/transparency" in schema["paths"]
    assert "/api/docs" not in schema["paths"]
