"""Short-lived single-use tokens for browser-native file downloads.

Why this exists. Single-file downloads (a PDF, a JPEG, a 4 GB DVD ISO)
should be plain ``<a href>`` clicks so the browser streams bytes
straight to disk — same UX as GitHub Releases, Google Drive single
files, every other web app. An anchor cannot carry an
``Authorization: Bearer`` header though, and the previous
``fetch + Blob`` workaround OOM'd browser tabs on multi-GB files
(Chrome caps Blobs at ~2 GiB).

Putting the persistent JWT in the URL would solve the technical
problem but leaks the long-lived credential into proxy logs, browser
history, and HTTP referrer headers. Instead the frontend asks for a
single-use, scope-bound token (5 min TTL, valid for exactly one
``(resource_kind, resource_id, child_id)`` triple) and appends it
as ``?dt=<token>`` on the download URL. The download endpoint
validates + atomically consumes the token via Redis ``GETDEL`` and
falls back to the normal Bearer path if the user is already
authenticated via the API.

The pattern mirrors S3 presigned URLs / Cloudfront signed URLs /
Stripe download links — short-lived, narrowly scoped, single-use —
without needing a third-party signer or coupling to S3-specific
features (works the same against MinIO / R2 / Scaleway / AWS).
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import User

logger = logging.getLogger(__name__)


_PREFIX = "dl:"
_DEFAULT_TTL_SECONDS = 300


async def issue_download_token(
    redis: Any,
    *,
    subject_id: uuid.UUID,
    resource_kind: str,
    resource_id: uuid.UUID,
    child_id: uuid.UUID | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> tuple[str, int]:
    """Mint a token bound to a specific resource and store it in Redis.

    The token is opaque (256 bits of randomness, URL-safe base64);
    the payload (subject + scope) lives only on the Redis side, so
    leaking the token from a log line is enough to attempt one
    download but not to derive anything about the user or the file.
    """
    token = secrets.token_urlsafe(32)
    payload = {
        "subject_id": str(subject_id),
        "resource_kind": resource_kind,
        "resource_id": str(resource_id),
        "child_id": str(child_id) if child_id else None,
    }
    await redis.set(_PREFIX + token, json.dumps(payload), ex=ttl_seconds)
    return token, ttl_seconds


async def _validate_token_payload(
    raw: str | bytes | None,
    *,
    resource_kind: str,
    resource_id: uuid.UUID,
    child_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Shared payload check for ``consume_*`` and ``peek_*``.

    Splits validation from the Redis read primitive (GETDEL vs GET)
    so the two flows below stay one-line each. Returns the
    payload's ``subject_id`` on success, ``None`` on any mismatch.
    """
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("download token payload was not valid JSON")
        return None
    if data.get("resource_kind") != resource_kind:
        return None
    if data.get("resource_id") != str(resource_id):
        return None
    expected_child = str(child_id) if child_id else None
    if (data.get("child_id") or None) != expected_child:
        return None
    try:
        return uuid.UUID(data["subject_id"])
    except (KeyError, TypeError, ValueError):
        return None


async def consume_download_token(
    redis: Any,
    token: str,
    *,
    resource_kind: str,
    resource_id: uuid.UUID,
    child_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Validate + atomically consume a download token.

    Returns the issuing subject's id on success; ``None`` on any
    failure (invalid / expired / scope mismatch / already consumed).
    Uses Redis ``GETDEL`` so the consume is atomic — two parallel
    requests with the same token cannot both succeed.

    Use this for one-shot capabilities (document / document_file /
    job_result downloads where the URL is anchor-clicked once).
    For multi-use within TTL (a recipient who legitimately wants to
    re-download the same prepared archive without re-entering the
    password every time), use :func:`peek_download_token`.
    """
    if not token:
        return None
    raw = await redis.getdel(_PREFIX + token)
    return await _validate_token_payload(
        raw,
        resource_kind=resource_kind,
        resource_id=resource_id,
        child_id=child_id,
    )


async def peek_download_token(
    redis: Any,
    token: str,
    *,
    resource_kind: str,
    resource_id: uuid.UUID,
    child_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Validate WITHOUT consuming. The token stays in Redis until its
    TTL expires (5 min by default), so the same dt allows N
    downloads within that window.

    Used by the share-link cached download endpoint: a recipient
    who entered the password once shouldn't have to re-verify for
    each retry / chunked-resume / "I forgot to save the file"
    cycle inside the next 5 minutes. Token leakage is bounded:
    only valid against a single share-link, only for 5 minutes,
    and only against this one resource.
    """
    if not token:
        return None
    raw = await redis.get(_PREFIX + token)
    return await _validate_token_payload(
        raw,
        resource_kind=resource_kind,
        resource_id=resource_id,
        child_id=child_id,
    )


async def resolve_download_user(
    *,
    user: User | None,
    dt: str | None,
    db: AsyncSession,
    resource_kind: str,
    resource_id: uuid.UUID,
    child_id: uuid.UUID | None = None,
) -> User:
    """Auth gate for download endpoints.

    Accepts two paths:

    * the standard ``Authorization: Bearer <jwt>`` already resolved
      by ``public_user`` upstream (passed in as ``user``);
    * ``?dt=<download_token>`` — a single-use scope-bound token
      issued by ``POST /api/auth/download-token``.

    Anchor-click downloads can't carry an Authorization header (the
    browser doesn't inject it on top-level navigation), so the
    fallback is the only practical way to authenticate native
    streaming-to-disk anchor downloads. Use ``public_user`` (not
    ``optional_user``) as the FastAPI dep that resolves ``user``,
    otherwise the global-auth gate raises 401 *before* the dt
    fallback gets a chance to run.

    Tokens are atomically consumed via Redis ``GETDEL`` so a leaked
    token can be replayed exactly zero times. The token's bound
    resource must match the URL exactly; a token issued for
    document A cannot be used to fetch document B.
    """
    if user is not None:
        return user
    if not dt:
        raise HTTPException(
            status_code=401,
            detail="authentication required (bearer token or ?dt=...)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Local imports keep this service decoupled from arq import order
    # in CLI helpers that only need the issuer.
    from arq import create_pool

    from bvphoenix.services.arq_redis import redis_settings

    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        subject_id = await consume_download_token(
            redis,
            dt,
            resource_kind=resource_kind,
            resource_id=resource_id,
            child_id=child_id,
        )
    finally:
        await redis.close()
    if subject_id is None:
        raise HTTPException(
            status_code=401,
            detail="invalid or expired download token",
        )
    resolved = (
        await db.execute(select(User).where(User.subject_id == subject_id))
    ).scalar_one_or_none()
    if resolved is None:
        raise HTTPException(status_code=401, detail="user not found")
    return resolved


__all__ = [
    "consume_download_token",
    "issue_download_token",
    "peek_download_token",
    "resolve_download_user",
]
