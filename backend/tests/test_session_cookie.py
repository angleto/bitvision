"""Unit tests for the canonical ``bvp_session`` cookie writer.

Password login, OIDC, and the share-link ``verify`` / ``claim`` recipient
flows must all emit a byte-identical session cookie so
``_creds_from_request`` reads any of them back. Regression guard for the
2026-06 fix: the share-link ``verify`` previously returned the JWT only
in the JSON body, which the cookie-auth SPA (post 2026-05-21 hardening)
discards via the no-op ``setStoredToken`` — so an external recipient hit
401 "authentication required" on the destination page.
"""

from __future__ import annotations

from fastapi import Response

from bvphoenix.auth.deps import clear_session_cookie, set_session_cookie


class _Req:
    """Minimal stand-in: the writer only reads ``request.url.scheme``."""

    def __init__(self, scheme: str) -> None:
        self.url = type("U", (), {"scheme": scheme})()


def test_https_cookie_is_secure_httponly_lax_root() -> None:
    resp = Response()
    set_session_cookie(resp, _Req("https"), "tok123", max_age=3600)  # type: ignore[arg-type]
    h = resp.headers["set-cookie"].lower()
    assert "bvp_session=tok123" in h
    assert "httponly" in h
    assert "secure" in h
    assert "samesite=lax" in h
    assert "path=/" in h
    assert "max-age=3600" in h


def test_http_cookie_omits_secure() -> None:
    # http://localhost dev must stay usable: no Secure flag, otherwise
    # the browser drops the cookie over plain http and login breaks.
    resp = Response()
    set_session_cookie(resp, _Req("http"), "tok123", max_age=60)  # type: ignore[arg-type]
    h = resp.headers["set-cookie"].lower()
    assert "bvp_session=tok123" in h
    assert "httponly" in h
    assert "secure" not in h


def test_clear_session_cookie_expires_it() -> None:
    resp = Response()
    clear_session_cookie(resp, _Req("https"))  # type: ignore[arg-type]
    h = resp.headers["set-cookie"].lower()
    assert "bvp_session=" in h
    assert "max-age=0" in h or "expires=" in h
