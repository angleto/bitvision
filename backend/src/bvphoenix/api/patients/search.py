# ruff: noqa: F405
# Auto-split from api/patients.py on 2026-05-21.
# Section: ``search``. Decorators register against the
# local ``router`` below; the package __init__.py
# aggregates every child via include_router so main.py's
# wiring stays a single line.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.patients import _shared  # for runtime access
from bvphoenix.api.patients._shared import *  # noqa: F403

# v3 folded the legacy ``Report`` / ``Consultation`` models into
# ``ReportContent`` (event-scoped, distinguished by ``authority_id``).
# These are not re-exported by ``_shared``; import them explicitly so the
# ``reports`` / ``consultations`` search sections resolve instead of
# raising NameError at request time.
from bvphoenix.db.models import ClinicalEvent, ReportContent
from bvphoenix.services.fts import dual_tsquery, dual_tsvector

# Authority ids that represent an actual clinician report (as opposed to
# a BitVision curated synthesis, which is ``canonical_synthesis``).
_REPORT_AUTHORITIES: tuple[str, ...] = ("original", "derived")

router = APIRouter()


@router.get("/patients/{patient_id}/search", response_model=PatientSearchOut)
async def patient_scoped_search(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    q: str = Query(..., min_length=1, max_length=128),
    sections: str | None = Query(
        None,
        description="Comma-separated subset of studies,reports,annotations,documents,consultations",
    ),
    semantic: bool = Query(False, description="Enable semantic fallback if text results < 10"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PatientSearchOut:
    """Full-text search scoped to a single patient's fascicolo.

    Uses PostgreSQL ts_rank over each section's relevant text column,
    merges results, and sorts by rank then recency. Optional semantic
    fallback (BiomedCLIP) is wired through to the S3 helper when it is
    available — today it's a no-op because the series-level embedding
    search doesn't yet expose a text-query API.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)
    wanted = _parse_sections(sections)

    ts_query = dual_tsquery(q)

    items: list[PatientSearchItem] = []
    by_section: dict[str, int] = dict.fromkeys(SEARCH_SECTIONS, 0)

    # ---- studies ----
    if "studies" in wanted:
        # Dual-config (italian || simple) generated, GIN-indexed columns.
        study_desc_vec = ImagingStudy.study_description_tsv
        series_desc_vec = Series.series_description_tsv
        # Take the greatest rank across study_description and the joined
        # series' series_description so series-level matches still surface.
        rank_expr = func.greatest(
            func.ts_rank(study_desc_vec, ts_query),
            func.coalesce(func.ts_rank(series_desc_vec, ts_query), 0.0),
        )
        study_q = (
            select(ImagingStudy, func.max(rank_expr).label("rank"))
            .outerjoin(Series, Series.study_id == ImagingStudy.id)
            .where(
                ImagingStudy.patient_id == patient.id,
                or_(study_desc_vec.op("@@")(ts_query), series_desc_vec.op("@@")(ts_query)),
            )
            .group_by(ImagingStudy.id)
            .order_by(desc("rank"), ImagingStudy.created_at.desc())
        )
        for study, rank in (await db.execute(study_q)).all():
            items.append(
                PatientSearchItem(
                    section="studies",
                    id=str(study.id),
                    title=study.study_description or f"ImagingStudy {study.study_instance_uid}",
                    preview=_preview(study.study_description),
                    rank=float(rank or 0.0),
                    created_at=study.created_at.isoformat(),
                )
            )
            by_section["studies"] += 1

    # ---- reports ----
    # v3: a clinician report is a ReportContent with authority
    # ``original`` / ``derived`` (vs ``canonical_synthesis`` = a curated
    # synthesis, handled by the consultations section). ReportContent is
    # event-scoped, so we reach the patient through ClinicalEvent and
    # drop superseded (``stale``) rows so we never surface a retired
    # version of a report.
    if "reports" in wanted:
        r_narr_vec = dual_tsvector(ReportContent.narrative_md)
        r_find_vec = dual_tsvector(ReportContent.findings_md)
        r_recs_vec = dual_tsvector(ReportContent.recommendations_md)
        rank_expr = func.greatest(
            func.ts_rank(r_narr_vec, ts_query),
            func.ts_rank(r_find_vec, ts_query),
            func.ts_rank(r_recs_vec, ts_query),
        )
        report_q = (
            select(ReportContent, rank_expr.label("rank"))
            .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
            .where(
                ClinicalEvent.patient_id == patient.id,
                ReportContent.authority_id.in_(_REPORT_AUTHORITIES),
                ReportContent.status != "stale",
                or_(
                    r_narr_vec.op("@@")(ts_query),
                    r_find_vec.op("@@")(ts_query),
                    r_recs_vec.op("@@")(ts_query),
                ),
            )
            .order_by(desc("rank"), ReportContent.created_at.desc())
        )
        for rc, rank in (await db.execute(report_q)).all():
            items.append(
                PatientSearchItem(
                    section="reports",
                    id=str(rc.id),
                    title=rc.title or "Referto",
                    preview=_preview(rc.narrative_md or rc.findings_md or rc.recommendations_md),
                    rank=float(rank or 0.0),
                    created_at=rc.created_at.isoformat(),
                )
            )
            by_section["reports"] += 1

    # The legacy "annotations" search section has been retired together
    # with the descriptor channel (option-B refactor). Markers carry
    # geometric / numeric data without enough free text to power FTS;
    # human prose lives on ``ClinicalNote`` and is queried elsewhere.

    # ---- documents ----
    if "documents" in wanted:
        doc_title_vec = dual_tsvector(Document.title)
        doc_text_vec = dual_tsvector(Document.text)
        # Weight the title match higher than body matches — a hit in the
        # title is a much stronger signal than one buried in the text.
        rank_expr = func.greatest(
            func.ts_rank(doc_title_vec, ts_query) * 2.0,
            func.ts_rank(doc_text_vec, ts_query),
        )
        doc_q = (
            select(Document, rank_expr.label("rank"))
            .where(
                Document.patient_id == patient.id,
                or_(doc_title_vec.op("@@")(ts_query), doc_text_vec.op("@@")(ts_query)),
            )
            .order_by(desc("rank"), Document.created_at.desc())
        )
        for doc, rank in (await db.execute(doc_q)).all():
            items.append(
                PatientSearchItem(
                    section="documents",
                    id=str(doc.id),
                    title=doc.title,
                    preview=_preview(doc.text),
                    rank=float(rank or 0.0),
                    created_at=doc.created_at.isoformat(),
                )
            )
            by_section["documents"] += 1

    # ---- consultations / synthesis ----
    # v3: Consultation was folded into ReportContent with
    # ``authority='canonical_synthesis'``. The FTS surface joins the
    # narrative_md / findings_md / recommendations_md columns plus
    # crosses ClinicalEvent → Patient because ReportContent is event-
    # scoped, not patient-scoped, in the v3 schema.
    if "consultations" in wanted:
        narr_vec = dual_tsvector(ReportContent.narrative_md)
        findings_vec = dual_tsvector(ReportContent.findings_md)
        recs_vec = dual_tsvector(ReportContent.recommendations_md)
        rank_expr = func.greatest(
            func.ts_rank(narr_vec, ts_query),
            func.ts_rank(findings_vec, ts_query),
            func.ts_rank(recs_vec, ts_query),
        )
        cons_q = (
            select(ReportContent, rank_expr.label("rank"))
            .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
            .where(
                ClinicalEvent.patient_id == patient.id,
                ReportContent.authority_id == "canonical_synthesis",
                or_(
                    narr_vec.op("@@")(ts_query),
                    findings_vec.op("@@")(ts_query),
                    recs_vec.op("@@")(ts_query),
                ),
            )
            .order_by(desc("rank"), ReportContent.created_at.desc())
        )
        for rc, rank in (await db.execute(cons_q)).all():
            preview = _preview(rc.narrative_md or rc.findings_md or rc.recommendations_md)
            items.append(
                PatientSearchItem(
                    section="consultations",
                    id=str(rc.id),
                    title=rc.title or "(untitled synthesis)",
                    preview=preview,
                    rank=float(rank or 0.0),
                    created_at=rc.created_at.isoformat(),
                )
            )
            by_section["consultations"] += 1

    # ---- Optional semantic fallback ----
    # If text-only search didn't give us much and the caller asked for
    # semantic, try to import the S3 helper. It isn't wired up yet so we
    # skip silently when unavailable — the endpoint still returns the
    # text results we did find.
    if semantic and len(items) < _SEMANTIC_FALLBACK_THRESHOLD:
        try:
            from bvphoenix.services.semantic_search import (  # type: ignore[import-not-found]
                semantic_search_patient,
            )

            extra = await semantic_search_patient(db, patient_id=patient.id, query=q)
            seen = {(it.section, it.id) for it in items}
            for entry in extra:
                key = (entry["section"], entry["id"])
                if key in seen:
                    continue
                items.append(PatientSearchItem(**entry))
                by_section[entry["section"]] = by_section.get(entry["section"], 0) + 1
        except ImportError:
            pass

    # Global sort: primary key is ts_rank DESC, tie-breaker is recency.
    # Python's sort is stable, so sort by the weaker key first.
    items.sort(key=lambda it: it.created_at, reverse=True)
    items.sort(key=lambda it: it.rank, reverse=True)

    total = len(items)
    paged = items[offset : offset + limit]

    return PatientSearchOut(
        patient_id=str(patient.id),
        query=q,
        total=total,
        by_section=by_section,
        items=paged,
    )


@router.get(
    "/patients/{patient_id}/mention-search",
    response_model=PatientSearchOut,
)
async def patient_mention_search(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    q: str | None = Query(
        default=None,
        max_length=128,
        description=(
            "Free-text prefix typed by the user after ``@`` in the editor. "
            "Empty / null returns the most recent items."
        ),
    ),
    sections: str | None = Query(
        default=None,
        description=(
            "Comma-separated subset of "
            "``studies,reports,documents,consultations,folders``. When "
            "omitted, every section is queried. The frontend uses this "
            "to scope by kind when the user types ``@stu`` (→ studies), "
            "``@doc`` (→ documents) or ``@cart`` (→ folders)."
        ),
    ),
    limit: int = Query(10, ge=1, le=30),
) -> PatientSearchOut:
    """Lightweight autocomplete for the Evidenze e sintesi editor.

    Difference from ``/patients/{id}/search``:

    * **Prefix match** (``ILIKE 'foo%'``) instead of full-text
      ``plainto_tsquery`` — typing ``stu`` matches "ImagingStudy chest CT"
      because to_tsvector tokenizes "study" as a whole word and
      ts_rank doesn't do prefix matching by default.
    * **Empty query is allowed** and returns the most-recent items
      across all kinds; the editor uses this so a bare ``@`` shows
      something useful instead of "no results".
    * **Tight cap** (default 10, max 30) because the dropdown is the
      consumer; pagination doesn't apply.

    Sections covered: study / report / document / consultation. The
    annotations table is omitted because annotations are typically
    not referenced by name in clinician notes — the underlying
    study can be referenced via ``@study:`` instead.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request, action=READ_METADATA)

    needle = (q or "").strip()
    use_prefix = len(needle) > 0
    # ILIKE pattern: escape the wildcards the user might have typed so a
    # ``%`` doesn't widen the match. Trailing ``%`` is the prefix anchor.
    if use_prefix:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"{escaped}%"
    else:
        pattern = None

    items: list[PatientSearchItem] = []
    by_section: dict[str, int] = dict.fromkeys(SEARCH_SECTIONS, 0)
    wanted = _parse_sections(sections) if sections else set(SEARCH_SECTIONS)

    # ---- studies ----
    if "studies" not in wanted:
        study_q = None
    else:
        study_q = (
            select(ImagingStudy)
            .where(ImagingStudy.patient_id == patient.id)
            .order_by(ImagingStudy.study_date.desc().nullslast(), ImagingStudy.created_at.desc())
            .limit(limit)
        )
        if use_prefix:
            study_q = study_q.where(ImagingStudy.study_description.ilike(pattern, escape="\\"))
    rows = (await db.execute(study_q)).scalars().all() if study_q is not None else []
    for study in rows:
        items.append(
            PatientSearchItem(
                section="studies",
                id=str(study.id),
                title=study.study_description or f"ImagingStudy {study.study_instance_uid}",
                preview=_preview(study.study_description),
                rank=1.0,
                created_at=study.created_at.isoformat(),
            )
        )
        by_section["studies"] += 1

    # ---- documents ----
    if "documents" not in wanted:
        docs: list[Document] = []
    else:
        doc_q = (
            select(Document)
            .where(Document.patient_id == patient.id)
            .order_by(Document.created_at.desc())
            .limit(limit)
        )
        if use_prefix:
            doc_q = doc_q.where(Document.title.ilike(pattern, escape="\\"))
        docs = (await db.execute(doc_q)).scalars().all()
    for d in docs:
        items.append(
            PatientSearchItem(
                section="documents",
                id=str(d.id),
                title=d.title,
                preview=_preview(d.text),
                rank=1.0,
                created_at=d.created_at.isoformat(),
            )
        )
        by_section["documents"] += 1

    # ---- reports (v3: original/derived ReportContent, patient-scoped via ClinicalEvent) ----
    if "reports" not in wanted:
        report_rows: list[ReportContent] = []
    else:
        report_q = (
            select(ReportContent)
            .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
            .where(
                ClinicalEvent.patient_id == patient.id,
                ReportContent.authority_id.in_(_REPORT_AUTHORITIES),
                ReportContent.status != "stale",
            )
            .order_by(ReportContent.created_at.desc())
            .limit(limit)
        )
        if use_prefix:
            report_q = report_q.where(ReportContent.title.ilike(pattern, escape="\\"))
        report_rows = list((await db.execute(report_q)).scalars().all())
    for rc in report_rows:
        items.append(
            PatientSearchItem(
                section="reports",
                id=str(rc.id),
                title=rc.title or "Referto",
                preview=_preview(rc.narrative_md),
                rank=1.0,
                created_at=rc.created_at.isoformat(),
            )
        )
        by_section["reports"] += 1

    # ---- consultations (v3: canonical_synthesis ReportContent via ClinicalEvent) ----
    if "consultations" not in wanted:
        cons_rows: list[ReportContent] = []
    else:
        cons_q = (
            select(ReportContent)
            .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
            .where(
                ClinicalEvent.patient_id == patient.id,
                ReportContent.authority_id == "canonical_synthesis",
                ReportContent.status != "stale",
            )
            .order_by(ReportContent.created_at.desc())
            .limit(limit)
        )
        if use_prefix:
            cons_q = cons_q.where(ReportContent.title.ilike(pattern, escape="\\"))
        cons_rows = list((await db.execute(cons_q)).scalars().all())
    for rc in cons_rows:
        items.append(
            PatientSearchItem(
                section="consultations",
                id=str(rc.id),
                title=rc.title or "(sintesi senza titolo)",
                preview=_preview(rc.narrative_md),
                rank=1.0,
                created_at=rc.created_at.isoformat(),
            )
        )
        by_section["consultations"] += 1

    # ---- folders ----
    # Only patient-scoped folders (those that live inside the
    # fascicolo) are eligible mention targets. Personal-workspace
    # folders carry ``patient_id IS NULL`` and are user-private — they
    # must not surface in another fascicolo's autocomplete.
    if "folders" not in wanted:
        folders: list[Folder] = []
    else:
        folder_q = (
            select(Folder)
            .where(Folder.patient_id == patient.id)
            .order_by(Folder.created_at.desc())
            .limit(limit)
        )
        if use_prefix:
            folder_q = folder_q.where(Folder.name.ilike(pattern, escape="\\"))
        folders = (await db.execute(folder_q)).scalars().all()
    for f in folders:
        items.append(
            PatientSearchItem(
                section="folders",
                id=str(f.id),
                title=f.name,
                preview=_preview(f.description),
                rank=1.0,
                created_at=f.created_at.isoformat(),
            )
        )
        by_section["folders"] += 1

    items.sort(key=lambda it: it.created_at, reverse=True)
    paged = items[:limit]

    return PatientSearchOut(
        patient_id=str(patient.id),
        query=needle,
        total=len(items),
        by_section=by_section,
        items=paged,
    )


@router.get("/patients/{patient_id}/shares", response_model=list[ShareLinkOut])
async def list_patient_shares(
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> list[ShareLinkOut]:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    if not (
        user.is_admin
        or patient.managed_by_subject_id == user.subject_id
        or patient.self_user_subject_id == user.subject_id
    ):
        raise HTTPException(status_code=403, detail="only the owner can view shares")

    rows = (
        await db.execute(
            select(ShareLink, Grant)
            .join(Grant, Grant.id == ShareLink.grant_id)
            .where(
                Grant.resource_kind == "patient",
                Grant.resource_id == patient.id,
            )
            .order_by(ShareLink.created_at.desc())
        )
    ).all()
    return [_link_out(link, grant) for link, grant in rows]
