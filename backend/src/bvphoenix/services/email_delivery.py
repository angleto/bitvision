"""Durable delivery ledger for outbound email.

Every message that leaves the process gets a row in ``email_deliveries``
*before* the first attempt, so a transport outage degrades to "late"
instead of "lost". The row records the discriminated ``error_code`` from
the transport, an operator-facing ``error_detail``, and the time of the
next attempt.

Design notes
------------

**The body is never persisted.** ``attempt`` rebuilds the MIME payload
from ``purpose`` plus the row's foreign keys via a registry of builders.
That keeps the ledger free of PHI and means a replayed share invitation
picks up the *current* state of its link rather than a stale snapshot.

**Retry lives here, not in the dispatcher.** The reminder dispatcher
used to retry the transport itself, requeueing without moving
``scheduled_at`` against a cron that fires every 5 minutes; its
four-attempt budget therefore burned out in a quarter of an hour and
left rows in ``failed`` with no code path back out. Backoff belongs on
the row, keyed to wall-clock time.

**Non-retriable failures do not consume the budget.** A refused
recipient or a rejected credential fails identically forever; retrying
it six times only delays the operator seeing it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models.email_delivery import EmailDelivery
from bvphoenix.services.email import (
    DeliveryOutcome,
    EmailMessage,
    send_email_sync,
)

logger = logging.getLogger(__name__)

# Backoff schedule by attempt number (1-indexed). Roughly 1m, 5m, 25m,
# 2h, 6h, 6h — about 14 hours of coverage across the default budget of
# six attempts, which comfortably spans an unattended overnight outage
# without hammering a relay that is refusing us.
_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 1500, 7200, 21600, 21600)

# Rebuilds the MIME payload for a queued row. Registered by the modules
# that own each purpose to avoid an import cycle (api.sharing imports
# this module, so this module cannot import api.sharing).
MessageBuilder = Callable[[AsyncSession, EmailDelivery], Awaitable[EmailMessage | None]]

_BUILDERS: dict[str, MessageBuilder] = {}


def register_builder(purpose: str, builder: MessageBuilder) -> None:
    """Bind a ``purpose`` to the coroutine that rebuilds its message.

    A builder returning ``None`` means "this message is no longer
    meaningful" (the share link was revoked, the account was deleted).
    That is a terminal, non-error outcome: the row goes to
    ``dead_letter`` rather than being retried forever.
    """
    _BUILDERS[purpose] = builder


def _backoff_for(attempt: int) -> timedelta:
    idx = min(max(attempt, 1), len(_BACKOFF_SECONDS)) - 1
    return timedelta(seconds=_BACKOFF_SECONDS[idx])


async def enqueue(
    db: AsyncSession,
    *,
    purpose: str,
    recipient_email: str,
    subject_line: str | None = None,
    locale: str = "it",
    share_link_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    notification_dispatch_id: uuid.UUID | None = None,
    max_attempts: int = 6,
) -> EmailDelivery:
    """Record the intent to deliver. Does not send.

    Flushes so the caller has an id to return, but does not commit: the
    row must land in the same transaction as whatever business change
    justified the email, or a rollback would leave an orphan promise.
    """
    row = EmailDelivery(
        purpose=purpose,
        recipient_email=recipient_email,
        subject_line=subject_line,
        locale=locale,
        share_link_id=share_link_id,
        subject_id=subject_id,
        notification_dispatch_id=notification_dispatch_id,
        status="queued",
        attempt_count=0,
        max_attempts=max_attempts,
        next_attempt_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def attempt(
    db: AsyncSession,
    delivery_id: uuid.UUID,
    *,
    message: EmailMessage | None = None,
) -> DeliveryOutcome:
    """Try to deliver one ledger row and record what happened.

    ``message`` may be supplied by a caller that has just built it (the
    synchronous request path), avoiding a redundant rebuild. Otherwise
    the registered builder for the row's purpose reconstructs it.

    The row is left in a terminal or a scheduled state; it is never left
    mid-flight. Returns the outcome so a request handler can answer
    truthfully in the same breath.
    """
    row = await db.get(EmailDelivery, delivery_id)
    if row is None:
        return DeliveryOutcome(
            ok=False, error_code="delivery_row_missing", error_detail=str(delivery_id)
        )
    if row.status == "sent":
        return DeliveryOutcome.success()

    if message is None:
        builder = _BUILDERS.get(row.purpose)
        if builder is None:
            row.status = "dead_letter"
            row.error_code = "no_builder_registered"
            row.error_detail = f"purpose={row.purpose}"
            await db.flush()
            return DeliveryOutcome(ok=False, error_code=row.error_code)
        message = await builder(db, row)
        if message is None:
            # The reason for the message evaporated. Not a failure.
            row.status = "dead_letter"
            row.error_code = "source_gone"
            await db.flush()
            return DeliveryOutcome(ok=False, error_code=row.error_code)

    row.attempt_count += 1
    # The transport blocks on a socket; keep it off the event loop.
    outcome = await _send_offloaded(message)

    if outcome.ok:
        row.status = "sent"
        row.sent_at = datetime.now(UTC)
        row.error_code = None
        row.error_detail = None
        await db.flush()
        return outcome

    row.error_code = outcome.error_code
    row.error_detail = outcome.error_detail
    if not outcome.retriable:
        # Deterministic refusal: burning the remaining budget on it only
        # delays the operator finding out.
        row.status = "failed"
    elif row.attempt_count >= row.max_attempts:
        row.status = "failed"
    else:
        row.status = "queued"
        row.next_attempt_at = datetime.now(UTC) + _backoff_for(row.attempt_count)
    await db.flush()
    logger.warning(
        "email delivery %s purpose=%s attempt=%d/%d -> %s (%s)",
        row.id,
        row.purpose,
        row.attempt_count,
        row.max_attempts,
        row.status,
        outcome.error_code,
    )
    return outcome


async def _send_offloaded(message: EmailMessage) -> DeliveryOutcome:
    """Run the blocking SMTP conversation off the event loop.

    Load-bearing: the backend image starts uvicorn without ``--workers``
    so there is exactly one event loop per pod, and it also serves
    ``/health``. Doing the connect inline meant a blackholed relay froze
    liveness for the whole timeout on every send.
    """
    return await asyncio.to_thread(send_email_sync, message)


async def claim_due(
    db: AsyncSession,
    *,
    limit: int = 50,
    now: datetime | None = None,
) -> list[EmailDelivery]:
    """Lock the next batch of due rows for this worker.

    ``FOR UPDATE SKIP LOCKED`` so concurrent worker replicas partition
    the backlog instead of colliding on it.
    """
    cutoff = now or datetime.now(UTC)
    stmt = (
        select(EmailDelivery)
        .where(EmailDelivery.status == "queued", EmailDelivery.next_attempt_at <= cutoff)
        .order_by(EmailDelivery.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await db.execute(stmt)).scalars().all())


async def requeue(
    db: AsyncSession,
    delivery_id: uuid.UUID,
    *,
    reset_attempts: bool = True,
) -> EmailDelivery | None:
    """Operator escape hatch out of ``failed`` / ``dead_letter``.

    The reminder dispatcher had no such path: rows that exhausted their
    budget were unreachable, and because the idempotency key carries no
    time component the dead row held the key forever, so the reminder
    could never be recreated either.
    """
    row = await db.get(EmailDelivery, delivery_id)
    if row is None or row.status == "sent":
        return row
    row.status = "queued"
    row.next_attempt_at = datetime.now(UTC)
    if reset_attempts:
        row.attempt_count = 0
    row.error_code = None
    row.error_detail = None
    await db.flush()
    return row


__all__ = [
    "attempt",
    "claim_due",
    "enqueue",
    "register_builder",
    "requeue",
]
