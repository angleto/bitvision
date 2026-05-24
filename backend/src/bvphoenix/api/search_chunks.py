"""HTTP endpoint for hybrid sub-document chunk search.

``GET /api/patients/{patient_id}/search/chunks?q=...&k=...&...``
exposes :func:`bvphoenix.services.chunk_search.search_chunks` over
HTTP. The endpoint is patient-scoped: ``patient_id`` is taken from
the path and forced into the SQL predicate, never from the query
string. Filters mirror the service: ``source_kind``, ``author_kind``,
``exclude_ai``, ``authority_id``, ``document_kind_id``, ``since``,
``until``, ``source_id``.

No S3 keys, no bucket names: the response references chunks by their
opaque ids (``chunk_id``, ``source_id``); the caller resolves them
via the existing patient-scoped APIs.

Used by:
* The MCP tool ``search_text_chunks`` (BYO agents).
* The frontend ``PatientAskPanel`` when the freemium tier needs raw
  retrieval results without LLM synthesis.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user
from bvphoenix.db.models import (
    CHUNK_AUTHOR_KINDS,
    CHUNK_SOURCE_KINDS,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.services.chunk_search import ChunkHit, search_chunks
from bvphoenix.services.permissions import get_patient_or_404

router = APIRouter(tags=["search"])


class ChunkHitOut(BaseModel):
    chunk_id: str
    source_kind: str
    source_id: str
    page: int | None = None
    excerpt: str
    score: float
    author_kind: str
    authority_id: str | None = None
    document_kind_id: str | None = None


class ChunkSearchOut(BaseModel):
    q: str
    k: int
    patient_id: str
    hits: list[ChunkHitOut]


def _hit_to_out(h: ChunkHit) -> ChunkHitOut:
    return ChunkHitOut(
        chunk_id=str(h.chunk_id),
        source_kind=h.source_kind,
        source_id=str(h.source_id),
        page=h.page,
        excerpt=h.excerpt,
        score=h.score,
        author_kind=h.author_kind,
        authority_id=h.authority_id,
        document_kind_id=h.document_kind_id,
    )


@router.get(
    "/patients/{patient_id}/search/chunks",
    response_model=ChunkSearchOut,
)
async def search_patient_chunks(
    patient_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    q: str = Query(min_length=1, max_length=512),
    k: int = Query(8, ge=1, le=20),
    source_kind: list[str] | None = Query(default=None),
    author_kind: list[str] | None = Query(default=None),
    exclude_ai: bool = Query(default=False),
    authority_id: list[str] | None = Query(default=None),
    document_kind_id: list[str] | None = Query(default=None),
    since: date | None = Query(default=None),
    until: date | None = Query(default=None),
    source_id: uuid.UUID | None = Query(default=None),
) -> ChunkSearchOut:
    """Hybrid (vector + FTS) chunk search for one patient."""

    # Same layered access gate the patient API uses: agent token
    # scope check + human visibility check. 404 on any failure.
    await get_patient_or_404(db, patient_id=patient_id, user=user, request=request)

    if source_kind is not None:
        for v in source_kind:
            if v not in CHUNK_SOURCE_KINDS:
                raise HTTPException(status_code=400, detail=f"invalid source_kind={v!r}")
    if author_kind is not None:
        for v in author_kind:
            if v not in CHUNK_AUTHOR_KINDS:
                raise HTTPException(status_code=400, detail=f"invalid author_kind={v!r}")

    hits = await search_chunks(
        db,
        patient_id=patient_id,
        query=q,
        k=k,
        source_kind=source_kind,
        author_kind=author_kind,
        exclude_ai=exclude_ai,
        authority_id=authority_id,
        document_kind_id=document_kind_id,
        since=since,
        until=until,
        source_id=source_id,
    )
    return ChunkSearchOut(
        q=q,
        k=k,
        patient_id=str(patient_id),
        hits=[_hit_to_out(h) for h in hits],
    )


__all__ = ["router"]
