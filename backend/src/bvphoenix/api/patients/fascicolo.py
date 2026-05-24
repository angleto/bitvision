# ruff: noqa: F405
# Auto-split from api/patients.py on 2026-05-21.
# Section: ``fascicolo``. Decorators register against the
# local ``router`` below; the package __init__.py
# aggregates every child via include_router so main.py's
# wiring stays a single line.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.patients import _shared  # for runtime access
from bvphoenix.api.patients._shared import *  # noqa: F403

router = APIRouter()


@router.get("/patients/{patient_id}/index", response_model=FascicoloIndex)
async def get_fascicolo_index(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> FascicoloIndex:
    patient = await _get_patient_or_404(db, patient_id, user, request)

    # Studies count + modality breakdown
    study_rows = (
        await db.execute(
            select(ImagingStudy.id, ImagingStudy.modalities, ImagingStudy.study_date)
            .where(ImagingStudy.patient_id == patient.id)
            .order_by(ImagingStudy.study_date.desc().nullslast())
        )
    ).all()
    study_count = len(study_rows)
    modality_breakdown: dict[str, int] = {}
    study_last_date: str | None = None
    for row in study_rows:
        if study_last_date is None and row.study_date:
            study_last_date = str(row.study_date)
        for m in row.modalities or []:
            modality_breakdown[m] = modality_breakdown.get(m, 0) + 1

    study_ids = [row.id for row in study_rows]

    # Reports count — v3: count ReportContent rows whose parent
    # ClinicalEvent is one of the imaging studies of this patient.
    # All authority levels are counted (the FE shows the breakdown
    # by authority in the section detail).
    report_count = 0
    report_last_date: str | None = None
    if study_ids:
        from bvphoenix.db.models import ClinicalEvent, ReportContent

        rc_agg = (
            await db.execute(
                select(func.count(), func.max(ReportContent.created_at))
                .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
                .join(ImagingStudy, ImagingStudy.clinical_event_id == ClinicalEvent.id)
                .where(ImagingStudy.id.in_(study_ids))
            )
        ).first()
        if rc_agg:
            report_count = rc_agg[0]
            if rc_agg[1]:
                report_last_date = rc_agg[1].isoformat()

    # Markers count (in-viewer ephemera: measurements, fiducials,
    # text-overlays, reading-note bookmarks). The legacy ``annotations``
    # table is retired; everything ephemeral lives on Markers now.
    annotation_count = 0
    annotation_last_date: str | None = None
    if study_ids:
        marker_agg = (
            await db.execute(
                select(func.count(), func.max(Marker.created_at)).where(
                    Marker.target_kind == "study",
                    Marker.target_id.in_(study_ids),
                )
            )
        ).first()
        if marker_agg:
            annotation_count = marker_agg[0]
            if marker_agg[1]:
                annotation_last_date = marker_agg[1].isoformat()

    # Patient documents count + type breakdown
    doc_rows = (
        await db.execute(
            select(Document.kind_id, func.count(), func.max(Document.created_at))
            .where(Document.patient_id == patient.id)
            .group_by(Document.kind_id)
        )
    ).all()
    doc_count = sum(r[1] for r in doc_rows)
    doc_breakdown: dict[str, int] = {r[0]: r[1] for r in doc_rows}
    doc_last_date: str | None = None
    for r in doc_rows:
        if r[2]:
            d = r[2].isoformat()
            if doc_last_date is None or d > doc_last_date:
                doc_last_date = d

    # Separate personal notebook count
    notebook_count = doc_breakdown.pop("personal_notebook", 0)

    # v3 phase 3b: the ``DocumentStudyLink`` table was retired. The v3
    # successor is ``ContentDocumentLink`` (Content↔Document, n:m,
    # routed through ``ReportContent.clinical_event_id`` rather than
    # directly through Study). The fascicolo index aggregates it via
    # ``content_document_links`` once phase 4 ships the matching
    # service helper; for now the section is reported as zero so
    # the UI does not display a stale count.
    link_count = 0
    link_last_date: str | None = None
    link_breakdown: dict[str, int] | None = None

    sections = [
        FascicoloSection(
            key="studies",
            label="Studi Diagnostici",
            count=study_count,
            last_date=study_last_date,
            breakdown=modality_breakdown if modality_breakdown else None,
        ),
        FascicoloSection(
            key="reports",
            label="Referti",
            count=report_count,
            last_date=report_last_date,
            breakdown=None,
        ),
        FascicoloSection(
            key="documents",
            label="Documenti Clinici",
            count=doc_count - notebook_count if doc_count > notebook_count else doc_count,
            last_date=doc_last_date,
            breakdown=doc_breakdown if doc_breakdown else None,
        ),
        FascicoloSection(
            key="document_study_links",
            label="Documenti collegati a studi",
            count=link_count,
            last_date=link_last_date,
            breakdown=link_breakdown,
        ),
        FascicoloSection(
            key="annotations",
            label="Annotazioni",
            count=annotation_count,
            last_date=annotation_last_date,
            breakdown=None,
        ),
        FascicoloSection(
            key="personal_notebook",
            label="Taccuino Personale",
            count=notebook_count,
            last_date=None,
            breakdown=None,
        ),
    ]

    total = study_count + report_count + annotation_count + doc_count
    contacts = await _load_patient_contacts(db, patient.id)
    return FascicoloIndex(
        patient=_patient_out(patient, user=user, contacts=contacts),
        sections=sections,
        total_items=total,
    )


@router.get("/patients/{patient_id}/timeline", response_model=list[TimelineItem])
async def get_timeline(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    section: str | None = Query(None, description="Filter: studies|reports|documents|annotations"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[TimelineItem]:
    patient = await _get_patient_or_404(db, patient_id, user, request)
    items: list[TimelineItem] = []

    study_ids: list[uuid.UUID] = []
    if section is None or section == "studies":
        studies = (
            (
                await db.execute(
                    select(ImagingStudy)
                    .where(ImagingStudy.patient_id == patient.id)
                    .order_by(ImagingStudy.study_date.desc().nullslast())
                )
            )
            .scalars()
            .all()
        )
        study_ids = [s.id for s in studies]
        for s in studies:
            items.append(
                TimelineItem(
                    type="study",
                    date=(str(s.study_date) if s.study_date else s.created_at.isoformat()),
                    data={
                        "id": str(s.id),
                        "study_description": s.study_description,
                        "modalities": s.modalities or [],
                        "study_date": str(s.study_date) if s.study_date else None,
                    },
                )
            )
    else:
        # Need study_ids for reports/annotations even if not showing studies
        study_ids = [
            r[0]
            for r in (
                await db.execute(
                    select(ImagingStudy.id).where(ImagingStudy.patient_id == patient.id)
                )
            ).all()
        ]

    if study_ids and (section is None or section == "reports"):
        # v3: timeline 'report' items are ReportContent rows whose
        # parent ClinicalEvent is one of the patient's imaging studies.
        from bvphoenix.db.models import ClinicalEvent, ReportContent

        reports = (
            await db.execute(
                select(ReportContent, ImagingStudy.id.label("imaging_study_id"))
                .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
                .join(ImagingStudy, ImagingStudy.clinical_event_id == ClinicalEvent.id)
                .where(ImagingStudy.id.in_(study_ids))
                .order_by(ReportContent.created_at.desc())
            )
        ).all()
        for r, imaging_study_id in reports:
            items.append(
                TimelineItem(
                    type="report",
                    date=r.created_at.isoformat(),
                    data={
                        "id": str(r.id),
                        "study_id": str(imaging_study_id),
                        "authority": r.authority_id,
                        "status": r.status,
                        "title": r.title,
                        "text": (r.narrative_md or "")[:200] or None,
                    },
                )
            )

    if study_ids and (section is None or section in ("annotations", "markers")):
        markers = (
            (
                await db.execute(
                    select(Marker)
                    .where(Marker.target_kind == "study", Marker.target_id.in_(study_ids))
                    .order_by(Marker.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for m in markers:
            items.append(
                TimelineItem(
                    type="marker",
                    date=m.created_at.isoformat(),
                    data={
                        "id": str(m.id),
                        "target_id": str(m.target_id),
                        "kind": m.kind,
                    },
                )
            )

    if section is None or section == "documents":
        docs = (
            (
                await db.execute(
                    select(Document)
                    .where(Document.patient_id == patient.id)
                    .order_by(Document.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for d in docs:
            items.append(
                TimelineItem(
                    type="document",
                    date=(str(d.document_date) if d.document_date else d.created_at.isoformat()),
                    data={
                        "id": str(d.id),
                        "document_type": d.kind_id,
                        "title": d.title,
                    },
                )
            )

    # Sort all items by date descending
    items.sort(key=lambda x: x.date, reverse=True)
    return items[offset : offset + limit]
