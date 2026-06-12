"""Inbound message intake + staging into the review queue.

Two stages, two trust levels:

* :func:`persist_raw_email` runs in the HTTP intake handler (called by
  the MTA adapter). It does the *minimum* with the untrusted bytes —
  header-only parse for dedup keys, sha-256, S3 put of the raw ``.eml``
  — and creates the ``inbound_emails`` row. No body decoding, no
  attachment extraction: a hostile payload costs the API nothing.
* :func:`stage_inbound_email` runs in the arq worker. Full MIME parse,
  attachment extraction to the staged S3 prefix, one ``InboxItem`` per
  message with the component manifest. Anything that goes wrong here
  fails one job, not the SMTP conversation.

Storage layout (raw bucket, both outside the canonical fascicolo
keyspace):

* raw message: ``inbound-email/{patient_id}/{email_id}.eml``
* staged components: ``_inbox/{patient_id}/{item_id}/{idx:04d}_{name}``
"""

from __future__ import annotations

import asyncio
import email.parser
import email.policy
import email.utils
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import InboundEmail, InboxItem, PatientInboxAddress
from bvphoenix.services.inbox.mime import parse_inbound_email
from bvphoenix.storage import get_s3_storage

logger = logging.getLogger(__name__)

RAW_EMAIL_PREFIX = "inbound-email"
STAGED_PREFIX = "_inbox"

_S3_SEGMENT_SANITISE = re.compile(r"[^A-Za-z0-9._-]+")


class InboundEmailError(ValueError):
    """Domain failure the intake endpoint maps onto a structured 4xx."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _safe_segment(name: str) -> str:
    return _S3_SEGMENT_SANITISE.sub("_", name)[:120] or "blob"


def raw_email_key(patient_id: uuid.UUID, email_id: uuid.UUID) -> str:
    return f"{RAW_EMAIL_PREFIX}/{patient_id}/{email_id}.eml"


def staged_prefix_for(patient_id: uuid.UUID, item_id: uuid.UUID) -> str:
    return f"{STAGED_PREFIX}/{patient_id}/{item_id}"


async def _rate_exceeded(db: AsyncSession, address: PatientInboxAddress) -> bool:
    settings = get_settings()
    window_start = datetime.now(UTC) - timedelta(hours=1)
    recent = (
        await db.execute(
            select(func.count())
            .select_from(InboundEmail)
            .where(
                InboundEmail.inbox_address_id == address.id,
                InboundEmail.created_at >= window_start,
            )
        )
    ).scalar_one()
    return recent >= settings.inbound_email_rate_per_hour


async def persist_raw_email(
    db: AsyncSession,
    *,
    address: PatientInboxAddress,
    raw: bytes,
    envelope_to: str | None = None,
) -> InboundEmail | None:
    """Accept one raw message for ``address``; returns the new row, or
    ``None`` on a dedup hit (idempotent accept — the MTA may retry a
    delivery whose 250 got lost).

    Raises :class:`InboundEmailError` on size / rate violations (the
    intake endpoint maps them so the MTA answers 552 / 451).
    """
    settings = get_settings()
    if len(raw) > settings.inbound_email_max_raw_bytes:
        raise InboundEmailError(
            "inbound.too_large",
            f"message of {len(raw)} bytes exceeds cap {settings.inbound_email_max_raw_bytes}",
        )
    if await _rate_exceeded(db, address):
        raise InboundEmailError(
            "inbound.rate_limited",
            "per-address ingestion rate exceeded; retry later",
        )

    # Header-only parse: BytesHeaderParser stops at the blank line, so
    # no payload is ever decoded on the intake path.
    headers = email.parser.BytesHeaderParser(policy=email.policy.default).parsebytes(raw)
    message_id = str(headers.get("Message-ID", "")).strip()[:998] or None
    raw_sha = hashlib.sha256(raw).hexdigest()

    # Dedup: Message-ID per address when present, raw hash otherwise.
    dedup = select(InboundEmail.id).where(InboundEmail.inbox_address_id == address.id)
    if message_id is not None:
        dedup = dedup.where(InboundEmail.message_id == message_id)
    else:
        dedup = dedup.where(InboundEmail.raw_sha256 == raw_sha)
    if (await db.execute(dedup.limit(1))).scalar_one_or_none() is not None:
        return None

    email_id = uuid.uuid4()
    key = raw_email_key(address.patient_id, email_id)
    storage = get_s3_storage()
    await asyncio.to_thread(storage.upload_bytes, raw, bucket=settings.s3_bucket_raw, key=key)

    from_header = headers.get("From")
    from_address: str | None = None
    if from_header:
        parsed = email.utils.getaddresses([str(from_header)])
        for _name, addr in parsed:
            if addr:
                from_address = addr.lower()[:320]
                break

    row = InboundEmail(
        id=email_id,
        patient_id=address.patient_id,
        inbox_address_id=address.id,
        message_id=message_id,
        from_address=from_address,
        to_address=(envelope_to or "").lower()[:320] or None,
        subject=str(headers.get("Subject", "")).strip()[:2000] or None,
        received_at=datetime.now(UTC),
        raw_s3_key=key,
        raw_sha256=raw_sha,
        raw_size_bytes=len(raw),
    )
    db.add(row)
    await db.flush()
    return row


async def stage_inbound_email(db: AsyncSession, *, inbound: InboundEmail) -> InboxItem:
    """Worker-side staging: full parse of the stored raw, components to
    S3, one reviewable ``InboxItem`` (the *lot*). Idempotent on the
    email: an item already referencing it is returned as-is."""
    existing = (
        await db.execute(select(InboxItem).where(InboxItem.inbound_email_id == inbound.id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    settings = get_settings()
    storage = get_s3_storage()
    raw = await asyncio.to_thread(
        storage.get_object_bytes, bucket=settings.s3_bucket_raw, key=inbound.raw_s3_key
    )
    parsed = parse_inbound_email(raw)

    # Backfill the row fields the header-only intake pass could not
    # compute (authentication results live scattered across headers).
    inbound.spf_result = parsed.spf_result
    inbound.dkim_result = parsed.dkim_result
    inbound.dmarc_result = parsed.dmarc_result
    # ``received_at`` is our intake stamp and stays authoritative; the
    # sender-claimed Date header only fills a missing value.
    if inbound.received_at is None and parsed.date is not None:
        inbound.received_at = parsed.date

    item_id = uuid.uuid4()
    prefix = staged_prefix_for(inbound.patient_id, item_id)
    components: list[dict] = []
    for idx, att in enumerate(parsed.attachments):
        key = f"{prefix}/{idx:04d}_{_safe_segment(att.filename)}"
        await asyncio.to_thread(
            storage.upload_bytes, att.payload, bucket=settings.s3_bucket_raw, key=key
        )
        components.append(
            {
                "name": att.filename,
                "s3_key": key,
                "size_bytes": len(att.payload),
                "content_type": att.content_type,
                "sha256": hashlib.sha256(att.payload).hexdigest(),
            }
        )

    item = InboxItem(
        id=item_id,
        patient_id=inbound.patient_id,
        inbound_email_id=inbound.id,
        source_channel="email",
        # Explicit client-side values for the server-defaulted mixin
        # columns: the worker reads them right after flush, and an
        # expired-attribute refresh inside an AsyncSession would raise.
        status="received",
        etag=uuid.uuid4(),
        staged_prefix=prefix,
        manifest={
            "version": 1,
            "components": components,
            "email": {
                "from": parsed.from_address or inbound.from_address,
                "subject": parsed.subject or inbound.subject,
                "spf": parsed.spf_result,
                "dkim": parsed.dkim_result,
                "dmarc": parsed.dmarc_result,
                "auto_submitted": parsed.is_auto_submitted,
            },
            # Preview only — the full body stays in the raw .eml; the
            # reviewer can promote it to a note deliberately, never
            # automatically.
            "body_preview": (parsed.body_text or "")[:500] or None,
        },
    )
    db.add(item)
    await db.flush()
    return item


async def purge_staged(item: InboxItem) -> int:
    """Delete the staged component blobs of a rejected/expired item.
    Best-effort (S3 deletes are idempotent); returns the count removed."""
    manifest = item.manifest or {}
    settings = get_settings()
    storage = get_s3_storage()
    removed = 0
    for comp in manifest.get("components", []):
        key = comp.get("s3_key")
        if not key or not key.startswith(f"{STAGED_PREFIX}/"):
            continue
        try:
            await asyncio.to_thread(storage.delete_object, bucket=settings.s3_bucket_raw, key=key)
            removed += 1
        except Exception:
            logger.warning("failed to purge staged blob %s", key, exc_info=True)
    return removed


__all__ = [
    "RAW_EMAIL_PREFIX",
    "STAGED_PREFIX",
    "InboundEmailError",
    "persist_raw_email",
    "purge_staged",
    "raw_email_key",
    "stage_inbound_email",
    "staged_prefix_for",
]
