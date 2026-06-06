"""Shared httpx client for calling the bitvision phoenix backend API."""

from __future__ import annotations

import json as _json

import httpx

from bvmcp.config import Settings, get_settings


def get_auth_header(settings: Settings) -> dict[str, str]:
    """Return the Authorization header for backend calls.

    Token resolution order:

    1. The bearer of the in-flight HTTP MCP request (from
       :func:`bvmcp.server_http.current_principal`) — set when the
       remote MCP transport is active. The bearer is the assistant's
       ``client_secret``; phoenix backend resolves the same secret via
       the agent-secret hash path in ``auth/deps.py``, so we just
       forward it verbatim.
    2. ``settings.agent_token`` — short-lived patient-scoped token
       used by the stdio transport.
    3. ``settings.user_token`` — long-lived user token, fallback for
       the stdio transport in dev.

    Empty dict means anonymous (dev only).
    """
    # Lazy import to avoid a circular dependency: ``server_http`` itself
    # imports the tool modules.
    try:
        from bvmcp.server_http import current_principal

        principal = current_principal()
    except Exception:  # pragma: no cover - defensive
        principal = None
    if principal is not None and principal.raw_jwt:
        return {"Authorization": f"Bearer {principal.raw_jwt}"}
    token = settings.agent_token or settings.user_token
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _get_headers() -> dict[str, str]:
    settings = get_settings()
    headers: dict[str, str] = {"accept": "application/json"}
    headers.update(get_auth_header(settings))
    return headers


async def api_get(path: str, params: dict | None = None) -> dict | list:
    settings = get_settings()
    url = f"{settings.backend_base_url}{path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=_get_headers(), params=params)
        resp.raise_for_status()
        return resp.json()


async def api_post(path: str, json: dict | None = None) -> dict | list:
    settings = get_settings()
    url = f"{settings.backend_base_url}{path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=_get_headers(), json=json)
        resp.raise_for_status()
        return resp.json()


async def api_get_bytes(path: str, params: dict | None = None) -> tuple[bytes, str]:
    """GET a binary resource. Returns ``(content, content_type)``.

    Auth headers mirror :func:`api_get`; ``accept`` is left unset so the
    backend chooses a content type (e.g. ``image/jpeg`` for thumbnails).
    """
    settings = get_settings()
    url = f"{settings.backend_base_url}{path}"
    headers = _get_headers()
    headers.pop("accept", None)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "application/octet-stream")


async def api_post_raw(path: str, json: dict | None = None) -> httpx.Response:
    """POST returning the raw Response so callers can inspect non-2xx status
    codes (e.g. surface 400 validation errors to the LLM instead of raising)."""
    settings = get_settings()
    url = f"{settings.backend_base_url}{path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.post(url, headers=_get_headers(), json=json)


def _augment_headers(
    *,
    if_match: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = _get_headers()
    if if_match:
        # Strong ETag values are quoted per RFC 9110. The phoenix
        # ``parse_if_match`` helper is forgiving but we send the canonical
        # form so callers can copy/paste the ETag straight from a GET
        # response.
        headers["If-Match"] = (
            if_match if if_match.startswith('"') or if_match.startswith("W/") else f'"{if_match}"'
        )
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def api_patch(
    path: str,
    json: dict | None = None,
    *,
    params: dict | None = None,
    if_match: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict | list, dict[str, str]]:
    """PATCH a resource; returns ``(json_body, response_headers)``.

    ``response_headers`` carries the ``ETag`` post-write so the caller
    can use it as the ``If-Match`` for the next mutation without a
    re-fetch.
    """
    settings = get_settings()
    url = f"{settings.backend_base_url}{path}"
    headers = _augment_headers(if_match=if_match, idempotency_key=idempotency_key)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.patch(url, headers=headers, json=json, params=params)
        resp.raise_for_status()
        return resp.json(), dict(resp.headers)


async def api_patch_bytes(path: str, body: bytes, *, upload_offset: int) -> dict | list:
    """PATCH raw bytes with an ``Upload-Offset`` header (resumable upload chunk).

    The backend reads the raw request body + Upload-Offset (no multipart). Used
    by the MCP upload-session chunk tool so an agent can stream a (small)
    document end-to-end without the GUI. Returns the FileStateOut JSON.
    """
    settings = get_settings()
    url = f"{settings.backend_base_url}{path}"
    headers = _get_headers()
    headers["content-type"] = "application/octet-stream"
    headers["Upload-Offset"] = str(upload_offset)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.patch(url, headers=headers, content=body)
        resp.raise_for_status()
        return resp.json()


async def api_post_with_headers(
    path: str,
    json: dict | None = None,
    *,
    params: dict | None = None,
    idempotency_key: str | None = None,
    if_match: str | None = None,
) -> tuple[dict | list, dict[str, str]]:
    settings = get_settings()
    url = f"{settings.backend_base_url}{path}"
    headers = _augment_headers(if_match=if_match, idempotency_key=idempotency_key)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json=json, params=params)
        resp.raise_for_status()
        return resp.json(), dict(resp.headers)


async def api_put(
    path: str,
    json: dict | None = None,
    *,
    if_match: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict | list, dict[str, str]]:
    """PUT a resource; returns ``(json_body, response_headers)``.

    Used for idempotent upserts (e.g. assigning an event to a care
    phase) where POST would imply a fresh row each call.
    """
    settings = get_settings()
    url = f"{settings.backend_base_url}{path}"
    headers = _augment_headers(if_match=if_match, idempotency_key=idempotency_key)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.put(url, headers=headers, json=json)
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return body, dict(resp.headers)


def format_http_error(exc: httpx.HTTPStatusError, *, hint: str = "") -> str:
    """Render a non-2xx backend response as a JSON string the LLM can read.

    ``raise_for_status()`` collapses 4xx into a one-line ``HTTPStatusError``
    that loses the response body. Tool handlers should catch the exception
    and feed it through this helper so the caller sees the structured
    ``detail`` (Pydantic ``loc``/``msg``/``type``, RFC 7807 problem, raw
    text fallback) instead of just ``"400 Client Error"``.
    """
    resp = exc.response
    status = resp.status_code
    method = exc.request.method
    path = exc.request.url.path
    body: object
    try:
        body = resp.json()
    except ValueError:
        body = resp.text or None
    payload = {
        "error": "backend_error",
        "http_status": status,
        "request": f"{method} {path}",
        "detail": body,
    }
    if hint:
        payload["hint"] = hint
    return _json.dumps(payload, indent=2, ensure_ascii=False)


async def api_delete(
    path: str,
    *,
    params: dict | None = None,
    if_match: str | None = None,
) -> int:
    """DELETE returning the response status code (typically 204)."""
    settings = get_settings()
    url = f"{settings.backend_base_url}{path}"
    headers = _augment_headers(if_match=if_match)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.status_code
