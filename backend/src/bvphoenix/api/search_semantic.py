"""Semantic text-to-anything search.

``GET /api/search/semantic`` embeds a natural-language query with either
BiomedCLIP (the image-and-text model used for radiology similarity) or
MiniLM (a lightweight general-purpose text encoder), then retrieves the
nearest ``k`` rows from the matching pgvector table and filters the
results by the caller's visibility set.

Design notes
------------
* **Two embedding spaces.** BiomedCLIP writes into the main
  ``embeddings`` table (512-dim, shared with image embeddings so
  text-to-image cross-modal search works). MiniLM writes into a
  separate ``text_embeddings`` table (384-dim) — that table is
  provisioned in a later migration; the endpoint degrades to an empty
  result set if the table is missing rather than 500-ing.
* **Cache.** Every query hashes to ``sha256(q.lower().strip()|model)``
  and hits Redis before doing any model work. 24h TTL.
* **Lazy model load.** The worker does image embeddings in arq, but we
  need synchronous text embedding here — load the encoders on first
  call (cached in module globals) and run the blocking forward pass
  in a thread so the event loop stays responsive. ``asyncio.to_thread``
  is the cheapest way to do this without pulling in a task queue.
* **Visibility.** ``target_kind`` rows that ultimately resolve to a
  study are filtered through ``visible_studies_filter`` the same way
  ``/search`` does. Patient-scoped kinds (``consultation``,
  ``document``, ``patient``) go through ``visible_patients_filter``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user
from bvphoenix.db.models import (
    Document,
    Embedding,
    ImagingStudy,
    Patient,
    Series,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.services.permissions import (
    visible_patients_filter,
    visible_studies_filter,
)
from bvphoenix.services.query_embed_cache import (
    cache_query_embedding,
    get_cached_query_embedding,
)
from bvphoenix.services.rate_limit import SEARCH_SEMANTIC_LIMIT, limiter

router = APIRouter(tags=["search"])

# Model IDs as stored in the embeddings.model_id column.
_BIOMEDCLIP_MODEL_ID = "biomedclip-v1"
_MINILM_MODEL_ID = "minilm-l6-v2"

# Only the MiniLM dimension is needed as a constant — it's what routes
# queries to the separate 384-dim ``text_embeddings`` table.
_MINILM_DIM = 384

# ---- Lazy-loaded text encoders ----

_biomedclip_model: Any | None = None
_biomedclip_tokenizer: Any | None = None
_minilm_model: Any | None = None


def _ensure_biomedclip() -> None:
    """Load BiomedCLIP text tower on first call. Cached in globals."""
    global _biomedclip_model, _biomedclip_tokenizer
    if _biomedclip_model is not None:
        return
    try:
        import open_clip  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — optional AI extra
        raise HTTPException(
            status_code=503,
            detail="BiomedCLIP dependencies not installed — install with extras=ai",
        ) from exc
    model, _preprocess_train, _preprocess_val = open_clip.create_model_and_transforms(
        "hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224"
    )
    tokenizer = open_clip.get_tokenizer(
        "hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224"
    )
    model.eval()
    _biomedclip_model = model
    _biomedclip_tokenizer = tokenizer


def _ensure_minilm() -> None:
    """Load sentence-transformers MiniLM on first call."""
    global _minilm_model
    if _minilm_model is not None:
        return
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — optional AI extra
        raise HTTPException(
            status_code=503,
            detail="sentence-transformers not installed — install with extras=ai",
        ) from exc
    _minilm_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")


def _embed_biomedclip_sync(query: str) -> list[float]:
    """Blocking BiomedCLIP text-encoder forward pass. Run in a thread."""
    import torch  # local import — only needed when model is used

    _ensure_biomedclip()
    assert _biomedclip_model is not None and _biomedclip_tokenizer is not None
    tokens = _biomedclip_tokenizer([query])
    with torch.no_grad():
        features = _biomedclip_model.encode_text(tokens)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.squeeze(0).tolist()


def _embed_minilm_sync(query: str) -> list[float]:
    """Blocking MiniLM forward pass. Run in a thread."""
    _ensure_minilm()
    assert _minilm_model is not None
    # SentenceTransformer.encode already L2-normalises when asked and
    # returns a numpy array; convert to plain float list for pgvector.
    import numpy as np

    arr = _minilm_model.encode([query], normalize_embeddings=True)
    vec = np.asarray(arr[0], dtype=float).tolist()
    return vec


async def embed_query_biomedclip(q: str) -> list[float]:
    """Produce a 512-dim L2-normalised text embedding for ``q``."""
    return await asyncio.to_thread(_embed_biomedclip_sync, q)


async def embed_query_minilm(q: str) -> list[float]:
    """Produce a 384-dim L2-normalised text embedding for ``q``."""
    return await asyncio.to_thread(_embed_minilm_sync, q)


async def _embed_with_cache(q: str, model: str) -> tuple[list[float], bool]:
    """Return ``(vector, cache_hit)`` — check Redis, embed on miss, backfill."""
    cached = await get_cached_query_embedding(q, model)
    if cached is not None:
        return cached, True
    if model == "biomedclip":
        vector = await embed_query_biomedclip(q)
    else:
        vector = await embed_query_minilm(q)
    await cache_query_embedding(q, model, vector)
    return vector, False


# ---- Response schema ----


class SemanticHit(BaseModel):
    target_kind: str
    target_id: str
    score: float
    preview_text: str | None = None
    link: str


class SemanticSearchOut(BaseModel):
    q: str
    target: str
    model: str
    k: int
    cache_hit: bool
    items: list[SemanticHit]


# ---- pgvector literal helper ----


def _vec_literal(vector: list[float]) -> str:
    """pgvector accepts its array literal format ``"[v1,v2,...]"``."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


async def _fetch_nearest(
    db: AsyncSession,
    *,
    table: str,
    target_kind: str,
    model_id: str,
    vector: list[float],
    limit: int,
) -> dict[uuid.UUID, float]:
    """Raw-SQL cosine-distance lookup against ``text_embeddings`` /
    ``embeddings``. Returns ``{target_id: distance}``; empty if the table
    or row set isn't provisioned yet (ProgrammingError swallowed)."""
    sql = text(
        f"SELECT target_id, vector <=> (:vec)::vector AS distance "
        f"FROM {table} "
        f"WHERE target_kind = :kind AND model_id = :model "
        f"ORDER BY distance ASC LIMIT :limit"
    )
    try:
        rows = (
            await db.execute(
                sql,
                {
                    "vec": _vec_literal(vector),
                    "kind": target_kind,
                    "model": model_id,
                    "limit": limit,
                },
            )
        ).all()
    except ProgrammingError:
        await db.rollback()
        return {}
    return {r[0]: float(r[1]) for r in rows}


# ---- Target-specific candidate resolution ----


async def _candidates_series(
    db: AsyncSession,
    user: User | None,
    vector: list[float],
    model_id: str,
    k: int,
) -> list[SemanticHit]:
    """Series-target hits come straight from the ``embeddings`` table,
    filtered to studies the caller can see."""
    # Over-fetch to account for visibility trimming.
    rows = (
        await db.execute(
            select(
                Embedding.target_id,
                Embedding.vector.cosine_distance(vector).label("distance"),
            )
            .where(
                Embedding.target_kind == "series",
                Embedding.model_id == model_id,
            )
            .order_by("distance")
            .limit(k * 4)
        )
    ).all()
    if not rows:
        return []
    dist_map = {r[0]: float(r[1]) for r in rows}

    visible_studies = await visible_studies_filter(db, user)
    visible_study_ids = visible_studies.with_only_columns(ImagingStudy.id).subquery()

    series_rows = (
        await db.execute(
            select(Series.id, Series.study_id, Series.series_description)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(
                Series.id.in_(list(dist_map.keys())),
                ImagingStudy.id.in_(visible_study_ids.select()),
            )
        )
    ).all()

    results: list[SemanticHit] = []
    for series_id, study_id, desc in series_rows:
        dist = dist_map.get(series_id, 999.0)
        results.append(
            SemanticHit(
                target_kind="series",
                target_id=str(series_id),
                score=round(1.0 - dist, 4),
                preview_text=desc,
                link=f"/api/studies/{study_id}/series/{series_id}",
            )
        )
    results.sort(key=lambda h: h.score, reverse=True)
    return results[:k]


async def _candidates_report(
    db: AsyncSession,
    user: User | None,
    vector: list[float],
    model_id: str,
    k: int,
    dim: int,
) -> list[SemanticHit]:
    """v3 phase 3b: legacy 'report' target_kind retired.

    The Expression layer (ReportContent) already handles original /
    derived narratives and is searched via _candidates_consultation
    (which now indexes target_kind='report_content'). Returning empty
    here keeps the dispatcher happy while the embedding-reindex job
    flips the existing rows from 'report' → 'report_content'."""
    return []


async def _candidates_consultation(
    db: AsyncSession,
    user: User | None,
    vector: list[float],
    model_id: str,
    k: int,
    dim: int,
) -> list[SemanticHit]:
    """Canonical syntheses are clinical-event-scoped, but the patient
    visibility filter still applies via the parent ClinicalEvent."""
    from bvphoenix.db.models import ClinicalEvent, ReportContent

    table = "text_embeddings" if dim == _MINILM_DIM else "embeddings"
    # v3 indexed under the new target_kind ``report_content`` rather
    # than the legacy ``consultation`` (the two were folded together
    # in phase 3a). The reindex job that flips the existing rows
    # ships in phase 4.
    dist_map = await _fetch_nearest(
        db,
        table=table,
        target_kind="report_content",
        model_id=model_id,
        vector=vector,
        limit=k * 4,
    )
    if not dist_map:
        return []

    visible_patients = await visible_patients_filter(db, user)
    visible_patient_ids_subq = visible_patients.with_only_columns(Patient.id).subquery()

    rc_rows = (
        await db.execute(
            select(
                ReportContent.id,
                ClinicalEvent.patient_id,
                ReportContent.title,
                ReportContent.narrative_md,
            )
            .join(ClinicalEvent, ClinicalEvent.id == ReportContent.clinical_event_id)
            .where(
                ReportContent.id.in_(list(dist_map.keys())),
                ReportContent.authority_id == "canonical_synthesis",
                ClinicalEvent.patient_id.in_(visible_patient_ids_subq.select()),
            )
        )
    ).all()

    results: list[SemanticHit] = []
    for rc_id, _patient_id, title, narrative_md in rc_rows:
        dist = dist_map.get(rc_id, 999.0)
        preview = (narrative_md or title or "")[:240] or None
        results.append(
            SemanticHit(
                target_kind="consultation",
                target_id=str(rc_id),
                score=round(1.0 - dist, 4),
                preview_text=preview,
                link=f"/api/report-contents/{rc_id}",
            )
        )
    results.sort(key=lambda h: h.score, reverse=True)
    return results[:k]


async def _candidates_document(
    db: AsyncSession,
    user: User | None,
    vector: list[float],
    model_id: str,
    k: int,
    dim: int,
) -> list[SemanticHit]:
    """Document — patient-scoped visibility."""
    table = "text_embeddings" if dim == _MINILM_DIM else "embeddings"
    dist_map = await _fetch_nearest(
        db, table=table, target_kind="document", model_id=model_id, vector=vector, limit=k * 4
    )
    if not dist_map:
        return []

    visible_patients = await visible_patients_filter(db, user)
    visible_patient_ids_subq = visible_patients.with_only_columns(Patient.id).subquery()

    doc_rows = (
        await db.execute(
            select(
                Document.id,
                Document.patient_id,
                Document.title,
                Document.text,
            ).where(
                Document.id.in_(list(dist_map.keys())),
                Document.patient_id.in_(visible_patient_ids_subq.select()),
            )
        )
    ).all()

    results: list[SemanticHit] = []
    for doc_id, patient_id, title, body_text in doc_rows:
        dist = dist_map.get(doc_id, 999.0)
        preview = ((body_text or title) or "")[:240] or None
        results.append(
            SemanticHit(
                target_kind="document",
                target_id=str(doc_id),
                score=round(1.0 - dist, 4),
                preview_text=preview,
                link=f"/api/patients/{patient_id}/documents/{doc_id}",
            )
        )
    results.sort(key=lambda h: h.score, reverse=True)
    return results[:k]


async def _candidates_patient(
    db: AsyncSession,
    user: User | None,
    vector: list[float],
    model_id: str,
    k: int,
    dim: int,
) -> list[SemanticHit]:
    """Patients — embeddings typically computed over patient summary text."""
    table = "text_embeddings" if dim == _MINILM_DIM else "embeddings"
    dist_map = await _fetch_nearest(
        db, table=table, target_kind="patient", model_id=model_id, vector=vector, limit=k * 4
    )
    if not dist_map:
        return []

    visible_patients = await visible_patients_filter(db, user)
    visible_patient_ids_subq = visible_patients.with_only_columns(Patient.id).subquery()

    pat_rows = (
        await db.execute(
            select(
                Patient.id,
                Patient.display_name,
                Patient.notes,
            ).where(
                Patient.id.in_(list(dist_map.keys())),
                Patient.id.in_(visible_patient_ids_subq.select()),
            )
        )
    ).all()

    results: list[SemanticHit] = []
    for pat_id, display_name, notes in pat_rows:
        dist = dist_map.get(pat_id, 999.0)
        preview = (display_name or notes or "")[:240] or None
        results.append(
            SemanticHit(
                target_kind="patient",
                target_id=str(pat_id),
                score=round(1.0 - dist, 4),
                preview_text=preview,
                link=f"/api/patients/{pat_id}",
            )
        )
    results.sort(key=lambda h: h.score, reverse=True)
    return results[:k]


# ---- Endpoint ----


@router.get("/search/semantic", response_model=SemanticSearchOut)
@limiter.limit(SEARCH_SEMANTIC_LIMIT)
async def search_semantic(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    q: str = Query(..., min_length=1, max_length=512, description="Natural-language query"),
    target: Literal["series", "report", "consultation", "document", "patient"] = Query(
        ..., description="Kind of artefact to search."
    ),
    model: Literal["biomedclip", "minilm"] = Query(
        "biomedclip", description="Embedding model for the query."
    ),
    k: int = Query(20, ge=1, le=100),
) -> SemanticSearchOut:
    """Embed ``q`` with the chosen model and return the k nearest
    ``target`` rows by cosine distance. Visibility-filtered."""
    # Embed *before* touching the DB so a cold-start model load doesn't
    # monopolise a connection.
    vector, cache_hit = await _embed_with_cache(q, model)

    # Cap the rest of the transaction at 3s — a pathological k combined
    # with a missing index can chew through a worker. 3s leaves headroom
    # for the HNSW scan on realistic corpora.
    await db.execute(text("SET LOCAL statement_timeout = '3s'"))

    if model == "biomedclip":
        model_id = _BIOMEDCLIP_MODEL_ID
        dim = len(vector)
    else:
        model_id = _MINILM_MODEL_ID
        dim = _MINILM_DIM

    if target == "series":
        # Series embeddings only exist for biomedclip today.
        if model == "minilm":
            items: list[SemanticHit] = []
        else:
            items = await _candidates_series(db, user, vector, model_id, k)
    elif target == "report":
        items = await _candidates_report(db, user, vector, model_id, k, dim)
    elif target == "consultation":
        items = await _candidates_consultation(db, user, vector, model_id, k, dim)
    elif target == "document":
        items = await _candidates_document(db, user, vector, model_id, k, dim)
    elif target == "patient":
        items = await _candidates_patient(db, user, vector, model_id, k, dim)
    else:  # pragma: no cover — validated above
        items = []

    return SemanticSearchOut(q=q, target=target, model=model, k=k, cache_hit=cache_hit, items=items)
