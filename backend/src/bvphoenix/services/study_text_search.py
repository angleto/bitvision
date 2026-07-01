"""Study-projected dense-text retrieval — the multilingual semantic arm.

The 4th arm of ``/api/search/hybrid`` (task fed1a35a, Phase 2). It encodes
the query with the ACTIVE registry text model (MiniLM / BGE-M3, the same
encoder ``services.chunk_search`` uses), runs an ANN over the coarse
whole-object text vectors that map to a study
(``target_kind IN ('report_content','finding')``), projects each hit to its
study, and returns a study-id ranking for RRF fusion.

Unlike the lexical ``text`` arm (tsvector over descriptions) this arm is
multilingual and semantic: "neoplasia epatica" can rank a study whose report
reads "hepatic tumour" with no shared token — recall the IT/EN thesaurus
alone cannot reach. It is orthogonal to the BiomedCLIP image arm and needs no
BiomedCLIP / inference-svc.

Coverage: coarse ``report_content`` / ``finding`` vectors exist only where the
write path fired (or a ``bvphoenix-backfill embed-text`` run filled them). The
public OpenData studies carry no reports/findings, so this arm contributes to a
user's OWN corpus, not anonymous browse. It degrades to an empty contribution
(never raises) when the registry has no routed text model, the encoder can't
load, or the store is unprovisioned.

Visibility is enforced in the projection step: every returned study is
intersected with the caller-visible set, so the arm can never surface a study
the caller could not already read.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Finding, ImagingStudy, ReportContent
from bvphoenix.services.chunk_search import _embed_query, _vec_literal
from bvphoenix.services.embedding_models import get_default_text_spec
from bvphoenix.services.text_models import BGE_M3_MODEL_ID, MULTILINGUAL_MODEL_ID
from bvphoenix.services.vector_search import tune_vector_query

logger = logging.getLogger(__name__)

# Coarse target kinds that map to exactly one study:
#   report_content -> study via the report's clinical_event
#                     (imaging_studies.clinical_event_id is 1:1)
#   finding        -> Finding.study_id directly
# Other coarse kinds (document, patient) don't resolve to a single study.
_STUDY_TARGET_KINDS = ("report_content", "finding")


async def _embed_active_query(model_id: str, q: str) -> list[float] | None:
    """Dense-encode ``q`` with the active text model's encoder.

    Reuses chunk_search's MiniLM encoder and the BGE-M3 dense encoder — no
    new encoder code. Returns ``None`` (→ the arm degrades to empty) when the
    model runtime isn't importable or the id has no encoder registered.
    """
    try:
        if model_id == BGE_M3_MODEL_ID:
            from bvphoenix.services.bge_m3 import embed_query_dense

            return await embed_query_dense(q)
        if model_id == MULTILINGUAL_MODEL_ID:
            return await _embed_query(q)
    except (ImportError, ModuleNotFoundError):
        return None
    logger.warning("study-text-dense: model %r has no query encoder; arm skipped", model_id)
    return None


async def text_dense_study_ids(
    db: AsyncSession,
    *,
    q: str,
    k: int,
    visible_ids_sq,
) -> list[uuid.UUID]:
    """Return studies ranked by dense-text similarity to ``q`` (best first).

    Two steps: (1) ANN over the active text store for the study-mappable coarse
    kinds; (2) project the hits to studies and intersect with the caller-visible
    set (``visible_ids_sq``). Empty on no routed model, no encoder, an
    unprovisioned store, or no matches.
    """
    try:
        spec = await get_default_text_spec(db)
    except Exception:
        # A failed registry read can abort the tx; roll back so the caller's
        # outer statement runs clean. Mirrors chunk_search's degrade path.
        await db.rollback()
        return []
    if spec is None:
        return []

    dense_vec = await _embed_active_query(spec.model_id, q)
    if not dense_vec:
        return []

    # Wider slab than k: the coarse text corpus is small (a user's reports /
    # findings), so an unscoped ANN followed by a visibility filter is correct
    # and cheap. If that corpus ever grows large, push the scope into the vector
    # query. ``spec.store_table`` is identifier-validated registry data.
    slab = k * 6
    kinds = ",".join(f"'{kind}'" for kind in _STUDY_TARGET_KINDS)
    ann_sql = text(
        f"""
        SELECT te.target_kind, te.target_id, te.vector <=> (:vec)::vector AS distance
        FROM {spec.store_table} te
        WHERE te.model_id = :model_id
          AND te.target_kind IN ({kinds})
        ORDER BY distance ASC
        LIMIT :slab
        """
    )
    await tune_vector_query(db, k=slab, filtered=True)
    try:
        cand_rows = (
            await db.execute(
                ann_sql,
                {"vec": _vec_literal(dense_vec), "model_id": spec.model_id, "slab": slab},
            )
        ).all()
    except ProgrammingError:
        await db.rollback()
        return []
    if not cand_rows:
        return []

    dist_by_target: dict[tuple[str, uuid.UUID], float] = {}
    rc_ids: list[uuid.UUID] = []
    fn_ids: list[uuid.UUID] = []
    for kind, target_id, distance in cand_rows:
        dist_by_target[(kind, target_id)] = float(distance)
        if kind == "report_content":
            rc_ids.append(target_id)
        elif kind == "finding":
            fn_ids.append(target_id)

    study_dist: dict[uuid.UUID, float] = {}

    def _accumulate(study_id: uuid.UUID, kind: str, target_id: uuid.UUID) -> None:
        d = dist_by_target.get((kind, target_id))
        if d is None:
            return
        prev = study_dist.get(study_id)
        if prev is None or d < prev:
            study_dist[study_id] = d

    if rc_ids:
        rows = (
            await db.execute(
                select(ImagingStudy.id, ReportContent.id)
                .select_from(ReportContent)
                .join(
                    ImagingStudy,
                    ImagingStudy.clinical_event_id == ReportContent.clinical_event_id,
                )
                .where(
                    ReportContent.id.in_(rc_ids),
                    ImagingStudy.id.in_(visible_ids_sq),
                )
            )
        ).all()
        for study_id, rc_id in rows:
            _accumulate(study_id, "report_content", rc_id)

    if fn_ids:
        rows = (
            await db.execute(
                select(Finding.study_id, Finding.id).where(
                    Finding.id.in_(fn_ids),
                    Finding.study_id.in_(visible_ids_sq),
                )
            )
        ).all()
        for study_id, fn_id in rows:
            _accumulate(study_id, "finding", fn_id)

    return sorted(study_dist, key=lambda sid: study_dist[sid])[:k]
