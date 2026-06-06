"""Authenticated OpenAPI documentation surface.

FastAPI's built-in ``docs_url`` / ``redoc_url`` / ``openapi_url`` are
disabled in ``main.py``; this module re-serves the same three artifacts
under the ``/api`` prefix and behind authentication. Two problems are
solved at once:

1. **Routing.** The production ingress forwards only ``/api/*`` to the
   backend (see ``deploy/.../ingress-bvphoenix.yaml``); everything else
   goes to the Next.js frontend. FastAPI's defaults live at ``/docs`` /
   ``/openapi.json`` — outside ``/api`` — so in production they were
   shadowed by the SPA catch-all and returned the frontend's 404. Moving
   them under ``/api`` puts them on the only path the ingress routes to
   the backend.

2. **Auth.** The docs used to be public. They now require a logged-in
   user. The auth layer already resolves the bearer credential from the
   ``bvp_session`` HttpOnly cookie *or* the ``Authorization`` header
   (see ``auth/deps.py::_creds_from_request``), so a browser navigating
   to ``/api/docs`` carries its session cookie automatically and the
   Swagger UI page can fetch ``/api/openapi.json`` same-origin with the
   same cookie — no token-injection JS required.

Anonymous handling differs by content type:

* The two HTML routes (``/docs``, ``/redoc``) resolve the user
  permissively (``public_user`` never raises) and, when there is no
  session, 302-redirect the browser to the frontend login page with a
  ``?next=`` pointing back here. A raw 401 JSON body would be a poor
  experience for a link clicked from the public landing footer.
* ``/openapi.json`` is fetched by Swagger UI's JavaScript, where a
  redirect to an HTML login page would be meaningless, so it hard-gates
  with ``require_user`` and returns 401 to anonymous callers. In the
  normal flow the HTML route has already bounced anonymous browsers to
  login before any schema fetch happens.

The Swagger UI / ReDoc bundles are loaded from FastAPI's default CDN
(jsdelivr). The backend emits no CSP (security headers come from the
Next.js layer, which the ingress does not apply to ``/api``), so the
CDN assets load. Self-hosting ``swagger-ui-dist`` is a possible future
hardening step; it is out of scope here since the docs are now
operator-facing only.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from bvphoenix.auth.deps import public_user, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import User

router = APIRouter(tags=["docs"])

_OPENAPI_PATH = "/api/openapi.json"


def _login_redirect(return_to: str) -> RedirectResponse:
    """Bounce an anonymous browser to the frontend login page.

    Built from ``public_frontend_url`` (absolute) so the redirect works
    in both the split-origin dev setup (FE :3000 / BE :8000) and the
    single-origin production deployment. ``next`` is URL-encoded so the
    login form's ``safeInternalPath`` validator receives a clean
    same-origin path.
    """
    base = get_settings().public_frontend_url.rstrip("/")
    qs = urlencode({"next": return_to})
    return RedirectResponse(url=f"{base}/login?{qs}", status_code=302)


@router.get("/openapi.json", include_in_schema=False)
async def openapi_schema(
    request: Request,
    _: Annotated[User, Depends(require_user)],
) -> JSONResponse:
    """Serve the OpenAPI schema to authenticated callers only.

    Fetched by the Swagger UI / ReDoc pages via XHR; the browser sends
    the ``bvp_session`` cookie same-origin, which ``require_user``
    resolves. Anonymous callers get 401 (the HTML routes redirect them
    to login before they ever reach this).
    """
    return JSONResponse(request.app.openapi())


@router.get("/docs", include_in_schema=False, response_model=None)
async def swagger_ui(
    user: Annotated[User | None, Depends(public_user)],
) -> HTMLResponse | RedirectResponse:
    """Swagger UI, gated. Anonymous browsers are sent to login."""
    if user is None:
        return _login_redirect("/api/docs")
    return get_swagger_ui_html(
        openapi_url=_OPENAPI_PATH,
        title="bitvision phoenix — API docs",
        # The API authenticates with a plain bearer / session cookie,
        # not an OAuth2 redirect flow, so the oauth2-redirect helper
        # route (which FastAPI would otherwise mount alongside the
        # default docs) is unnecessary. Disabling it avoids referencing
        # a route that no longer exists once the defaults are off.
        oauth2_redirect_url=None,
    )


@router.get("/redoc", include_in_schema=False, response_model=None)
async def redoc_ui(
    user: Annotated[User | None, Depends(public_user)],
) -> HTMLResponse | RedirectResponse:
    """ReDoc, gated. Anonymous browsers are sent to login."""
    if user is None:
        return _login_redirect("/api/redoc")
    return get_redoc_html(
        openapi_url=_OPENAPI_PATH,
        title="bitvision phoenix — API docs",
    )
