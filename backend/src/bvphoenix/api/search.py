"""Search API — metadata + full-text + similarity.

`q` performs a full-text query against study and series descriptions
using the GIN/tsvector index installed in migration 0002. Combine
with structured filters (``modality``, ``body_part``, date range) for
precise scoping.

``/similar-to/{target_id}`` finds visually similar series using
pgvector cosine distance on BiomedCLIP embeddings.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._schemas import PaginatedStudies, StudyOut
from bvphoenix.auth import optional_user
from bvphoenix.db.models import Embedding, ImagingStudy, Series, Tag, User
from bvphoenix.db.session import get_db
from bvphoenix.services.permissions import (
    READ_METADATA,
    apply_scope_filter,
    can,
    visible_studies_filter,
)
from bvphoenix.services.rate_limit import SEARCH_LIMIT, limiter

router = APIRouter(tags=["search"])

# Cap facet bucket count per field. 20 is enough for a UI sidebar
# without inflating the response; callers that need the long tail can
# paginate through filtered searches.
FACET_TOP_N = 20


@router.get("/search", response_model=PaginatedStudies)
@limiter.limit(SEARCH_LIMIT)
async def search(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    # q capped at 128 chars — 512 was the old limit but that's enough
    # rope to build a pathological ILIKE / to_tsquery expression that
    # blows past the statement_timeout. 128 covers every legitimate
    # query we've seen (natural-language terms, radiology acronyms).
    q: str | None = Query(None, max_length=128),
    modality: str | None = Query(None, max_length=16),
    body_part: str | None = Query(None, max_length=64),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    tag: list[str] | None = Query(
        None,
        description="Repeatable. Format: 'namespace:value' (e.g. tag=anatomy:lung).",
    ),
    sort: Literal["relevance", "newest", "oldest"] | None = Query(
        None,
        description=(
            "Ordering. Defaults to 'relevance' when q is set (ts_rank_cd × "
            "recency decay), otherwise 'newest' (created_at DESC)."
        ),
    ),
    scope: Literal["all", "public", "mine"] | None = Query(
        None,
        description=(
            "Visibility scope. 'public' = OpenData library + studies marked "
            "is_public. 'mine' = studies owned by the caller. Default 'all' "
            "= everything the caller is authorised to read."
        ),
    ),
    facets: bool = Query(
        False,
        description="If true, compute per-field counts over the filtered set.",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PaginatedStudies:
    # Cap this transaction at 3s. Full-text queries usually return in
    # <50ms, but a crafted ``q`` combined with the ILIKE body-part
    # filter can force a seq scan over series — the timeout means the
    # client gets a 500 instead of tying up a worker indefinitely.
    await db.execute(text("SET LOCAL statement_timeout = '3s'"))

    # ``relevance`` requires a ``q`` to rank against; otherwise it
    # collapses to ``newest`` (which is also the default).
    effective_sort: Literal["relevance", "newest", "oldest"] = sort or (
        "relevance" if q else "newest"
    )
    if effective_sort == "relevance" and not q:
        effective_sort = "newest"

    base = await visible_studies_filter(db, user)
    # UX scope narrowing — auth filter above is the ceiling, this can
    # only restrict further (e.g. user sees grants + own + public but
    # asked for 'mine' → drop grants and OpenData).
    base = apply_scope_filter(base, scope, user)

    ts_query = func.plainto_tsquery("simple", q) if q else None

    if ts_query is not None:
        # Full-text on study + (joined) series descriptions. The
        # to_tsvector calls match the GIN indexes in migration 0002 so
        # they can be used by the planner.
        base = base.outerjoin(Series, Series.study_id == ImagingStudy.id).where(
            or_(
                func.to_tsvector("simple", func.coalesce(ImagingStudy.study_description, "")).op(
                    "@@"
                )(ts_query),
                func.to_tsvector("simple", func.coalesce(Series.series_description, "")).op("@@")(
                    ts_query
                ),
            )
        )

    if modality:
        base = base.where(ImagingStudy.modalities.any(modality.upper()))

    if body_part:
        base = base.outerjoin(Series, Series.study_id == ImagingStudy.id).where(
            Series.body_part_examined.ilike(f"%{body_part}%")
        )

    if date_from:
        base = base.where(ImagingStudy.study_date >= date_from)
    if date_to:
        base = base.where(ImagingStudy.study_date <= date_to)

    if tag:
        for raw in tag:
            if ":" not in raw:
                continue
            ns, val = raw.split(":", 1)
            base = base.where(
                ImagingStudy.id.in_(
                    select(Tag.target_id).where(
                        Tag.target_kind == "study",
                        Tag.namespace == ns,
                        Tag.value == val,
                    )
                )
            )

    # Distinct because the joins above can multiply rows. Materialize
    # the filtered study-id set once so count, facets and the item page
    # all operate on the same logical CTE.
    filtered_ids_subq = base.with_only_columns(ImagingStudy.id).distinct().subquery()

    count_query = select(func.count()).select_from(filtered_ids_subq)
    total = (await db.execute(count_query)).scalar_one()

    # Re-select ImagingStudy from the filtered id set so ranking can be
    # attached without re-joining Series (which would force DISTINCT).
    items_query = select(ImagingStudy).where(ImagingStudy.id.in_(select(filtered_ids_subq.c.id)))
    ranked = effective_sort == "relevance" and ts_query is not None

    if ranked:
        study_tsv = func.to_tsvector("simple", func.coalesce(ImagingStudy.study_description, ""))
        # Recency decay keeps the factor in (0, 1] so it never
        # dominates the text relevance score.
        age_days = func.extract("epoch", func.now() - ImagingStudy.created_at) / literal(86400.0)
        decay = literal(1.0) / (literal(1.0) + age_days / literal(365.0))
        rank_expr = (func.ts_rank_cd(study_tsv, ts_query) * decay).label("rank")
        items_query = items_query.add_columns(rank_expr).order_by(
            rank_expr.desc(), ImagingStudy.created_at.desc()
        )
    elif effective_sort == "oldest":
        items_query = items_query.order_by(ImagingStudy.created_at.asc())
    else:
        items_query = items_query.order_by(ImagingStudy.created_at.desc())

    items_query = items_query.limit(limit).offset(offset)

    items_result = await db.execute(items_query)
    if ranked:
        # add_columns makes the row a tuple; ImagingStudy is in column 0.
        studies = [row[0] for row in items_result.all()]
    else:
        studies = items_result.scalars().all()

    facet_payload = await _compute_facets(db, filtered_ids_subq) if facets else None

    return PaginatedStudies(
        items=[StudyOut.model_validate(s) for s in studies],
        total=int(total),
        limit=limit,
        offset=offset,
        facets=facet_payload,
    )


async def _compute_facets(db: AsyncSession, filtered_ids_subq) -> dict:
    """Aggregate counts over the filtered study-id set, capped to top-N per field."""
    filtered_ids = select(filtered_ids_subq.c.id)

    # modalities is a text[] on studies; unnest before counting so a
    # multi-modality study is counted once per modality.
    modality_col = func.unnest(ImagingStudy.modalities).label("modality")
    modality_rows = (
        await db.execute(
            select(modality_col, func.count().label("n"))
            .where(ImagingStudy.id.in_(filtered_ids))
            .group_by(modality_col)
            .order_by(func.count().desc())
            .limit(FACET_TOP_N)
        )
    ).all()

    # body_part lives on Series — count distinct studies so a study with
    # 10 chest series still contributes 1 to CHEST.
    body_part_rows = (
        await db.execute(
            select(
                func.upper(Series.body_part_examined).label("bp"),
                func.count(func.distinct(Series.study_id)).label("n"),
            )
            .where(
                Series.study_id.in_(filtered_ids),
                Series.body_part_examined.is_not(None),
                Series.body_part_examined != "",
            )
            .group_by(func.upper(Series.body_part_examined))
            .order_by(func.count(func.distinct(Series.study_id)).desc())
            .limit(FACET_TOP_N)
        )
    ).all()

    # Prefer clinical study_date; fall back to created_at so rows
    # without a date still appear in the histogram.
    year_expr = func.extract(
        "year", func.coalesce(ImagingStudy.study_date, func.date(ImagingStudy.created_at))
    ).label("year")
    year_rows = (
        await db.execute(
            select(year_expr, func.count().label("n"))
            .where(ImagingStudy.id.in_(filtered_ids))
            .group_by(year_expr)
            .order_by(year_expr.desc())
            .limit(FACET_TOP_N)
        )
    ).all()

    top_tag_rows = (
        await db.execute(
            select(
                Tag.namespace,
                Tag.value,
                func.count(func.distinct(Tag.target_id)).label("n"),
            )
            .where(
                Tag.target_kind == "study",
                Tag.target_id.in_(filtered_ids),
            )
            .group_by(Tag.namespace, Tag.value)
            .order_by(func.count(func.distinct(Tag.target_id)).desc())
            .limit(FACET_TOP_N)
        )
    ).all()

    return {
        "modality": {str(m): int(n) for m, n in modality_rows if m},
        "body_part": {str(bp): int(n) for bp, n in body_part_rows if bp},
        "year": {str(int(y)): int(n) for y, n in year_rows if y is not None},
        "top_tags": [{"namespace": ns, "value": v, "count": int(n)} for ns, v, n in top_tag_rows],
    }


# ---- Similarity search (pgvector) ----


class SimilarStudyOut(BaseModel):
    study: StudyOut
    score: float
    matched_series_id: str


async def find_similar_studies(
    *,
    db: AsyncSession,
    user: User | None,
    target_id: uuid.UUID,
    k: int = 10,
    modality: str | None = None,
) -> list[SimilarStudyOut]:
    """Core similarity search, callable from both the HTTP handler and
    from in-process callers (A2A skills, MCP tools). The HTTP handler
    wraps this with a rate-limit decorator; callers that already go
    through their own quota (agent tokens, for instance) can call this
    directly without paying the IP-based budget twice.
    """
    # Resolve the query vector — try series first, then study
    source_emb = (
        await db.execute(
            select(Embedding).where(
                Embedding.target_kind == "series",
                Embedding.target_id == target_id,
            )
        )
    ).scalar_one_or_none()

    if source_emb is None:
        # Maybe target_id is a study — find the first embedded series
        source_emb = (
            await db.execute(
                select(Embedding)
                .join(Series, Series.id == Embedding.target_id)
                .where(
                    Embedding.target_kind == "series",
                    Series.study_id == target_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    if source_emb is None:
        # Indexing happens asynchronously by the embedding worker after
        # ingestion. Fresh uploads, or studies whose pixel data never
        # materialised (e.g. SR-only series), won't have a vector.
        # Treat this as "no similar cases yet" rather than a hard 404:
        # the resource (the study) DOES exist, the FE always rendered
        # ``[]`` and ``404`` identically as "No similar cases found"
        # anyway, and the 404 was generating spurious red entries in
        # every viewer's DevTools console (one per page load). The
        # ``not_indexed`` distinction was never surfaced anywhere.
        return []

    # Check the user can see the source study
    source_series = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == source_emb.target_id)
        )
    ).first()
    if source_series is None:
        raise HTTPException(status_code=404, detail="source series not found")
    _, source_study = source_series
    if not await can(db, user=user, action=READ_METADATA, study=source_study):
        raise HTTPException(status_code=404, detail="not found")

    # pgvector cosine distance: <=> operator
    # Fetch k+10 candidates to account for visibility filtering
    query_vec = source_emb.vector
    candidates = (
        await db.execute(
            select(
                Embedding.target_id,
                Embedding.vector.cosine_distance(query_vec).label("distance"),
            )
            .where(
                Embedding.target_kind == "series",
                Embedding.target_id != source_emb.target_id,
            )
            .order_by("distance")
            .limit(k * 3)
        )
    ).all()

    if not candidates:
        return []

    # Resolve series → study and filter by visibility + modality
    candidate_series_ids = [c[0] for c in candidates]
    distance_map = {c[0]: float(c[1]) for c in candidates}

    visible_base = await visible_studies_filter(db, user)
    series_study_rows = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(
                Series.id.in_(candidate_series_ids),
                ImagingStudy.id.in_(
                    visible_base.with_only_columns(ImagingStudy.id).subquery().select()
                ),
            )
        )
    ).all()

    results: list[SimilarStudyOut] = []
    seen_study_ids: set[uuid.UUID] = set()

    # Sort by distance (similarity)
    series_study_rows.sort(key=lambda r: distance_map.get(r[0].id, 999))

    for series, study in series_study_rows:
        if study.id in seen_study_ids:
            continue
        if modality and modality.upper() not in (study.modalities or []):
            continue
        seen_study_ids.add(study.id)
        score = 1.0 - distance_map.get(series.id, 0)  # cosine similarity = 1 - distance
        results.append(
            SimilarStudyOut(
                study=StudyOut.model_validate(study),
                score=round(score, 4),
                matched_series_id=str(series.id),
            )
        )
        if len(results) >= k:
            break

    return results


@router.get("/similar-to/{target_id}", response_model=list[SimilarStudyOut])
@limiter.limit(SEARCH_LIMIT)
async def similar_to(
    request: Request,
    target_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    k: int = Query(10, ge=1, le=100),
    modality: str | None = Query(None, max_length=16),
) -> list[SimilarStudyOut]:
    """Find studies with visually similar series using BiomedCLIP embeddings.

    ``target_id`` can be a series or study UUID. If it's a study, the first
    series with an embedding is used as the query vector.
    """
    return await find_similar_studies(db=db, user=user, target_id=target_id, k=k, modality=modality)
