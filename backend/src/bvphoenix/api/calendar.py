"""Calendar feed endpoint.

``GET /api/patients/{patient_id}/calendar`` is the per-patient
read-only projection used by the calendar UI (day/week/month/agenda
views) and by the ICS subscription form.

Two response shapes selected via ``?format=`` (default ``json``):

- ``json``: ``CalendarFeedOut`` with a flat ``occurrences`` array
  (newest-first by anchor timestamp) plus per-status counts.
- ``ics``: RFC 5545 iCalendar text rendered by
  ``services.calendar_ics.render_ics``. This route stays behind
  ``require_user``. For an external calendar app to subscribe without
  a BitVision login, mint a public handle via
  ``POST /patients/{pid}/calendar/subscriptions`` and hand out the
  resulting ``/api/calendar/feed/{token}.ics`` URL — that is the only
  anonymous-allowed calendar surface (HMAC-signed, revocable; see the
  subscription section at the bottom of this module).

Filtering: ``from`` / ``to`` are DATE (inclusive). ``statuses`` is a
multi-value query (``?statuses=planned&statuses=confirmed``) with
server-side whitelist. ``kinds`` mirrors the existing
``ClinicalEvent.kind`` enum.

Cross-patient impossibility: the route lives under
``/patients/{patient_id}/...`` and every query is bound to
``patient_id`` server-side, matching the project-wide guardrail
(see memory ``cross_patient_links_forbidden``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import (
    enforce_agent_patient_scope,
    enforce_agent_scope,
    public_user,
    require_user,
)
from bvphoenix.db.models import (
    CLINICAL_EVENT_KINDS,
    CLINICAL_EVENT_STATUSES,
    CalendarSubscription,
    ClinicalEvent,
    Patient,
    PatientTask,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.middleware.idempotency import IdempotencyContext, require_idempotency_key
from bvphoenix.services.agent_context import AgentContext
from bvphoenix.services.calendar_ics import (
    render_ics,
    render_single_event_ics,
    render_single_task_ics,
)
from bvphoenix.services.calendar_subscription_token import sign as sign_subscription_token
from bvphoenix.services.calendar_subscription_token import verify as verify_subscription_token
from bvphoenix.services.permissions import READ_METADATA, can_patient

from ._dry_run import dry_run_flag

router = APIRouter(tags=["calendar"])


class CalendarOccurrenceOut(BaseModel):
    """Single occurrence on the feed. For non-recurring events this
    is a 1:1 with the underlying ``clinical_events`` row; recurrence
    expansion (RRULE) is deferred to step 3 polish — there are no
    recurring events in production data yet."""

    event_id: str
    kind: str
    title: str
    event_status: str
    occurrence_dt_start: str | None  # ISO 8601 with tz
    occurrence_dt_end: str | None
    timezone: str | None
    location_struct: dict | None
    parent_event_id: str | None
    etag: str


class CalendarFeedOut(BaseModel):
    """Wire shape for ``format=json``. Includes summary counts so the
    UI can render a sidebar without an extra round-trip."""

    patient_id: str
    range_from: date | None
    range_to: date | None
    timezone: str
    occurrences: list[CalendarOccurrenceOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    generated_at: str


def _anchor_dt(ev: ClinicalEvent) -> datetime | None:
    """Best-effort anchor for ordering/range checks.

    Picks ``planned_start_at`` for upcoming events, ``actual_start_at``
    for completed/missed, falling back to ``event_date`` interpreted
    as midnight UTC when the timestamp columns are unset.
    """
    if ev.event_status in ("planned", "confirmed", "rescheduled"):
        return ev.planned_start_at
    if ev.event_status in ("completed", "missed"):
        return ev.actual_start_at or (
            datetime.combine(ev.event_date, datetime.min.time()) if ev.event_date else None
        )
    if ev.planned_start_at:
        return ev.planned_start_at
    if ev.event_date:
        return datetime.combine(ev.event_date, datetime.min.time())
    return None


def _to_occurrence(ev: ClinicalEvent) -> CalendarOccurrenceOut:
    if ev.event_status in ("planned", "confirmed", "rescheduled") and ev.planned_start_at:
        start, end = ev.planned_start_at, ev.planned_end_at
    elif ev.event_status in ("completed", "missed") and ev.actual_start_at:
        start, end = ev.actual_start_at, ev.actual_end_at
    else:
        start, end = ev.planned_start_at, ev.planned_end_at
    return CalendarOccurrenceOut(
        event_id=str(ev.id),
        kind=ev.kind,
        title=ev.title,
        event_status=ev.event_status,
        occurrence_dt_start=start.isoformat() if start else None,
        occurrence_dt_end=end.isoformat() if end else None,
        timezone=ev.timezone,
        location_struct=ev.location_struct,
        parent_event_id=str(ev.parent_event_id) if ev.parent_event_id else None,
        etag=str(ev.etag),
    )


async def _load_calendar_events(
    db: AsyncSession,
    patient_id: uuid.UUID,
    *,
    statuses: list[str] | None = None,
    kinds: list[str] | None = None,
    from_: date | None = None,
    to_: date | None = None,
) -> list[ClinicalEvent]:
    """Single source of truth for the calendar event query.

    Shared by the auth-required feed (``patient_calendar``) and the
    public subscription feed so the two surfaces can never drift on
    filtering, ordering, or the per-patient binding. Every query is
    bound to ``patient_id`` server-side — there is no code path that
    accepts a cross-patient predicate."""
    stmt = select(ClinicalEvent).where(ClinicalEvent.patient_id == patient_id)
    if statuses:
        stmt = stmt.where(ClinicalEvent.event_status.in_(statuses))
    if kinds:
        stmt = stmt.where(ClinicalEvent.kind.in_(kinds))
    # Range filter against the derived event_date (always populated
    # by the migration 0098 trigger). Inclusive on both ends.
    if from_ is not None:
        stmt = stmt.where(ClinicalEvent.event_date >= from_)
    if to_ is not None:
        stmt = stmt.where(ClinicalEvent.event_date <= to_)
    stmt = stmt.order_by(ClinicalEvent.event_date.desc().nulls_last(), ClinicalEvent.id)
    return list((await db.execute(stmt)).scalars().all())


@router.get(
    "/patients/{patient_id}/calendar",
    response_model=None,  # we return either JSON or text/calendar
)
async def patient_calendar(
    patient_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to_: Annotated[date | None, Query(alias="to")] = None,
    statuses: Annotated[list[str] | None, Query()] = None,
    kinds: Annotated[list[str] | None, Query()] = None,
    format_: Annotated[Literal["json", "ics"], Query(alias="format")] = "json",
    tz: Annotated[str | None, Query(max_length=64)] = None,
) -> CalendarFeedOut | Response:
    """Return the calendar feed for ``patient_id`` between ``from``
    (inclusive) and ``to`` (inclusive). The default range is "all
    events"; pass explicit dates to narrow."""
    del audit
    del request
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")

    if statuses is not None:
        invalid = [s for s in statuses if s not in CLINICAL_EVENT_STATUSES]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"invalid status(es) {invalid}, allowed: {list(CLINICAL_EVENT_STATUSES)}",
            )
    if kinds is not None:
        invalid_k = [k for k in kinds if k not in CLINICAL_EVENT_KINDS]
        if invalid_k:
            raise HTTPException(
                status_code=422,
                detail=f"invalid kind(s) {invalid_k}, allowed: {list(CLINICAL_EVENT_KINDS)}",
            )

    events = await _load_calendar_events(
        db, patient_id, statuses=statuses, kinds=kinds, from_=from_, to_=to_
    )

    if format_ == "ics":
        # Feed subscriptions poll repeatedly; emitting VALARM here
        # would re-arm the recipient's reminders on every poll, which
        # produces duplicate notifications. The single-event endpoints
        # below carry VALARMs because they're imported once.
        body = render_ics(
            events,
            lang="it" if not tz or "Europe" in (tz or "") else "en",
            with_valarm=False,
        )
        return Response(
            content=body,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="bitvision-{patient_id}.ics"',
                "Cache-Control": "no-store",
            },
        )

    # JSON: sort planned-first (upcoming on top), then by event_date desc.
    def _sort_key(e: ClinicalEvent):
        dt = _anchor_dt(e)
        return (
            0 if e.event_status in ("planned", "confirmed") else 1,
            -(dt.timestamp() if dt else 0),
        )

    events.sort(key=_sort_key)
    counts: dict[str, int] = {}
    for e in events:
        counts[e.event_status] = counts.get(e.event_status, 0) + 1
    from datetime import timezone as _tz

    return CalendarFeedOut(
        patient_id=str(patient_id),
        range_from=from_,
        range_to=to_,
        timezone=tz or "UTC",
        occurrences=[_to_occurrence(e) for e in events],
        counts=counts,
        generated_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Single-resource ICS exports (v3.4, sprint B)
# ---------------------------------------------------------------------------
#
# These endpoints emit one .ics file containing exactly one VEVENT,
# with VALARM blocks pre-populated from ``reminder_offsets_minutes``.
# Audience: a one-shot download / email attachment that the recipient
# imports once into their calendar. Local notifications then fire on
# the recipient's device without any server-side push from BitVision.
#
# Distinct from the feed endpoint above: the feed is polled, so
# VALARMs would re-arm on every poll (the spec says clients must
# treat repeated imports as updates, and apps differ wildly on how
# they de-duplicate alarms). Single-shot exports avoid that loop.


@router.get(
    "/clinical-events/{event_id}/calendar.ics",
    response_model=None,
)
async def export_event_ics(
    event_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    lang: Annotated[str, Query(max_length=8)] = "it",
    with_valarm: Annotated[bool, Query()] = True,
) -> Response:
    """Export a single clinical event as an iCalendar file.

    ``with_valarm`` defaults to ``True`` here (opposite of the feed):
    a single-shot export is imported once, so embedding the alarms is
    a net win for the recipient.
    """
    del audit
    ev = (
        await db.execute(select(ClinicalEvent).where(ClinicalEvent.id == event_id))
    ).scalar_one_or_none()
    if ev is None:
        raise HTTPException(status_code=404, detail="clinical event not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == ev.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="clinical event not found")
    enforce_agent_patient_scope(request, patient.id)

    body = render_single_event_ics(ev, lang=lang, with_valarm=with_valarm)
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="bitvision-event-{event_id}.ics"',
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/patient-tasks/{task_id}/calendar.ics",
    response_model=None,
)
async def export_task_ics(
    task_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    lang: Annotated[str, Query(max_length=8)] = "it",
    with_valarm: Annotated[bool, Query()] = True,
) -> Response:
    """Export a single patient task as an iCalendar file.

    Tasks without ``due_at`` (and without ``completed_at`` for done
    rows) cannot be placed on a calendar; we refuse with 422 rather
    than ship an empty .ics file that calendar apps drop silently.
    """
    del audit
    task = (
        await db.execute(select(PatientTask).where(PatientTask.id == task_id))
    ).scalar_one_or_none()
    if task is None or task.deleted_at is not None:
        raise HTTPException(status_code=404, detail="patient task not found")
    patient = (
        await db.execute(select(Patient).where(Patient.id == task.patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient task not found")
    enforce_agent_patient_scope(request, patient.id)

    anchor = task.due_at or (task.completed_at if task.status == "done" else None)
    if anchor is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "task_has_no_anchor",
                "message": (
                    "task has neither due_at nor completed_at; cannot be placed on a calendar"
                ),
            },
        )

    body = render_single_task_ics(task, lang=lang, with_valarm=with_valarm)
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="bitvision-task-{task_id}.ics"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Public iCal subscription handles (v3.6)
# ---------------------------------------------------------------------------
#
# A subscription is a revocable, non-expiring handle whose URL carries
# an HMAC token (``services.calendar_subscription_token``) binding
# (subscription_id, patient_id). The feed endpoint is the ONLY public
# (anonymous-allowed) calendar surface; everything else stays behind
# ``require_user``.
#
# Why DB-backed + HMAC and not a bare stateless token: a public,
# non-expiring URL to a patient's clinical calendar must be revocable
# on its own (rotating the global secret would nuke every link) and
# must leave a usage trail. The HMAC half makes the URL untamperable
# and cross-patient inexpressible; the row half makes it revocable and
# auditable. Each half covers what the other cannot.

_SUBSCRIPTION_FEED_PREFIX = "/api/calendar/feed/"


class CalendarSubscriptionCreateIn(BaseModel):
    """Create body. ``label`` is a free-text note the owner sees in the
    settings list (e.g. "Caregiver — sister"), never exposed in the
    feed itself."""

    label: str | None = Field(default=None, max_length=255)


class CalendarSubscriptionOut(BaseModel):
    """Wire shape. ``feed_path`` is the canonical relative URL the token
    lives in; ``feed_url`` is the same resolved against the request's
    public origin for copy/paste convenience. The token is never
    returned as a bare field — it only exists embedded in the path, so
    a leaked listing response is no more sensitive than the URL itself
    (which the owner is about to share anyway)."""

    id: str
    patient_id: str
    label: str | None
    author_kind: str
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    last_accessed_at: str | None
    access_count: int
    feed_path: str
    feed_url: str


def _subscription_out(row: CalendarSubscription, request: Request) -> CalendarSubscriptionOut:
    token = sign_subscription_token(row.id, row.patient_id)
    feed_path = f"{_SUBSCRIPTION_FEED_PREFIX}{token}.ics"
    base = str(request.base_url).rstrip("/")
    return CalendarSubscriptionOut(
        id=str(row.id),
        patient_id=str(row.patient_id),
        label=row.label,
        author_kind=row.author_kind,
        created_at=row.created_at.isoformat(),
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
        last_accessed_at=row.last_accessed_at.isoformat() if row.last_accessed_at else None,
        access_count=row.access_count,
        feed_path=feed_path,
        feed_url=f"{base}{feed_path}",
    )


async def _load_patient_or_404(db: AsyncSession, patient_id: uuid.UUID, user: User) -> Patient:
    """Resolve a patient the caller may read, or 404 (never 403, so the
    existence of a fascicolo is not leaked). Mirrors the gate used by
    the calendar feed itself."""
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    return patient


@router.post(
    "/patients/{patient_id}/calendar/subscriptions",
    response_model=None,
)
async def create_calendar_subscription(
    patient_id: uuid.UUID,
    body: CalendarSubscriptionCreateIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    idem: Annotated[IdempotencyContext, Depends(require_idempotency_key)],
    dry_run: Annotated[bool, Depends(dry_run_flag)] = False,
):
    """Mint a public, revocable iCal subscription URL for this patient.

    Anyone who can read the patient's calendar can mint one (a feed
    grants no data the creator could not already export). The link is
    non-expiring by design (calendar apps stop syncing silently when a
    feed 404/410s) and is killed via DELETE. Requires ``Idempotency-Key``
    and supports ``?dry_run=true`` (returns the would-be row, persists
    nothing, no audit)."""
    if idem.replay is not None:
        return idem.replay
    patient = await _load_patient_or_404(db, patient_id, user)
    enforce_agent_patient_scope(request, patient.id)
    enforce_agent_scope(request, "calendar:subscribe")

    author_kind = AgentContext.from_request(request).author_kind

    if dry_run:
        preview = CalendarSubscription(
            id=uuid.uuid4(),
            patient_id=patient.id,
            label=body.label,
            author_kind=author_kind,
            created_at=datetime.now(UTC),
            access_count=0,
        )
        return idem.capture(_subscription_out(preview, request).model_dump(), status_code=200)

    row = CalendarSubscription(
        patient_id=patient.id,
        label=body.label,
        author_kind=author_kind,
        created_by_subject_id=user.subject_id if author_kind == "human" else None,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    await audit.log(
        action="calendar_subscription.create",
        actor_subject_id=user.subject_id,
        resource_kind="calendar_subscription",
        resource_id=row.id,
        metadata={
            "patient_id": str(patient.id),
            "label": body.label,
            "author_kind": author_kind,
        },
    )
    await db.commit()
    await db.refresh(row)
    return idem.capture(_subscription_out(row, request).model_dump(), status_code=201)


@router.get(
    "/patients/{patient_id}/calendar/subscriptions",
    response_model=list[CalendarSubscriptionOut],
)
async def list_calendar_subscriptions(
    patient_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_revoked: Annotated[bool, Query()] = False,
) -> list[CalendarSubscriptionOut]:
    """List the subscription handles for a patient (active by default;
    pass ``include_revoked=true`` to also see killed ones for audit)."""
    patient = await _load_patient_or_404(db, patient_id, user)
    enforce_agent_patient_scope(request, patient.id)
    enforce_agent_scope(request, "calendar:read")
    stmt = select(CalendarSubscription).where(CalendarSubscription.patient_id == patient.id)
    if not include_revoked:
        stmt = stmt.where(CalendarSubscription.revoked_at.is_(None))
    stmt = stmt.order_by(CalendarSubscription.created_at.desc())
    rows = list((await db.execute(stmt)).scalars().all())
    return [_subscription_out(r, request) for r in rows]


@router.delete(
    "/patients/{patient_id}/calendar/subscriptions/{subscription_id}",
    response_model=None,
)
async def revoke_calendar_subscription(
    patient_id: uuid.UUID,
    subscription_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    purge: Annotated[bool, Query()] = False,
) -> Response:
    """Revoke a subscription. Soft by default (``revoked_at`` set, the
    token immediately 403s but the row survives for audit);
    ``?purge=true`` hard-deletes it. Idempotent: revoking an
    already-revoked / absent handle still returns 204 so a retry never
    errors."""
    patient = await _load_patient_or_404(db, patient_id, user)
    enforce_agent_patient_scope(request, patient.id)
    enforce_agent_scope(request, "calendar:subscribe")
    row = (
        await db.execute(
            select(CalendarSubscription).where(
                CalendarSubscription.id == subscription_id,
                CalendarSubscription.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return Response(status_code=204)

    if purge:
        await db.delete(row)
        action = "calendar_subscription.purge"
    else:
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            row.revoked_by_subject_id = user.subject_id
        action = "calendar_subscription.revoke"
    await audit.log(
        action=action,
        actor_subject_id=user.subject_id,
        resource_kind="calendar_subscription",
        resource_id=subscription_id,
        metadata={"patient_id": str(patient.id), "purge": purge},
    )
    await db.commit()
    return Response(status_code=204)


@router.get(
    "/calendar/feed/{token}.ics",
    response_model=None,
)
async def public_calendar_feed(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User | None, Depends(public_user)] = None,
) -> Response:
    """Public (anonymous-allowed) iCal feed for a subscription token.

    The ONLY unauthenticated calendar surface. Resolution order, each
    step failing closed with a generic 404 so the endpoint never
    discloses whether a patient/subscription exists:

    1. HMAC verify the token (tamper / forgery / wrong-secret -> 404).
    2. Load the row by the signed subscription id AND assert its
       ``patient_id`` equals the signed one (cross-patient is
       cryptographically inexpressible; this is belt-and-braces).
    3. Reject revoked / expired handles.
    4. Render the full calendar (same renderer as the auth feed; no
       VALARM because subscription clients poll).

    Usage is recorded on the row (best-effort; a counter write never
    breaks the feed) instead of a per-poll audit entry, which would
    flood the audit log given calendar apps refresh every few minutes.
    """
    verified = verify_subscription_token(token)
    if verified is None:
        raise HTTPException(status_code=404, detail="not found")
    subscription_id, signed_patient_id = verified

    row = (
        await db.execute(
            select(CalendarSubscription).where(
                CalendarSubscription.id == subscription_id,
                CalendarSubscription.patient_id == signed_patient_id,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if (
        row is None
        or row.revoked_at is not None
        or (row.expires_at is not None and row.expires_at <= now)
    ):
        raise HTTPException(status_code=404, detail="not found")

    events = await _load_calendar_events(db, row.patient_id)
    ics = render_ics(events, lang="it", with_valarm=False)

    # Best-effort usage trail. A failure here must never deny the feed.
    try:
        await db.execute(
            update(CalendarSubscription)
            .where(CalendarSubscription.id == row.id)
            .values(
                access_count=CalendarSubscription.access_count + 1,
                last_accessed_at=now,
            )
        )
        await db.commit()
    except Exception:  # pragma: no cover - defensive, feed must not fail
        await db.rollback()

    return Response(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="bitvision-calendar.ics"',
            # Subscription clients poll; allow a short private cache but
            # never let a shared proxy retain clinical data.
            "Cache-Control": "private, max-age=300",
        },
    )
