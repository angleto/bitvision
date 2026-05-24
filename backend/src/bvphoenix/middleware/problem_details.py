"""RFC 9457 Problem Details exception handler.

Every error emitted by FastAPI / Starlette goes through ``HTTPException``
or ``RequestValidationError``; this module wires both into a JSON body
that conforms to the ``application/problem+json`` media type defined by
RFC 9457 (the successor of RFC 7807).

Why first-class problem details?
--------------------------------
The Agents API spec (sezione 2.5) requires structured errors so a
single agent can branch on ``type`` without parsing the human-readable
``detail``. Beyond the agent use case, this also gives REST clients a
stable contract for telemetry and retry logic — e.g. an SDK can detect
``etag_mismatch`` and re-fetch transparently.

Body shape
----------
``{
    "type": "https://bitvision.example/errors/<slug>",
    "title": "<short human-readable summary>",
    "status": <int>,
    "detail": "<long form explanation>",
    "instance": "<request path>",
    ... extra members ...
}``

The ``extra`` members are merged from ``HTTPException.detail`` when it is
already a dict. This lets endpoints raise a structured payload directly
(``raise problem(412, "etag_mismatch", "...", extra={"current_etag": "..."})``)
without forcing every call site to assemble the canonical envelope.

The handler is intentionally conservative: it never inspects the
exception traceback (PHI risk). All sensitive metadata stays in the
server logs; the wire body only carries the contract fields.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE: Final[str] = "application/problem+json"

# Base URI used for the ``type`` member. Public-stable URL: even if the
# host changes, agents can pin on the slug. We never require this to
# resolve — RFC 9457 explicitly allows opaque type URIs.
_TYPE_BASE: Final[str] = "https://bitvision.example/errors"


# Canonical short slugs by HTTP status. Endpoints can override the slug
# explicitly; this is the *fallback* when the caller raised a plain
# ``HTTPException`` without a slug.
_DEFAULT_SLUGS: Final[dict[int, str]] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_412_PRECONDITION_FAILED: "etag_mismatch",
    status.HTTP_413_CONTENT_TOO_LARGE: "payload_too_large",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "validation_failed",
    status.HTTP_423_LOCKED: "locked",
    status.HTTP_428_PRECONDITION_REQUIRED: "precondition_required",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
    status.HTTP_502_BAD_GATEWAY: "bad_gateway",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
    status.HTTP_504_GATEWAY_TIMEOUT: "gateway_timeout",
}


_DEFAULT_TITLES: Final[dict[int, str]] = {
    status.HTTP_400_BAD_REQUEST: "Bad request",
    status.HTTP_401_UNAUTHORIZED: "Authentication required",
    status.HTTP_403_FORBIDDEN: "Forbidden",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "Method not allowed",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_412_PRECONDITION_FAILED: "ETag mismatch",
    status.HTTP_413_CONTENT_TOO_LARGE: "Payload too large",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "Unsupported media type",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Validation failed",
    status.HTTP_423_LOCKED: "Locked",
    status.HTTP_428_PRECONDITION_REQUIRED: "Precondition required",
    status.HTTP_429_TOO_MANY_REQUESTS: "Rate limit exceeded",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "Internal error",
    status.HTTP_502_BAD_GATEWAY: "Bad gateway",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service unavailable",
    status.HTTP_504_GATEWAY_TIMEOUT: "Gateway timeout",
}


def _slug_for(status_code: int) -> str:
    return _DEFAULT_SLUGS.get(status_code, f"http_{status_code}")


def _title_for(status_code: int) -> str:
    return _DEFAULT_TITLES.get(status_code, f"HTTP {status_code}")


def problem(
    status_code: int,
    slug: str,
    detail: str,
    *,
    title: str | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> StarletteHTTPException:
    """Build an ``HTTPException`` whose ``detail`` is already structured.

    Use this in endpoints when you want full control over the slug and
    the extra members surfaced to the agent — e.g.::

        raise problem(
            412, "etag_mismatch",
            "current ETag is x, you sent y",
            extra={"current_etag": "x"},
        )
    """
    payload: dict[str, Any] = {
        "type": f"{_TYPE_BASE}/{slug}",
        "title": title or _title_for(status_code),
        "status": status_code,
        "detail": detail,
    }
    if extra:
        # Avoid clobbering the canonical members; extras are merged after.
        for key in ("type", "title", "status", "detail", "instance"):
            extra.pop(key, None)
        payload.update(extra)
    return StarletteHTTPException(
        status_code=status_code,
        detail=payload,  # type: ignore[arg-type]
        headers=headers,
    )


def _build_body(
    status_code: int,
    detail: Any,
    request: Request,
) -> dict[str, Any]:
    """Normalise an HTTPException ``detail`` into a Problem Details body."""
    body: dict[str, Any]
    if isinstance(detail, dict):
        body = dict(detail)
        body.setdefault("type", f"{_TYPE_BASE}/{_slug_for(status_code)}")
        body.setdefault("title", _title_for(status_code))
        body.setdefault("status", status_code)
        body.setdefault("detail", _title_for(status_code))
    else:
        body = {
            "type": f"{_TYPE_BASE}/{_slug_for(status_code)}",
            "title": _title_for(status_code),
            "status": status_code,
            "detail": str(detail) if detail is not None else _title_for(status_code),
        }
    body.setdefault("instance", request.url.path)
    return body


async def _http_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    body = _build_body(exc.status_code, exc.detail, request)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=exc.headers,
    )


async def _validation_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    body = {
        "type": f"{_TYPE_BASE}/validation_failed",
        "title": _title_for(status.HTTP_422_UNPROCESSABLE_CONTENT),
        "status": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "detail": "Request body failed validation",
        "instance": request.url.path,
        "errors": exc.errors(),
    }
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
    )


def install_problem_details(app: FastAPI) -> None:
    """Register the Problem Details handlers on ``app``.

    Call once during application setup, after the routers are
    registered. The handlers cover both Starlette's
    :class:`HTTPException` (the base class) and FastAPI's
    :class:`RequestValidationError` so 4xx/5xx responses share the same
    contract regardless of which layer surfaced them.
    """
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)


__all__ = [
    "PROBLEM_CONTENT_TYPE",
    "install_problem_details",
    "problem",
]
