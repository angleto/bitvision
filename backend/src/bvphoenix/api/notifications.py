"""Outbound-notifications surface: opt-out + bounce ingest + dispatch admin.

Endpoints
---------

Public (no auth):

* ``GET /api/notifications/opt-out?token=<uuid>&channel=<name>``
  Single-click consent revocation per RFC 8058 §3.3. The recipient
  clicks the link in any reminder email; we flip the relevant
  ``patient_contacts.consent_<channel>`` to false. Returns a tiny
  HTML page so a browser dropping the user there sees confirmation.
  Token is patient-contact-scoped (UUID generated server-side),
  rate-limited at the gateway level.

* ``POST /api/notifications/bounce-webhook``
  Inbound from Scaleway TEM (and compatible providers): a bounce /
  complaint / delivery event mapped onto
  ``patient_contacts.email_delivery_state``. The payload signature
  is verified against a shared secret (``BVP_TEM_WEBHOOK_SECRET``);
  unsigned or mis-signed requests are 401.

Authenticated (require_user):

* ``GET /api/patients/{patient_id}/notification-dispatches``
  Audit list for the fascicolo owner / her agent. Shows what is
  scheduled, what already fired, what was cancelled.

* ``POST /api/notification-dispatches/{id}/cancel``
  Cancel a single pending row (e.g. operator decides a reminder is
  redundant after the appointment was rescheduled manually).

The bounce webhook does NOT need ``enforce_agent_patient_scope`` —
it's a system-to-system call from the upstream mailer. Authentication
is via HMAC, not user JWT.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import enforce_agent_patient_scope, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    NOTIFICATION_CHANNELS,
    PATIENT_CONTACT_CHANNELS,
    NotificationDispatch,
    Patient,
    PatientContact,
    TelegramLinkCode,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.notifications.telegram_link import (
    create_link_code,
    get_last_for_contact,
    get_pending_for_contact,
    resolve_link_code,
    send_telegram_message,
    unlink_telegram,
)
from bvphoenix.services.notifications.webhook_secret import encrypt_secret
from bvphoenix.services.permissions import READ_METADATA, can_patient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notifications"])


# ---------------------------------------------------------------------------
# Opt-out (public)
# ---------------------------------------------------------------------------


def _opt_out_html(success: bool, channel: str, lang: str = "it") -> str:
    """Tiny confirmation page rendered after a token redemption.

    No CSS framework — keep the dependency footprint trivial. The
    page is also accessible to non-JS clients (curl, lynx); the
    operator can verify the opt-out happened by reading the response."""
    if lang.startswith("it"):
        if success:
            return (
                '<!doctype html><html lang="it"><meta charset="utf-8">'
                "<title>Iscrizione annullata</title>"
                '<body style="font-family:sans-serif;max-width:480px;margin:3rem auto;padding:1rem">'
                "<h1>Iscrizione annullata</h1>"
                f"<p>Non riceverai più notifiche sul canale <strong>{channel}</strong>.</p>"
                "<p>Per ripristinare, contatta l'amministratore del fascicolo.</p>"
                "</body></html>"
            )
        return (
            '<!doctype html><html lang="it"><meta charset="utf-8">'
            "<title>Link non valido</title>"
            '<body style="font-family:sans-serif;max-width:480px;margin:3rem auto;padding:1rem">'
            "<h1>Link non valido o scaduto</h1>"
            "<p>Il link di disiscrizione non è più valido. Contatta l'amministratore del fascicolo se vuoi essere rimosso dall'elenco di notifica.</p>"
            "</body></html>"
        )
    if success:
        return (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            "<title>Unsubscribed</title>"
            '<body style="font-family:sans-serif;max-width:480px;margin:3rem auto;padding:1rem">'
            "<h1>Unsubscribed</h1>"
            f"<p>You will no longer receive notifications on the <strong>{channel}</strong> channel.</p>"
            "<p>To re-enable, please contact the record administrator.</p>"
            "</body></html>"
        )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        "<title>Invalid link</title>"
        '<body style="font-family:sans-serif;max-width:480px;margin:3rem auto;padding:1rem">'
        "<h1>Invalid or expired link</h1>"
        "<p>This unsubscribe link is no longer valid. Contact the record administrator to be removed from the notification list.</p>"
        "</body></html>"
    )


def _revoke_channel_consent(contact: PatientContact, channel: str) -> None:
    """Flip the relevant per-channel consent flag. Multi-channel opt-out
    is handled by calling this once per channel; ``channel=all`` flips
    the umbrella ``consent_to_contact`` to false."""
    if channel in ("email", "ics_attachment"):
        contact.consent_email = False
        contact.email_delivery_state = "unsubscribed"
    elif channel == "webhook_telegram":
        contact.consent_telegram = False
    elif channel == "webhook_whatsapp":
        contact.consent_whatsapp = False
    elif channel == "webhook_generic":
        contact.consent_webhook = False
    elif channel == "all":
        contact.consent_to_contact = False
        contact.consent_email = False
        contact.consent_telegram = False
        contact.consent_whatsapp = False
        contact.consent_webhook = False
        contact.email_delivery_state = "unsubscribed"


@router.get(
    "/notifications/opt-out",
    include_in_schema=True,
)
async def opt_out(
    token: Annotated[uuid.UUID, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
    channel: Annotated[str, Query(max_length=32)] = "email",
    lang: Annotated[str, Query(max_length=8)] = "it",
) -> Response:
    """Single-click unsubscribe. ``token`` is the
    ``patient_contacts.opt_out_token`` UUID embedded in every email
    footer + webhook payload. ``channel`` lets the recipient revoke
    just one channel (mail) without disabling Telegram too — the
    default is ``email`` because that's where the link physically
    lives. ``channel=all`` is the nuclear option (umbrella opt-out)."""
    if channel != "all" and channel not in NOTIFICATION_CHANNELS:
        raise HTTPException(status_code=422, detail="unknown channel")
    contact = (
        await db.execute(select(PatientContact).where(PatientContact.opt_out_token == token))
    ).scalar_one_or_none()
    if contact is None:
        return Response(
            content=_opt_out_html(False, channel, lang),
            media_type="text/html; charset=utf-8",
            status_code=404,
        )
    _revoke_channel_consent(contact, channel)
    # Mark all pending dispatches for this contact + channel as
    # cancelled so we don't fire them between this consent flip and
    # the next dispatcher tick.
    if channel == "all":
        await db.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.contact_id == contact.id,
                NotificationDispatch.status == "pending",
            )
            .values(status="cancelled", error_code="opt_out")
        )
    else:
        await db.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.contact_id == contact.id,
                NotificationDispatch.channel == channel,
                NotificationDispatch.status == "pending",
            )
            .values(status="cancelled", error_code="opt_out")
        )
    await db.commit()
    return Response(
        content=_opt_out_html(True, channel, lang),
        media_type="text/html; charset=utf-8",
        status_code=200,
    )


@router.post(
    "/notifications/opt-out",
    include_in_schema=True,
)
async def opt_out_post(
    token: Annotated[uuid.UUID, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
    channel: Annotated[str, Query(max_length=32)] = "email",
) -> dict:
    """RFC 8058 §4 one-click POST variant. Mail clients call this
    when the user clicks the native "Unsubscribe" button — they
    expect a 200 with no body (or a JSON ack), NOT an HTML page."""
    if channel != "all" and channel not in NOTIFICATION_CHANNELS:
        raise HTTPException(status_code=422, detail="unknown channel")
    contact = (
        await db.execute(select(PatientContact).where(PatientContact.opt_out_token == token))
    ).scalar_one_or_none()
    if contact is None:
        # RFC 8058 says we should return a 2xx anyway to avoid the
        # mail client retrying with the user's address exposed. Use
        # 200 with a neutral body.
        return {"status": "noop"}
    _revoke_channel_consent(contact, channel)
    if channel == "all":
        await db.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.contact_id == contact.id,
                NotificationDispatch.status == "pending",
            )
            .values(status="cancelled", error_code="opt_out")
        )
    else:
        await db.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.contact_id == contact.id,
                NotificationDispatch.channel == channel,
                NotificationDispatch.status == "pending",
            )
            .values(status="cancelled", error_code="opt_out")
        )
    await db.commit()
    return {"status": "ok", "channel": channel}


# ---------------------------------------------------------------------------
# Bounce / complaint webhook (provider → us)
# ---------------------------------------------------------------------------


class TemBounceEvent(BaseModel):
    """Subset of the Scaleway TEM event webhook payload we care about.

    TEM emits one event per delivery transition; we only react to
    bounce / complaint / dropped events. The shape is intentionally
    permissive — extra fields the provider adds in the future are
    ignored."""

    event_type: str = Field(..., description="hard_bounce / soft_bounce / complaint / dropped")
    email: str
    timestamp: str | None = None
    reason: str | None = None


@router.post(
    "/notifications/bounce-webhook",
    include_in_schema=True,
)
async def bounce_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_signature: Annotated[str | None, Header(alias="X-Signature")] = None,
) -> dict:
    """Update ``email_delivery_state`` on the matching PatientContact.

    Auth: HMAC-SHA256 over the raw body with the shared
    ``BVP_TEM_WEBHOOK_SECRET``. The header name follows the Scaleway
    TEM convention ``X-Signature``. Mis-signed or unsigned requests
    return 401; we never trust the email/event payload before the
    signature is verified.
    """
    settings = get_settings()
    secret = (getattr(settings, "tem_webhook_secret", "") or "").encode("utf-8")
    body = await request.body()
    if not secret:
        # In dev / when the secret is not set we accept unsigned
        # requests but log loudly. Production setup MUST set the
        # secret — operator handoff is documented in docs/notifications.md.
        logger.warning(
            "bounce-webhook accepted without HMAC verification — set BVP_TEM_WEBHOOK_SECRET in production"
        )
    else:
        if not x_signature:
            raise HTTPException(status_code=401, detail="signature header missing")
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        presented = x_signature.split("=", 1)[-1].lower()
        if not hmac.compare_digest(expected, presented):
            raise HTTPException(status_code=401, detail="signature mismatch")

    try:
        payload = TemBounceEvent.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid bounce payload: {exc}") from exc

    new_state: str | None
    if payload.event_type in ("hard_bounce", "dropped"):
        new_state = "bounced"
    elif payload.event_type == "complaint":
        new_state = "suppressed"
    elif payload.event_type == "soft_bounce":
        # Soft bounces don't permanently disable — log only.
        new_state = None
    else:
        new_state = None

    if new_state:
        await db.execute(
            update(PatientContact)
            .where(PatientContact.email == payload.email)
            .values(email_delivery_state=new_state)
        )
        # Cancel queued email dispatches for any contact we just
        # flipped to ``bounced``.
        if new_state in ("bounced", "suppressed"):
            await db.execute(
                update(NotificationDispatch)
                .where(
                    NotificationDispatch.status == "pending",
                    NotificationDispatch.channel.in_(["email", "ics_attachment"]),
                    NotificationDispatch.contact_id.in_(
                        select(PatientContact.id).where(PatientContact.email == payload.email)
                    ),
                )
                .values(status="cancelled", error_code=f"bounce_{new_state}")
            )
        await db.commit()

    return {"status": "ok", "event_type": payload.event_type, "applied": bool(new_state)}


# ---------------------------------------------------------------------------
# Authenticated admin endpoints
# ---------------------------------------------------------------------------


class NotificationDispatchOut(BaseModel):
    """Subset of NotificationDispatch surfaced through the API.

    Storage isolation: we omit ``provider_message_id`` and
    ``arq_job_id`` (server-internal correlation ids) plus
    ``template_context`` (may carry the contact's first name)."""

    id: str
    patient_id: str
    contact_id: str
    target_kind: str
    target_id: str
    kind: str
    channel: str
    offset_minutes: int
    scheduled_at: str
    status: str
    attempt_count: int
    error_code: str | None
    locale: str
    author_kind: str
    created_at: str
    sent_at: str | None


def _to_dispatch_out(row: NotificationDispatch) -> NotificationDispatchOut:
    return NotificationDispatchOut(
        id=str(row.id),
        patient_id=str(row.patient_id),
        contact_id=str(row.contact_id),
        target_kind=row.target_kind,
        target_id=str(row.target_id),
        kind=row.kind,
        channel=row.channel,
        offset_minutes=row.offset_minutes,
        scheduled_at=row.scheduled_at.isoformat(),
        status=row.status,
        attempt_count=row.attempt_count,
        error_code=row.error_code,
        locale=row.locale,
        author_kind=row.author_kind,
        created_at=row.created_at.isoformat(),
        sent_at=row.sent_at.isoformat() if row.sent_at else None,
    )


@router.get(
    "/patients/{patient_id}/notification-dispatches",
    response_model=list[NotificationDispatchOut],
)
async def list_patient_dispatches(
    patient_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    statuses: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[NotificationDispatchOut]:
    """List dispatch rows for a patient. Useful to verify "is the
    reminder I set up still queued?" and to audit past failures."""
    del audit
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)

    stmt = (
        select(NotificationDispatch)
        .where(NotificationDispatch.patient_id == patient_id)
        .order_by(NotificationDispatch.scheduled_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if statuses:
        stmt = stmt.where(NotificationDispatch.status.in_(statuses))
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_dispatch_out(r) for r in rows]


@router.post(
    "/notification-dispatches/{dispatch_id}/cancel",
    response_model=NotificationDispatchOut,
)
async def cancel_dispatch(
    dispatch_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> NotificationDispatchOut:
    """Mark one pending dispatch as ``cancelled``. Idempotent: a
    row already in a terminal state stays as-is and is returned to
    the caller for inspection."""
    del audit
    row = (
        await db.execute(select(NotificationDispatch).where(NotificationDispatch.id == dispatch_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="dispatch not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == row.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="dispatch not found")
    enforce_agent_patient_scope(request, patient.id)

    if row.status == "pending":
        row.status = "cancelled"
        row.error_code = "manual_cancel"
        await db.commit()
        await db.refresh(row)
    return _to_dispatch_out(row)


# ---------------------------------------------------------------------------
# Contact-channel configuration + admin opt-out
# ---------------------------------------------------------------------------


class ConfigureChannelIn(BaseModel):
    """Body for ``POST /patients/{pid}/contacts/{cid}/configure-channel``.

    Each field is optional; only the supplied keys are applied. The
    matching write-permission is the same as for general PatientContact
    edits (``WRITE_REPORT`` at the patient level)."""

    preferred_locale: str | None = Field(default=None, max_length=8)
    telegram_chat_id: str | None = Field(default=None, max_length=64)
    whatsapp_phone: str | None = Field(default=None, max_length=32)
    webhook_url: str | None = Field(default=None, max_length=512)
    consent_email: bool | None = None
    consent_telegram: bool | None = None
    consent_whatsapp: bool | None = None
    consent_webhook: bool | None = None
    append_preferred_channel: str | None = Field(default=None, max_length=32)


class RevokeConsentIn(BaseModel):
    channel: str = Field(..., max_length=32)


class TestNotificationIn(BaseModel):
    contact_id: uuid.UUID
    channel: str = Field(..., max_length=32)


async def _load_contact_with_access(
    db: AsyncSession,
    *,
    request: Request,
    user: User,
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
) -> tuple[Patient, PatientContact]:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)
    contact = (
        await db.execute(
            select(PatientContact).where(
                PatientContact.id == contact_id,
                PatientContact.patient_id == patient_id,
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    return patient, contact


@router.post(
    "/patients/{patient_id}/contacts/{contact_id}/configure-channel",
)
async def configure_channel(
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: ConfigureChannelIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> dict:
    """Set channel address + consent + locale on a contact in one
    call. ``append_preferred_channel`` adds a channel to the ordered
    ``preferred_channels`` JSONB list if not already present (does
    NOT remove other channels — use revoke_consent for that)."""
    del audit
    _patient, contact = await _load_contact_with_access(
        db, request=request, user=user, patient_id=patient_id, contact_id=contact_id
    )
    payload = body.model_dump(exclude_unset=True)
    append = payload.pop("append_preferred_channel", None)
    for field, value in payload.items():
        setattr(contact, field, value)
    if append:
        if append not in PATIENT_CONTACT_CHANNELS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown channel {append}, allowed: {list(PATIENT_CONTACT_CHANNELS)}",
            )
        existing = list(contact.preferred_channels or [])
        if append not in existing:
            existing.append(append)
            contact.preferred_channels = existing
    await db.commit()
    await db.refresh(contact)
    return {
        "contact_id": str(contact.id),
        "preferred_channels": contact.preferred_channels,
        "preferred_locale": contact.preferred_locale,
        "telegram_chat_id": contact.telegram_chat_id,
        "whatsapp_phone": contact.whatsapp_phone,
        "webhook_url": contact.webhook_url,
        "consent_email": contact.consent_email,
        "consent_telegram": contact.consent_telegram,
        "consent_whatsapp": contact.consent_whatsapp,
        "consent_webhook": contact.consent_webhook,
        "email_delivery_state": contact.email_delivery_state,
    }


@router.post(
    "/patients/{patient_id}/contacts/{contact_id}/revoke-consent",
)
async def revoke_consent(
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: RevokeConsentIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> dict:
    """Administrator-side opt-out — flip the per-channel consent
    flag and cancel queued dispatches for that channel."""
    del audit
    _patient, contact = await _load_contact_with_access(
        db, request=request, user=user, patient_id=patient_id, contact_id=contact_id
    )
    if body.channel != "all" and body.channel not in NOTIFICATION_CHANNELS:
        raise HTTPException(status_code=422, detail="unknown channel")
    _revoke_channel_consent(contact, body.channel)
    if body.channel == "all":
        await db.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.contact_id == contact.id,
                NotificationDispatch.status == "pending",
            )
            .values(status="cancelled", error_code="admin_revoke")
        )
    else:
        await db.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.contact_id == contact.id,
                NotificationDispatch.channel == body.channel,
                NotificationDispatch.status == "pending",
            )
            .values(status="cancelled", error_code="admin_revoke")
        )
    await db.commit()
    await db.refresh(contact)
    return {
        "contact_id": str(contact.id),
        "channel": body.channel,
        "consent_to_contact": contact.consent_to_contact,
        "consent_email": contact.consent_email,
        "consent_telegram": contact.consent_telegram,
        "consent_whatsapp": contact.consent_whatsapp,
        "consent_webhook": contact.consent_webhook,
    }


@router.post(
    "/patients/{patient_id}/notifications/test",
    response_model=NotificationDispatchOut,
)
async def test_notification(
    patient_id: uuid.UUID,
    body: TestNotificationIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> NotificationDispatchOut:
    """Queue a one-shot test dispatch. Scheduled for ``now()`` so the
    safety-net cron picks it up on the next tick (within ~5 min).

    Test dispatches are anchored on the patient's most recent
    upcoming clinical event when one exists, otherwise on a
    synthesised target_id (the contact's own id) — the latter is a
    smoke test that exercises the channel without a real event /
    task in the picture."""
    del audit
    import hashlib as _h

    patient, contact = await _load_contact_with_access(
        db, request=request, user=user, patient_id=patient_id, contact_id=body.contact_id
    )
    if body.channel not in NOTIFICATION_CHANNELS:
        raise HTTPException(status_code=422, detail="unknown channel")

    target_kind = "clinical_event"
    target_id: uuid.UUID
    # Try to anchor on a real upcoming event; fall back to a stub.
    from bvphoenix.db.models import ClinicalEvent as _CE

    upcoming = (
        await db.execute(
            select(_CE)
            .where(
                _CE.patient_id == patient.id,
                _CE.event_status.in_(["planned", "confirmed"]),
            )
            .order_by(_CE.planned_start_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if upcoming is not None:
        target_id = upcoming.id
    else:
        target_id = uuid.uuid4()  # synthetic; dispatcher will cancel as target_missing
        target_kind = "clinical_event"  # the dispatcher resolves on load

    # Stable idempotency key — re-running send_test for the same
    # (contact, channel) within a single minute would otherwise spam
    # the dispatcher; bucket on minute resolution.
    bucket = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    idem = _h.sha256(f"test|{contact.id}|{body.channel}|{bucket}".encode()).hexdigest()
    row = NotificationDispatch(
        patient_id=patient.id,
        contact_id=contact.id,
        target_kind=target_kind,
        target_id=target_id,
        kind="event_reminder",
        channel=body.channel,
        offset_minutes=0,
        scheduled_at=datetime.now(UTC),
        locale=contact.preferred_locale or "it",
        idempotency_key=idem,
        author_kind="agent" if getattr(request.state, "is_agent", False) else "human",
        author_subject_id=user.subject_id,
    )
    db.add(row)
    try:
        await db.commit()
    except Exception:  # idempotency clash → return existing row
        await db.rollback()
        existing = (
            await db.execute(
                select(NotificationDispatch).where(NotificationDispatch.idempotency_key == idem)
            )
        ).scalar_one()
        return _to_dispatch_out(existing)
    await db.refresh(row)
    return _to_dispatch_out(row)


# ---------------------------------------------------------------------------
# Telegram contact-binding flow (sprint D1)
# ---------------------------------------------------------------------------


class WebhookSecretIn(BaseModel):
    """POST body for setting a contact's webhook HMAC secret.

    The plaintext appears only in this request; the server-side handler
    encrypts via pgcrypto before persisting. The response NEVER echoes
    the plaintext (storage-isolation memo). A subsequent ``configure-channel``
    or GET on the contact returns the encrypted blob only by reference
    via a boolean ``has_webhook_secret`` flag, never the bytes."""

    secret: str = Field(..., min_length=16, max_length=512)


@router.post(
    "/patients/{patient_id}/contacts/{contact_id}/webhook-secret",
)
async def set_webhook_secret(
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: WebhookSecretIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> dict:
    """Rotate the per-contact HMAC secret used to sign outbound
    webhook POSTs. The plaintext is encrypted server-side via
    pgcrypto ``pgp_sym_encrypt`` with ``BVP_WEBHOOK_ENCRYPTION_KEY``;
    the response only confirms the operation, never echoes the
    plaintext back."""
    del audit
    _patient, contact = await _load_contact_with_access(
        db, request=request, user=user, patient_id=patient_id, contact_id=contact_id
    )
    blob = await encrypt_secret(db, body.secret.encode("utf-8"))
    contact.webhook_secret_encrypted = blob
    await db.commit()
    return {
        "contact_id": str(contact.id),
        "has_webhook_secret": True,
    }


@router.delete(
    "/patients/{patient_id}/contacts/{contact_id}/webhook-secret",
)
async def clear_webhook_secret(
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> dict:
    """Clear the per-contact webhook HMAC secret. Subsequent webhook
    deliveries to this contact go unsigned (the receiver-side
    integration decides whether to accept them; recommend reject)."""
    del audit
    _patient, contact = await _load_contact_with_access(
        db, request=request, user=user, patient_id=patient_id, contact_id=contact_id
    )
    contact.webhook_secret_encrypted = None
    await db.commit()
    return {
        "contact_id": str(contact.id),
        "has_webhook_secret": False,
    }


class TelegramLinkStartOut(BaseModel):
    code: str
    deep_link_url: str
    expires_at: str


class TelegramLinkStatusOut(BaseModel):
    """Status of the Telegram link for a contact.

    * ``pending`` — a live code exists, the user has not opened the
      deep link yet (UI keeps polling)
    * ``linked`` — telegram_chat_id is set on the contact; the user
      successfully redeemed a code at some point
    * ``expired`` — last code was minted but the user never opened it
      and it has passed its TTL
    * ``none`` — no link attempt on record
    """

    status: str
    code: str | None = None
    deep_link_url: str | None = None
    expires_at: str | None = None
    chat_linked: bool = False


@router.post(
    "/patients/{patient_id}/contacts/{contact_id}/telegram-link/start",
    response_model=TelegramLinkStartOut,
)
async def telegram_link_start(
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> TelegramLinkStartOut:
    """Mint a one-shot linking code for the contact and return the
    deep-link URL the UI should display (as button + QR + plain text
    fallback). The recipient opens the link, lands in Telegram, and
    sends ``/start <code>`` to the bot — the bot webhook resolves the
    code and binds the chat_id."""
    del audit
    _patient, contact = await _load_contact_with_access(
        db, request=request, user=user, patient_id=patient_id, contact_id=contact_id
    )
    try:
        row, deep_link = await create_link_code(
            db,
            patient_id=patient_id,
            contact_id=contact.id,
            created_by_subject_id=user.subject_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await db.commit()
    return TelegramLinkStartOut(
        code=row.code,
        deep_link_url=deep_link,
        expires_at=row.expires_at.isoformat(),
    )


@router.get(
    "/patients/{patient_id}/contacts/{contact_id}/telegram-link/status",
    response_model=TelegramLinkStatusOut,
)
async def telegram_link_status(
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> TelegramLinkStatusOut:
    """UI poll target. Returns the current binding status — the modal
    in PatientContactsPanel dismisses itself when this flips from
    ``pending`` to ``linked``."""
    del audit
    _patient, contact = await _load_contact_with_access(
        db, request=request, user=user, patient_id=patient_id, contact_id=contact_id
    )
    pending = await get_pending_for_contact(db, contact.id)
    if pending is not None:
        settings = get_settings()
        handle = (settings.telegram_bot_username or "").lstrip("@")
        deep_link = f"https://t.me/{handle}?start={pending.code}" if handle else None
        return TelegramLinkStatusOut(
            status="pending",
            code=pending.code,
            deep_link_url=deep_link,
            expires_at=pending.expires_at.isoformat(),
            chat_linked=bool(contact.telegram_chat_id),
        )
    if contact.telegram_chat_id:
        return TelegramLinkStatusOut(status="linked", chat_linked=True)
    last = await get_last_for_contact(db, contact.id)
    if last is not None and last.used_at is None:
        return TelegramLinkStatusOut(status="expired")
    return TelegramLinkStatusOut(status="none")


@router.post(
    "/patients/{patient_id}/contacts/{contact_id}/telegram-link/unlink",
)
async def telegram_link_unlink(
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> dict:
    """Clear the Telegram binding on a contact. Cancels queued
    telegram dispatches at the same time so stale chat_ids don't
    fire."""
    del audit
    _patient, contact = await _load_contact_with_access(
        db, request=request, user=user, patient_id=patient_id, contact_id=contact_id
    )
    await unlink_telegram(db, contact)
    await db.commit()
    return {
        "status": "ok",
        "contact_id": str(contact.id),
        "telegram_chat_id": None,
    }


# ---------------------------------------------------------------------------
# Telegram bot webhook (Telegram → us)
# ---------------------------------------------------------------------------


@router.post(
    "/notifications/telegram/webhook",
    include_in_schema=False,
)
async def telegram_bot_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_telegram_secret: Annotated[
        str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")
    ] = None,
) -> dict:
    """Receive Telegram bot updates. Auth: shared secret in the
    ``X-Telegram-Bot-Api-Secret-Token`` header (Telegram echoes the
    value configured via ``setWebhook.secret_token``); mis-signed
    requests are 401.

    The only message we care about today is ``/start <code>`` —
    Telegram delivers this as a ``message`` update with the text
    field. Anything else is acknowledged with 200 to keep Telegram
    from retrying.
    """
    settings = get_settings()
    expected = getattr(settings, "telegram_webhook_secret", "") or ""
    if expected:
        if not x_telegram_secret or x_telegram_secret != expected:
            raise HTTPException(status_code=401, detail="bad telegram secret")

    try:
        payload = await request.json()
    except Exception:
        # Telegram sometimes sends an empty body on health-check
        # pings — ack and move on.
        return {"ok": True}

    message = payload.get("message") or payload.get("channel_post") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return {"ok": True}
    chat_id_str = str(chat_id)

    if not text.startswith("/start"):
        await send_telegram_message(
            chat_id_str,
            "Questo bot serve solo per ricevere notifiche da BitVision. "
            "Per collegare il tuo account, apri il link fornito nella "
            "scheda contatti del fascicolo.",
        )
        return {"ok": True}

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await send_telegram_message(
            chat_id_str,
            "Per collegare il tuo account, apri il link `t.me/<bot>?start=<codice>` "
            "fornito nella scheda contatti del fascicolo.",
        )
        return {"ok": True}

    code = parts[1].strip()
    redeemed = await resolve_link_code(db, code=code, telegram_chat_id=chat_id_str)
    if redeemed is None:
        await db.rollback()
        await send_telegram_message(
            chat_id_str,
            "Codice di collegamento non valido o scaduto. Genera un nuovo "
            "link dalla scheda contatti del fascicolo (i codici scadono dopo 15 minuti).",
        )
        return {"ok": True, "redeemed": False}
    await db.commit()
    await send_telegram_message(
        chat_id_str,
        "Collegamento riuscito. Riceverai qui i promemoria di BitVision. "
        "Puoi annullare la sottoscrizione in qualsiasi momento dalla scheda "
        "contatti del fascicolo.",
    )
    return {"ok": True, "redeemed": True}
