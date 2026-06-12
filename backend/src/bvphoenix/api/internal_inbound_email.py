"""Internal intake endpoints for the inbound-email MTA adapter.

The MTA (``bvphoenix-mta``) is a deliberately dumb SMTP→HTTP adapter
with **no S3/DB credentials** (storage isolation): it validates each
``RCPT TO`` here, and at end-of-DATA forwards the raw message +
envelope. All persistence happens on this side.

Trust model: the endpoints live under ``/api/internal/*`` — stripped at
the Traefik ingress, reachable only on the in-cluster ClusterIP — and
additionally require the ``X-Inbound-Key`` shared secret
(``BVP_INBOUND_INTERNAL_SECRET``). The secret is distinct from
``BVP_INTERNAL_API_KEY`` on least-privilege grounds: the MTA terminates
port 25 on the open Internet, and its credential must unlock the mail
intake and nothing else.

Status mapping for the MTA (it speaks SMTP to the sender):

* 200 validate-rcpt        → 250 (recipient ok)
* 404 validate-rcpt        → 550 (unknown/revoked code; identical on
  purpose — a revoked capability must not confirm it once existed)
* 200/201 inbound-email    → 250 (accepted; 200 = duplicate, still 250)
* 413 / 429                → 552 / 451
* 5xx or unreachable       → 451 (sender retries; mail is never lost
  silently)
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from arq import create_pool
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.session import get_db
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.inbox.addresses import resolve_active_code
from bvphoenix.services.inbox.codes import split_local_part
from bvphoenix.services.inbox.emails import InboundEmailError, persist_raw_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/inbound-email", tags=["internal"])


def _check_inbound_auth(provided: str | None) -> None:
    settings = get_settings()
    if not settings.inbound_email_enabled or not settings.inbound_internal_secret:
        # Fail closed: a deployment without the feature (or without a
        # secret) must refuse mail rather than accept it half-wired.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inbound email disabled",
        )
    if not provided or not secrets.compare_digest(provided, settings.inbound_internal_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid inbound key",
        )


class ValidateRcptIn(BaseModel):
    # The full local part as received in the envelope (``code+tag``).
    local_part: str = Field(min_length=1, max_length=255)


class ValidateRcptOut(BaseModel):
    accept: bool


@router.post("/validate-rcpt", response_model=ValidateRcptOut)
async def validate_rcpt(
    body: ValidateRcptIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_inbound_key: Annotated[str | None, Header(alias="X-Inbound-Key")] = None,
) -> ValidateRcptOut:
    """RCPT-time routing check: 200 when the local part resolves to an
    active address, 404 otherwise. No detail beyond accept/refuse — the
    response is relayed to an untrusted sender."""
    _check_inbound_auth(x_inbound_key)
    settings = get_settings()
    split = split_local_part(body.local_part)
    if split is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown recipient")
    code, tag = split
    if tag != settings.inbound_email_tag:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown recipient")
    address = await resolve_active_code(db, code)
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown recipient")
    return ValidateRcptOut(accept=True)


class InboundEmailOut(BaseModel):
    accepted: bool
    duplicate: bool = False


@router.post("", response_model=InboundEmailOut, status_code=status.HTTP_201_CREATED)
async def receive_inbound_email(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_inbound_key: Annotated[str | None, Header(alias="X-Inbound-Key")] = None,
    x_envelope_rcpt: Annotated[str | None, Header(alias="X-Envelope-Rcpt")] = None,
) -> InboundEmailOut:
    """End-of-DATA intake: raw RFC 5322 bytes in the body, the envelope
    recipient (the full address) in ``X-Envelope-Rcpt``. Persists raw +
    row and enqueues the staging worker; duplicates are 200/no-op so an
    MTA retry after a lost 250 stays idempotent."""
    _check_inbound_auth(x_inbound_key)
    settings = get_settings()

    rcpt = (x_envelope_rcpt or "").strip()
    local_part = rcpt.split("@", 1)[0] if "@" in rcpt else rcpt
    split = split_local_part(local_part)
    if split is None or split[1] != settings.inbound_email_tag:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown recipient")
    address = await resolve_active_code(db, split[0])
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown recipient")

    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty message")

    try:
        row = await persist_raw_email(db, address=address, raw=raw, envelope_to=rcpt)
    except InboundEmailError as exc:
        status_code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if exc.code == "inbound.too_large"
            else status.HTTP_429_TOO_MANY_REQUESTS
        )
        raise HTTPException(
            status_code=status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc

    if row is None:
        # Dedup hit: already stored (and already queued for staging).
        return InboundEmailOut(accepted=True, duplicate=True)

    await db.commit()

    # Staging runs in the worker (full MIME parse never happens on the
    # API). An enqueue failure after the commit is recovered by the
    # worker's maintenance sweep over unstaged inbound_emails.
    try:
        redis = await create_pool(redis_settings(settings.redis_url))
        try:
            await redis.enqueue_job(
                "process_inbound_email",
                str(row.id),
                _job_id=f"inbound-email:{row.id}",
            )
        finally:
            await redis.close()
    except Exception:
        logger.exception("failed to enqueue process_inbound_email for %s", row.id)

    return InboundEmailOut(accepted=True)


__all__ = ["router"]
