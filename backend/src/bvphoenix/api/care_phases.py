"""REST API for care phases and the care timeline.

Routes are namespaced under ``/api/patients/{patient_id}/...`` so
cross-patient operations are unrepresentable at the URL level: a
phase id from another patient resolves to 404 (the resource does
not exist within this namespace), never 400. The composite FK
``(patient_id, phase_id) → care_phase (patient_id, id)`` declared on
``clinical_events`` enforces the same invariant at the DB level.

Mutations follow the v3 convention: ``If-Match`` ETag mandatory on
PATCH, ``Idempotency-Key`` mandatory on the proposal-apply endpoint.
Audit lines are written by the service layer.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import PlainTextResponse
from fastapi.responses import Response as FResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import Patient, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services import care_phases as svc
from bvphoenix.services.care_phase_schemas import (
    ApplyProposalIn,
    ApplyProposalOut,
    AssignPhaseIn,
    CarePhaseCreateIn,
    CarePhaseDetailOut,
    CarePhaseMaterialOut,
    CarePhaseOut,
    CarePhaseRevisionOut,
    CarePhaseUpdateIn,
    CareTimelineOut,
    ProposePhasesOut,
    ReorderIn,
    RestoreRevisionIn,
    TimelineEventOut,
    TimelineHealthOut,
)
from bvphoenix.services.etag import format_etag, parse_if_match
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
)

router = APIRouter(tags=["care-phases"])


# ----------------------------------------------------------------------
# Authorization helper
# ----------------------------------------------------------------------


async def _ensure_patient_access(
    db: AsyncSession,
    *,
    request: Request,
    user: User,
    patient_id: uuid.UUID,
    write: bool = False,
) -> Patient:
    """Resolve + authorise the patient for a care-phases endpoint.

    Order of checks (each fails closed):

    1. Patient row exists. 404 otherwise — same shape used everywhere
       under ``/api/patients/{id}/...``.
    2. Human RBAC: ``can_patient(action=READ_METADATA|WRITE_REPORT)``.
       Failure also returns 404 (resource does not exist in this
       namespace) so the caller cannot tell ``no permission`` from
       ``not exists``.
    3. **Agent patient-scope**: when the call carries an agent token
       (``request.state.is_agent``) the patient must belong to the
       assistant's ``agent_patient_ids`` set. The cross-patient
       invariant (memoria ``cross_patient_links_forbidden``) is the
       reason this check runs even after the human RBAC clears: a
       compromised agent token whose underlying user is broadly
       privileged must still be confined to its consented fascicoli.
    """
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    action = WRITE_REPORT if write else READ_METADATA
    if not await can_patient(db, user=user, action=action, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)
    return patient


def _author_kind(request: Request) -> str:
    """Derive ``author_kind`` from the request state set by
    ``_resolve_user``. Agent tokens write rows stamped ``"agent"`` so
    the provenance trail (and the UI's "AI" badge) reflects reality —
    see memoria ``feedback_ai_provenance_must_be_visible``."""
    return "agent" if getattr(request.state, "is_agent", False) else "human"


# ----------------------------------------------------------------------
# Read endpoints
# ----------------------------------------------------------------------


@router.get(
    "/patients/{patient_id}/care-phases",
    response_model=list[CarePhaseOut],
)
async def list_phases(
    patient_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
) -> list[CarePhaseOut]:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id)
    return await svc.list_phases(patient_id=patient_id, db=db)


@router.get(
    "/patients/{patient_id}/care-phases/{phase_id}",
    response_model=CarePhaseDetailOut,
)
async def read_phase(
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    response: Response,
) -> CarePhaseDetailOut:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id)
    detail = await svc.get_phase(patient_id=patient_id, phase_id=phase_id, db=db)
    response.headers["ETag"] = format_etag(str(detail.etag))
    return detail


@router.get(
    "/patients/{patient_id}/care-phases/{phase_id}/material",
    response_model=CarePhaseMaterialOut,
)
async def read_phase_material(
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
) -> CarePhaseMaterialOut:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id)
    return await svc.get_phase_material(patient_id=patient_id, phase_id=phase_id, db=db)


@router.get(
    "/patients/{patient_id}/care-phases/{phase_id}/revisions",
    response_model=list[CarePhaseRevisionOut],
)
async def read_phase_revisions(
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
) -> list[CarePhaseRevisionOut]:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id)
    return await svc.list_revisions(patient_id=patient_id, phase_id=phase_id, db=db)


@router.get(
    "/patients/{patient_id}/care-timeline/health",
    response_model=TimelineHealthOut,
)
async def read_timeline_health(
    patient_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
) -> TimelineHealthOut:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id)
    return await svc.timeline_health(patient_id=patient_id, db=db)


@router.get("/patients/{patient_id}/care-timeline")
async def read_timeline(
    patient_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    lang: Annotated[Literal["it", "en"], Query()] = "it",
    format: Annotated[Literal["json", "svg", "markdown", "ics", "pdf"], Query()] = "json",
    theme: Annotated[Literal["light", "dark"], Query()] = "light",
    width: Annotated[int, Query(ge=400, le=2000)] = 680,
):
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id)
    timeline = await svc.get_timeline(patient_id=patient_id, lang=lang, db=db)
    if format == "json":
        return timeline
    if format == "svg":
        from bvphoenix.services.care_phase_svg import render_svg

        body = render_svg(timeline, lang=lang, theme=theme, width=width)
        return FResponse(content=body, media_type="image/svg+xml")
    if format == "markdown":
        return PlainTextResponse(content=_render_timeline_markdown(timeline, lang=lang))
    if format == "ics":
        body = _render_timeline_ics(timeline, lang=lang)
        return FResponse(content=body, media_type="text/calendar; charset=utf-8")
    if format == "pdf":
        import cairosvg

        from bvphoenix.services.care_phase_svg import render_svg

        svg_body = render_svg(timeline, lang=lang, theme=theme, width=width)
        pdf_body = cairosvg.svg2pdf(bytestring=svg_body.encode("utf-8"))
        filename = f"care-timeline-{timeline.patient_id}.pdf"
        return FResponse(
            content=pdf_body,
            media_type="application/pdf",
            headers={"content-disposition": f'inline; filename="{filename}"'},
        )
    raise HTTPException(status_code=400, detail="unsupported format")


def _render_timeline_ics(timeline: CareTimelineOut, *, lang: str) -> str:
    """Render the timeline as an iCalendar (RFC 5545) document.

    One VEVENT per clinical event, categorised by phase slug, with a
    deterministic UID so re-imports update existing entries instead of
    duplicating them.
    """
    from datetime import timedelta

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//bitvision//care-timeline//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    name = (
        f"Care timeline {timeline.patient_id}"
        if lang == "en"
        else f"Timeline clinica {timeline.patient_id}"
    )
    lines.append(f"X-WR-CALNAME:{name}")

    def _emit_event(ev, phase_slug: str | None) -> None:
        if ev.event_date is None:
            return
        dt = ev.event_date.strftime("%Y%m%d")
        dt_end = (ev.event_date + timedelta(days=1)).strftime("%Y%m%d")
        uid = f"care-event-{ev.id}@bitvision"
        summary = ev.title.replace("\n", " ").strip()
        cats = phase_slug or ("unassigned" if lang == "en" else "non-assegnati")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{timeline.generated_at.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART;VALUE=DATE:{dt}",
                f"DTEND;VALUE=DATE:{dt_end}",
                f"SUMMARY:{summary}",
                f"CATEGORIES:{cats}",
                f"DESCRIPTION:kind={ev.kind}",
                "END:VEVENT",
            ]
        )

    for phase in timeline.phases:
        for ev in phase.events:
            _emit_event(ev, phase.slug)
    for ev in timeline.unassigned_events:
        _emit_event(ev, None)

    lines.append("END:VCALENDAR")
    # iCal mandates CRLF line endings.
    return "\r\n".join(lines) + "\r\n"


def _render_timeline_markdown(timeline: CareTimelineOut, *, lang: str) -> str:
    """Render a markdown view of the timeline with mcp:// links."""
    parts: list[str] = []
    parts.append(f"# Care timeline ({timeline.patient_id})")
    for phase in timeline.phases:
        title = phase.name_i18n.get(lang) or phase.name
        parts.append(f"\n## {title}")
        if phase.start_date or phase.end_date:
            parts.append(f"_{phase.start_date or ''} → {phase.end_date or ''}_  ")
        if phase.narrative_md:
            parts.append(phase.narrative_md)
        for ev in phase.events:
            date_s = ev.event_date.isoformat() if ev.event_date else "—"
            parts.append(f"- {date_s} — {ev.title} ([{ev.target.kind}]({ev.target.mcp_uri}))")
    if timeline.unassigned_events:
        parts.append("\n## Non assegnati" if lang == "it" else "\n## Unassigned")
        for ev in timeline.unassigned_events:
            date_s = ev.event_date.isoformat() if ev.event_date else "—"
            parts.append(f"- {date_s} — {ev.title} ([{ev.target.kind}]({ev.target.mcp_uri}))")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Write endpoints
# ----------------------------------------------------------------------


@router.post(
    "/patients/{patient_id}/care-phases",
    response_model=CarePhaseOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_phase(
    patient_id: uuid.UUID,
    data: CarePhaseCreateIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    request: Request,
    response: Response,
) -> CarePhaseOut:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id, write=True)
    out = await svc.create_phase(
        patient_id=patient_id,
        data=data,
        actor_id=user.subject_id,
        author_kind=_author_kind(request),
        db=db,
        request=request,
    )
    response.headers["ETag"] = format_etag(str(out.etag))
    return out


@router.patch(
    "/patients/{patient_id}/care-phases/{phase_id}",
    response_model=CarePhaseOut,
)
async def patch_phase(
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    data: CarePhaseUpdateIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    request: Request,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CarePhaseOut:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id, write=True)
    expected = parse_if_match(if_match)
    out = await svc.update_phase(
        patient_id=patient_id,
        phase_id=phase_id,
        data=data,
        expected_etag=expected,
        actor_id=user.subject_id,
        author_kind=_author_kind(request),
        db=db,
        request=request,
    )
    response.headers["ETag"] = format_etag(str(out.etag))
    return out


@router.delete(
    "/patients/{patient_id}/care-phases/{phase_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_phase(
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    request: Request,
) -> Response:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id, write=True)
    await svc.delete_phase(
        patient_id=patient_id,
        phase_id=phase_id,
        actor_id=user.subject_id,
        db=db,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/patients/{patient_id}/care-phases/{phase_id}/events/{event_id}",
    response_model=TimelineEventOut,
)
async def assign_event(
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    event_id: uuid.UUID,
    data: AssignPhaseIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    request: Request,
) -> TimelineEventOut:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id, write=True)
    return await svc.assign_event(
        patient_id=patient_id,
        phase_id=phase_id,
        event_id=event_id,
        confidence=data.confidence,
        actor_id=user.subject_id,
        author_kind=_author_kind(request),
        db=db,
        request=request,
    )


@router.delete(
    "/patients/{patient_id}/care-phases/{phase_id}/events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unassign_event(
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    event_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    request: Request,
) -> Response:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id, write=True)
    await svc.unassign_event(
        patient_id=patient_id,
        phase_id=phase_id,
        event_id=event_id,
        actor_id=user.subject_id,
        db=db,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/patients/{patient_id}/care-phases:reorder",
    response_model=list[CarePhaseOut],
)
async def reorder_phases(
    patient_id: uuid.UUID,
    data: ReorderIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    request: Request,
) -> list[CarePhaseOut]:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id, write=True)
    return await svc.reorder_phases(
        patient_id=patient_id,
        items=data.ordinals,
        actor_id=user.subject_id,
        db=db,
        request=request,
    )


@router.post(
    "/patients/{patient_id}/care-phases/{phase_id}/restore",
    response_model=CarePhaseOut,
)
async def restore_phase_revision(
    patient_id: uuid.UUID,
    phase_id: uuid.UUID,
    data: RestoreRevisionIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    request: Request,
    response: Response,
) -> CarePhaseOut:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id, write=True)
    out = await svc.restore_revision(
        patient_id=patient_id,
        phase_id=phase_id,
        revision_no=data.revision_no,
        actor_id=user.subject_id,
        db=db,
        request=request,
    )
    response.headers["ETag"] = format_etag(str(out.etag))
    return out


# ----------------------------------------------------------------------
# Proposal flow (LLM classifier)
# ----------------------------------------------------------------------


@router.post(
    "/patients/{patient_id}/care-phases:propose",
    response_model=ProposePhasesOut,
)
async def propose_phases(
    patient_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    request: Request,
    lang: Annotated[Literal["it", "en"], Query()] = "it",
    async_run: Annotated[
        bool,
        Query(
            alias="async",
            description=(
                "Enqueue the classifier on the arq worker instead of "
                "running it synchronously in this request. Use for "
                "patients with many events when the LLM call would "
                "exceed the request timeout. The endpoint still "
                "returns 200 with a synthetic ProposePhasesOut whose "
                "``status`` is 'queued' and whose ``proposal_id`` is "
                "empty until the worker materialises the row; poll "
                "``GET /api/jobs/{job_id}`` to track progress."
            ),
        ),
    ] = False,
) -> ProposePhasesOut:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id, write=True)
    if async_run:
        # Enqueue and return a "queued" envelope. The worker calls the
        # same classifier function under the hood.
        from arq import create_pool

        from bvphoenix.config import get_settings as _settings
        from bvphoenix.services.arq_redis import redis_settings

        redis = await create_pool(redis_settings(_settings().redis_url))
        try:
            arq_handle = await redis.enqueue_job(
                "propose_care_phases",
                str(patient_id),
                str(user.subject_id) if user.subject_id else None,
                lang,
                _job_id=f"care-phase-propose-{patient_id}",
            )
        finally:
            await redis.close()
        from datetime import UTC
        from datetime import datetime as _dt

        from bvphoenix.services.care_phase_schemas import ProposalPayload

        job_id = uuid.UUID(arq_handle.job_id) if arq_handle else None
        return ProposePhasesOut(
            proposal_id=uuid.UUID(int=0),
            job_id=job_id,
            status="queued",
            payload=ProposalPayload(phases=[], assignments=[]),
            model_id="(pending)",
            cached=False,
            created_at=_dt.now(UTC),
        )

    from bvphoenix.services.care_phase_classifier import propose_for_patient

    return await propose_for_patient(
        patient_id=patient_id,
        actor_id=user.subject_id,
        lang=lang,
        db=db,
        request=request,
    )


@router.post(
    "/patients/{patient_id}/care-phases:apply-proposal",
    response_model=ApplyProposalOut,
)
async def apply_proposal(
    patient_id: uuid.UUID,
    data: ApplyProposalIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _audit: AuditDep,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ApplyProposalOut:
    await _ensure_patient_access(db, request=request, user=user, patient_id=patient_id, write=True)
    if not idempotency_key:
        raise HTTPException(
            status_code=428,
            detail="Idempotency-Key header is required for this mutation",
        )
    applied_phase_ids, n_applied, n_skipped = await svc.apply_proposal(
        patient_id=patient_id,
        data=data,
        actor_id=user.subject_id,
        db=db,
        request=request,
    )
    return ApplyProposalOut(
        applied_phases=applied_phase_ids,
        applied_assignments=n_applied,
        skipped_assignments=n_skipped,
    )


__all__ = ["router"]
