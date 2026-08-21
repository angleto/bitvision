"""Materialise notification_dispatches rows from event / task writes.

Called from the api/clinical_events.py + api/patient_tasks.py write
paths after the source row is committed. Idempotent: each
``(target, contact, offset, channel)`` combination has a deterministic
``idempotency_key`` and the unique index on the column turns a re-fire
into a clean no-op. Since migration 0047 that index is PARTIAL
(``WHERE status <> 'cancelled'``): a cancelled reminder releases its
key, so cancel-then-rebuild, which is what every re-scheduling path
does, can insert instead of being silently swallowed.

Channels picked for a contact: intersection of
``contact.preferred_channels`` AND the per-channel consent flags AND
the per-channel feature flag in Settings. A contact opted out of
email but who has a working Telegram chat_id + telegram consent gets
the Telegram channel; a contact with no working channel gets no
dispatch (silent — the timeline UI still shows the underlying event,
the notification path just doesn't fire).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    ClinicalEvent,
    NotificationDispatch,
    PatientContact,
    PatientTask,
)

logger = logging.getLogger(__name__)

# Channels we ever auto-pick from a contact's preferred_channels.
# WhatsApp stays auto-skipped until ops flips the feature flag (the
# dispatcher will refuse to send anyway, but materialising rows that
# can never fire would clutter the dispatch table).
_AUTO_CHANNELS_BY_FEATURE: dict[str, str] = {
    "email": "notifications_email_enabled",
    "ics_attachment": "notifications_email_enabled",
    "webhook_generic": "notifications_webhook_enabled",
    "webhook_telegram": "notifications_telegram_enabled",
    "webhook_whatsapp": "notifications_whatsapp_enabled",
}


def _idempotency_key(
    target_id: uuid.UUID,
    contact_id: uuid.UUID,
    offset_minutes: int,
    channel: str,
    anchor: datetime,
) -> str:
    """Deterministic per-tuple key. Same inputs → same key, so the
    ON CONFLICT DO NOTHING in the bulk insert dedups re-runs after
    transient failures.

    ``anchor`` is part of the hash because a reminder is identified by
    WHEN it fires, not only by who/which channel. Without it, moving an
    appointment produced the same key as the reminder that was just
    cancelled for the old slot, so the fresh row was dropped by the
    ON CONFLICT and the patient got no reminder at all. With the anchor
    in the key, a moved appointment is a different reminder; a true
    re-run of the same schedule still dedups.
    """
    raw = f"{target_id}|{contact_id}|{offset_minutes}|{channel}|{anchor.isoformat()}".encode()
    return hashlib.sha256(raw).hexdigest()


def _parse_default_offsets(raw: str) -> list[int]:
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


def _normalise_offsets(offsets: Sequence[int] | None) -> list[int]:
    settings = get_settings()
    raw = (
        list(offsets) if offsets else _parse_default_offsets(settings.notifications_default_offsets)
    )
    seen: set[int] = set()
    out: list[int] = []
    for v in raw:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= 5:
            break
    return out


def _pick_channels(contact: PatientContact) -> list[str]:
    """Channels the dispatcher will attempt for this contact, in
    preferred order. Filtered by per-channel consent AND the global
    feature flag."""
    settings = get_settings()
    raw = contact.preferred_channels or []
    out: list[str] = []
    for ch in raw:
        if not isinstance(ch, str):
            continue
        flag_attr = _AUTO_CHANNELS_BY_FEATURE.get(ch)
        if flag_attr is None:
            continue
        if not getattr(settings, flag_attr, False):
            continue
        # Per-channel consent. Email and ICS share the email consent
        # (ICS goes through email transport).
        if ch in ("email", "ics_attachment"):
            if not contact.consent_email:
                continue
            if contact.email_delivery_state != "active":
                continue
            if not contact.email:
                continue
        elif ch == "webhook_telegram":
            if not contact.consent_telegram or not contact.telegram_chat_id:
                continue
        elif ch == "webhook_whatsapp":
            if not contact.consent_whatsapp or not contact.whatsapp_phone:
                continue
        elif ch == "webhook_generic":
            if not contact.consent_webhook or not contact.webhook_url:
                continue
        if not contact.consent_to_contact:
            # Umbrella opt-out short-circuits everything.
            continue
        out.append(ch)
    return out


def _author_kind_from_event(event: ClinicalEvent) -> str:
    return event.status_changed_by_kind or "system"


def _author_kind_from_task(task: PatientTask) -> str:
    return task.author_kind or "system"


async def _load_contacts_for_patient(
    db: AsyncSession, patient_id: uuid.UUID
) -> list[PatientContact]:
    rows = (
        (await db.execute(select(PatientContact).where(PatientContact.patient_id == patient_id)))
        .scalars()
        .all()
    )
    return list(rows)


async def materialise_event_dispatches(db: AsyncSession, event: ClinicalEvent) -> int:
    """Schedule notification_dispatches rows for a clinical event.

    Called after the event lands or is patched in a way that affects
    timing (``planned_start_at`` change, status flip to
    planned/confirmed). Returns the number of newly inserted rows
    (existing rows with matching idempotency_key are no-ops).
    """
    settings = get_settings()
    if not settings.notifications_enabled:
        return 0
    if event.event_status not in ("planned", "confirmed"):
        return 0
    anchor = event.planned_start_at
    if anchor is None:
        return 0
    offsets = _normalise_offsets(event.reminder_offsets_minutes)
    if not offsets:
        return 0
    contacts = await _load_contacts_for_patient(db, event.patient_id)
    if not contacts:
        return 0
    rows: list[dict] = []
    for contact in contacts:
        channels = _pick_channels(contact)
        for offset in offsets:
            scheduled = anchor + timedelta(minutes=offset)
            for channel in channels:
                rows.append(
                    {
                        "patient_id": event.patient_id,
                        "contact_id": contact.id,
                        "target_kind": "clinical_event",
                        "target_id": event.id,
                        "kind": "event_reminder",
                        "channel": channel,
                        "offset_minutes": offset,
                        "scheduled_at": scheduled,
                        "locale": contact.preferred_locale or "it",
                        "idempotency_key": _idempotency_key(
                            event.id, contact.id, offset, channel, anchor
                        ),
                        "author_kind": _author_kind_from_event(event),
                    }
                )
    if not rows:
        return 0
    stmt = (
        pg_insert(NotificationDispatch)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["idempotency_key"],
            # Mirrors the partial UNIQUE index of migration 0047: a
            # cancelled row no longer owns the key, so cancel-then-rebuild
            # can insert. Naming the arbiter rather than using a bare
            # DO NOTHING keeps an unexpected conflict (a PK collision,
            # say) loud instead of silently dropping the row. Safe across
            # the rollout window: an index with no predicate is inferable
            # under any ``index_where``, so this also plans correctly
            # against the pre-0047 global UNIQUE.
            index_where=text("status <> 'cancelled'"),
        )
        .returning(NotificationDispatch.id)
    )
    res = await db.execute(stmt)
    inserted = list(res.scalars().all())
    return len(inserted)


async def materialise_task_dispatches(db: AsyncSession, task: PatientTask) -> int:
    """Schedule notification_dispatches rows for a patient task.

    Called after the task lands or is patched in a way that affects
    its anchor (``due_at`` change). Tasks without ``due_at`` produce
    no dispatches — the user gets to know about the task on the UI
    but no reminder fires.
    """
    settings = get_settings()
    if not settings.notifications_enabled:
        return 0
    if task.status in ("done", "dropped"):
        return 0
    anchor = task.due_at
    if anchor is None:
        return 0
    offsets = _normalise_offsets(task.reminder_offsets_minutes)
    if not offsets:
        return 0
    contacts = await _load_contacts_for_patient(db, task.patient_id)
    if not contacts:
        return 0
    rows: list[dict] = []
    for contact in contacts:
        channels = _pick_channels(contact)
        for offset in offsets:
            scheduled = anchor + timedelta(minutes=offset)
            for channel in channels:
                rows.append(
                    {
                        "patient_id": task.patient_id,
                        "contact_id": contact.id,
                        "target_kind": "patient_task",
                        "target_id": task.id,
                        "kind": "task_reminder",
                        "channel": channel,
                        "offset_minutes": offset,
                        "scheduled_at": scheduled,
                        "locale": contact.preferred_locale or "it",
                        "idempotency_key": _idempotency_key(
                            task.id, contact.id, offset, channel, anchor
                        ),
                        "author_kind": _author_kind_from_task(task),
                    }
                )
    if not rows:
        return 0
    stmt = (
        pg_insert(NotificationDispatch)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["idempotency_key"],
            # Mirrors the partial UNIQUE index of migration 0047: a
            # cancelled row no longer owns the key, so cancel-then-rebuild
            # can insert. Naming the arbiter rather than using a bare
            # DO NOTHING keeps an unexpected conflict (a PK collision,
            # say) loud instead of silently dropping the row. Safe across
            # the rollout window: an index with no predicate is inferable
            # under any ``index_where``, so this also plans correctly
            # against the pre-0047 global UNIQUE.
            index_where=text("status <> 'cancelled'"),
        )
        .returning(NotificationDispatch.id)
    )
    res = await db.execute(stmt)
    inserted = list(res.scalars().all())
    return len(inserted)


async def cancel_dispatches_for_target(
    db: AsyncSession,
    target_kind: str,
    target_id: uuid.UUID,
    *,
    reason: str = "target_changed",
) -> int:
    """Mark all pending dispatches for a target as ``cancelled``.

    Called when a task / event is deleted or its anchor changes in a
    way that invalidates all queued reminders (rescheduled, dropped,
    cancelled). Returns the number of rows updated.
    """
    from sqlalchemy import update

    stmt = (
        update(NotificationDispatch)
        .where(
            NotificationDispatch.target_kind == target_kind,
            NotificationDispatch.target_id == target_id,
            NotificationDispatch.status == "pending",
        )
        .values(status="cancelled", error_code=reason)
        .returning(NotificationDispatch.id)
    )
    res = await db.execute(stmt)
    return len(list(res.scalars().all()))


__all__ = [
    "cancel_dispatches_for_target",
    "materialise_event_dispatches",
    "materialise_task_dispatches",
]
