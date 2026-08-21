"""Notification dispatcher — picks up a pending row and ships the bytes.

Entry point: :func:`dispatch_notification(dispatch_id, db)`. Called
from the arq worker (``workers/tasks/dispatch_notification.py``) at
the scheduled time AND from the safety-net cron when a row sits past
its scheduled_at + grace.

Flow per dispatch:

1. Load the ``NotificationDispatch`` row. Exit fast if not pending
   (idempotency: rerunning a sent row is a no-op).
2. Load the contact + verify consent + delivery state. Cancel the
   row when consent was revoked between scheduling and firing.
3. Resolve the source artefact (clinical_event / patient_task) to
   build the template context. Cancel the row when the source was
   deleted in the meantime.
4. Render templates (subject + body_text + body_html). PII allowlist
   enforced by ``templates_engine.render``.
5. Build the optional ICS attachment when the kind benefits from one
   (event_reminder anchors on a calendared event; task_reminder
   anchors on a due_at-bearing task).
6. Hand the payload to the right notifier.
7. Persist the outcome on the dispatch row: ``status``,
   ``attempt_count``, ``provider_message_id`` (server-side only),
   ``error_code``, ``sent_at``.

The dispatcher never re-raises: a failed delivery becomes a ``failed``
or ``dead_letter`` row, never a worker crash.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    ClinicalEvent,
    NotificationDispatch,
    Patient,
    PatientContact,
    PatientTask,
)
from bvphoenix.services.calendar_ics import (
    render_single_event_ics,
    render_single_task_ics,
)
from bvphoenix.services.notifications.base import (
    NOTIFIER_REGISTRY,
    NotificationChannel,
    NotificationKind,
    NotificationPayload,
    NotificationResult,
    NotificationTargetKind,
    Notifier,
)
from bvphoenix.services.notifications.email_notifier import EmailNotifier
from bvphoenix.services.notifications.ics_attachment_notifier import (
    ICSAttachmentNotifier,
)
from bvphoenix.services.notifications.templates_engine import render
from bvphoenix.services.notifications.webhook_notifier import (
    GenericWebhookNotifier,
    TelegramNotifier,
    WhatsAppNotifier,
)
from bvphoenix.services.notifications.webhook_secret import decrypt_secret

logger = logging.getLogger(__name__)


# Cap on retries before a row goes to dead_letter. Matches the arq
# job retry budget (3) + the safety-net cron's one re-fire, so a
# transient receiver outage is forgiven up to ~4 attempts before we
# stop trying.
MAX_RETRIES = 4


def _ensure_notifier_registry() -> None:
    """Lazy registry population. Safe to call multiple times."""
    if NOTIFIER_REGISTRY:
        return
    for notifier_cls in (
        EmailNotifier,
        ICSAttachmentNotifier,
        GenericWebhookNotifier,
        TelegramNotifier,
        WhatsAppNotifier,
    ):
        inst: Notifier = notifier_cls()
        NOTIFIER_REGISTRY[inst.channel] = inst


def _opt_out_url(token: uuid.UUID, channel: str) -> str:
    settings = get_settings()
    base = settings.notifications_opt_out_base_url or settings.public_frontend_url
    return f"{base.rstrip('/')}/api/notifications/opt-out?token={token}&channel={channel}"


def _format_when_local(dt: datetime | None, locale: str, tz_hint: str | None) -> str:
    """Best-effort human formatting of a datetime in the recipient's
    locale. Falls back to ISO when the timezone is unknown."""
    if dt is None:
        return ""
    try:
        from zoneinfo import ZoneInfo

        local = dt.astimezone(ZoneInfo(tz_hint)) if tz_hint else dt
    except Exception:
        local = dt
    # Compact "15 giu 2026, 09:00" / "Jun 15, 2026 9:00 AM"
    if locale.startswith("it"):
        months = [
            "gen",
            "feb",
            "mar",
            "apr",
            "mag",
            "giu",
            "lug",
            "ago",
            "set",
            "ott",
            "nov",
            "dic",
        ]
        return f"{local.day} {months[local.month - 1]} {local.year}, {local.strftime('%H:%M')}"
    return local.strftime("%b %d, %Y %H:%M")


async def _load_dispatch(db: AsyncSession, dispatch_id: uuid.UUID) -> NotificationDispatch | None:
    return (
        await db.execute(select(NotificationDispatch).where(NotificationDispatch.id == dispatch_id))
    ).scalar_one_or_none()


async def _load_contact(
    db: AsyncSession, contact_id: uuid.UUID, patient_id: uuid.UUID
) -> PatientContact | None:
    row = (
        await db.execute(select(PatientContact).where(PatientContact.id == contact_id))
    ).scalar_one_or_none()
    if row is None or row.patient_id != patient_id:
        return None
    return row


def _channel_address(contact: PatientContact, channel: NotificationChannel) -> str:
    if channel in (NotificationChannel.EMAIL, NotificationChannel.ICS_ATTACHMENT):
        return contact.email or ""
    if channel == NotificationChannel.WEBHOOK_GENERIC:
        return contact.webhook_url or ""
    if channel == NotificationChannel.WEBHOOK_TELEGRAM:
        return contact.telegram_chat_id or ""
    if channel == NotificationChannel.WEBHOOK_WHATSAPP:
        return contact.whatsapp_phone or ""
    return ""


def _channel_consent(contact: PatientContact, channel: NotificationChannel) -> bool:
    if not contact.consent_to_contact:
        return False
    if channel in (NotificationChannel.EMAIL, NotificationChannel.ICS_ATTACHMENT):
        return contact.consent_email and contact.email_delivery_state == "active"
    if channel == NotificationChannel.WEBHOOK_TELEGRAM:
        return contact.consent_telegram
    if channel == NotificationChannel.WEBHOOK_WHATSAPP:
        return contact.consent_whatsapp
    if channel == NotificationChannel.WEBHOOK_GENERIC:
        return contact.consent_webhook
    return False


def _contact_first_name(contact: PatientContact) -> str | None:
    """Pick the first whitespace-separated token from the contact
    label. Keeps the salutation human without leaking the surname."""
    if not contact.label:
        return None
    head = contact.label.strip().split()
    return head[0] if head else None


async def _load_event_anchor(db: AsyncSession, event_id: uuid.UUID) -> ClinicalEvent | None:
    return (
        await db.execute(select(ClinicalEvent).where(ClinicalEvent.id == event_id))
    ).scalar_one_or_none()


async def _load_task_anchor(db: AsyncSession, task_id: uuid.UUID) -> PatientTask | None:
    return (
        await db.execute(
            select(PatientTask).where(PatientTask.id == task_id, PatientTask.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def _build_payload(
    db: AsyncSession,
    dispatch: NotificationDispatch,
    contact: PatientContact,
) -> NotificationPayload | None:
    """Render templates + assemble the NotificationPayload. Returns
    None when the source artefact is missing (caller should cancel
    the dispatch row)."""
    settings = get_settings()
    channel = NotificationChannel(dispatch.channel)
    kind = NotificationKind(dispatch.kind)
    target_kind = NotificationTargetKind(dispatch.target_kind)

    # Resolve source
    event_title = ""
    event_when_local = ""
    event_where = ""
    event_meeting_url = ""
    app_url = ""
    ics_blob: bytes | None = None
    ics_filename: str | None = None

    if target_kind == NotificationTargetKind.CLINICAL_EVENT:
        ev = await _load_event_anchor(db, dispatch.target_id)
        if ev is None:
            return None
        event_title = ev.title
        anchor_dt = ev.planned_start_at or ev.actual_start_at
        event_when_local = _format_when_local(anchor_dt, dispatch.locale, ev.timezone)
        if ev.location_struct:
            event_where = " ".join(
                str(v)
                for v in (
                    ev.location_struct.get("facility"),
                    ev.location_struct.get("city"),
                )
                if v
            )
        event_meeting_url = ev.meeting_url or ""
        ics_text = render_single_event_ics(ev, lang=dispatch.locale, with_valarm=True)
        ics_blob = ics_text.encode("utf-8")
        ics_filename = f"bitvision-event-{ev.id}.ics"
        app_url = f"{settings.public_frontend_url.rstrip('/')}/patients/{ev.patient_id}?view=events"
    elif target_kind == NotificationTargetKind.PATIENT_TASK:
        task = await _load_task_anchor(db, dispatch.target_id)
        if task is None:
            return None
        event_title = task.title
        anchor_dt = task.due_at or task.completed_at
        event_when_local = _format_when_local(anchor_dt, dispatch.locale, task.timezone)
        if task.due_at or task.completed_at:
            ics_text = render_single_task_ics(task, lang=dispatch.locale, with_valarm=True)
            ics_blob = ics_text.encode("utf-8")
            ics_filename = f"bitvision-task-{task.id}.ics"
        app_url = (
            f"{settings.public_frontend_url.rstrip('/')}/patients/{task.patient_id}?view=tasks"
        )

    opt_out = _opt_out_url(contact.opt_out_token, dispatch.channel)
    # Resolve the per-contact webhook secret for the generic webhook
    # channel. Other channels don't need it; we only pay the pgcrypto
    # cost when ``channel='webhook_generic'`` and the contact has a
    # blob set. Decryption failures (key rotated, blob corrupted) are
    # silently treated as "no secret" → the notifier sends an
    # unsigned POST. Receiver-side integrations should reject those.
    webhook_secret_bytes: bytes | None = None
    if channel == NotificationChannel.WEBHOOK_GENERIC and contact.webhook_secret_encrypted:
        webhook_secret_bytes = await decrypt_secret(db, contact.webhook_secret_encrypted)

    context = {
        "patient_first_name": _contact_first_name(contact),
        "event_title": event_title,
        "event_when_local": event_when_local,
        "event_where": event_where,
        "event_meeting_url": event_meeting_url,
        "app_url": app_url,
        "opt_out_url": opt_out,
    }
    # Drop empty optional keys before allowlist check so the template
    # has them as Falsey defaults (Jinja's ``{% if %}`` does the right
    # thing). The allowlist still rejects keys not on the canonical
    # list — we never leak past the boundary by accident.
    context = {k: v for k, v in context.items() if v not in (None, "")}
    # opt_out_url is mandatory; restore it if filtered above.
    context["opt_out_url"] = opt_out

    subject, body_text, body_html = render(
        kind=dispatch.kind, locale=dispatch.locale, context=context
    )
    address = _channel_address(contact, channel)
    return NotificationPayload(
        patient_id=dispatch.patient_id,
        contact_id=dispatch.contact_id,
        target_kind=target_kind,
        target_id=dispatch.target_id,
        channel=channel,
        kind=kind,
        locale=dispatch.locale,
        scheduled_at=dispatch.scheduled_at,
        offset_minutes=dispatch.offset_minutes,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        ics_blob=ics_blob,
        ics_filename=ics_filename,
        channel_address=address,
        opt_out_url=opt_out,
        author_kind=dispatch.author_kind,
        author_subject_id=dispatch.author_subject_id,
        idempotency_key=dispatch.idempotency_key,
        webhook_secret=webhook_secret_bytes,
    )


def _record_outcome(
    dispatch: NotificationDispatch,
    result: NotificationResult,
) -> str:
    """Mutate the dispatch row's status / error fields. Returns the
    final status string for logging."""
    dispatch.attempt_count = (dispatch.attempt_count or 0) + 1
    if result.success:
        dispatch.status = "sent"
        dispatch.sent_at = datetime.now(UTC)
        dispatch.provider_message_id = result.provider_message_id
        dispatch.error_code = None
        dispatch.error_detail = None
        return "sent"
    # ``error_detail`` has been on NotificationResult all along and is
    # populated in five places by the webhook notifier, but no column
    # received it, so every failure collapsed to an opaque code. It is
    # operator-only and never echoed on the public API.
    dispatch.error_detail = result.error_detail
    if result.retriable and dispatch.attempt_count < MAX_RETRIES:
        dispatch.status = "pending"
        dispatch.error_code = result.error_code
        return "pending_retry"
    dispatch.status = "dead_letter" if not result.retriable else "failed"
    dispatch.error_code = result.error_code
    return dispatch.status


async def dispatch_notification(db: AsyncSession, dispatch_id: uuid.UUID) -> str:
    """Top-level entry. Returns a short status string for the caller
    (worker / cron) to log. Never raises."""
    _ensure_notifier_registry()
    settings = get_settings()
    if not settings.notifications_enabled:
        return "disabled"

    dispatch = await _load_dispatch(db, dispatch_id)
    if dispatch is None:
        return "missing"
    if dispatch.status != "pending":
        # Idempotency: a row already sent / cancelled / dead-lettered
        # is a no-op.
        return f"noop_{dispatch.status}"

    contact = await _load_contact(db, dispatch.contact_id, dispatch.patient_id)
    if contact is None:
        dispatch.status = "cancelled"
        dispatch.error_code = "contact_missing"
        await db.commit()
        return "cancelled_contact_missing"

    channel = NotificationChannel(dispatch.channel)
    if not _channel_consent(contact, channel):
        dispatch.status = "cancelled"
        dispatch.error_code = "consent_revoked"
        await db.commit()
        return "cancelled_consent"

    # Ensure the patient still exists (CASCADE delete would normally
    # have removed the dispatch row, but a soft-deleted patient could
    # in principle stay).
    patient = (
        await db.execute(select(Patient).where(Patient.id == dispatch.patient_id))
    ).scalar_one_or_none()
    if patient is None:
        dispatch.status = "cancelled"
        dispatch.error_code = "patient_missing"
        await db.commit()
        return "cancelled_patient_missing"

    payload = await _build_payload(db, dispatch, contact)
    if payload is None:
        dispatch.status = "cancelled"
        dispatch.error_code = "target_missing"
        await db.commit()
        return "cancelled_target_missing"

    notifier = NOTIFIER_REGISTRY.get(channel)
    if notifier is None or not notifier.supports(payload):
        dispatch.status = "dead_letter"
        dispatch.error_code = "channel_unsupported"
        await db.commit()
        return "dead_letter_channel"

    try:
        result = await notifier.send(payload)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("notifier crash on dispatch=%s", dispatch.id)
        result = NotificationResult(
            success=False,
            error_code="notifier_crashed",
            error_detail=str(exc)[:512],
            retriable=True,
        )

    outcome = _record_outcome(dispatch, result)
    await db.commit()
    return outcome


__all__ = ["MAX_RETRIES", "dispatch_notification"]
