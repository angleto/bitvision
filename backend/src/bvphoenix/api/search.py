"""Search API — metadata + full-text + similarity.

`q` performs a full-text query against study and series descriptions
using the dual-config (italian || simple) generated tsvector columns
and their GIN indexes (migration 0010). Combine with structured filters
(``modality``, ``body_part``, date range) for precise scoping.

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
from bvphoenix.api.search_hybrid import SERIES_EMBED_MODEL_ID
from bvphoenix.auth import optional_user
from bvphoenix.db.models import Embedding, ImagingStudy, Series, Tag, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.problem_details import problem
from bvphoenix.services.embedding_status import indexed_study_ids
from bvphoenix.services.mmr import MMRCandidate, mmr_rerank
from bvphoenix.services.permissions import (
    READ_METADATA,
    apply_scope_filter,
    can,
    visible_studies_filter,
)
from bvphoenix.services.rate_limit import SEARCH_LIMIT, limiter
from bvphoenix.services.thesaurus import expand_tsquery
from bvphoenix.services.vector_search import tune_vector_query

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
    include_index_status: bool = Query(
        False,
        description=(
            "If true, set ``indexed`` on each item (has a BiomedCLIP image "
            "vector → can anchor /similar-to). One extra index-backed query "
            "over the page. Used by the Visual Search picker."
        ),
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

    # Expand acronyms / bilingual synonyms (TC -> CT -> tomografia ...) on
    # the FTS side only; falls back to plain dual-config FTS when the
    # thesaurus is empty (see services/thesaurus.py).
    ts_query = expand_tsquery(q) if q else None

    if ts_query is not None:
        # Full-text on study + (joined) series descriptions. The match is
        # against the dual-config (italian || simple) generated tsvector
        # columns so the GIN indexes from migration 0009 are used and
        # both stemmed Italian terms and exact acronyms hit.
        base = base.outerjoin(Series, Series.study_id == ImagingStudy.id).where(
            or_(
                ImagingStudy.study_description_tsv.op("@@")(ts_query),
                Series.series_description_tsv.op("@@")(ts_query),
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
    has_snippet = ts_query is not None

    # Extra select columns, appended in a fixed order so the row tuple is
    # predictable: [snippet?, rank?]. ImagingStudy is always column 0.
    extra_cols = []
    if has_snippet:
        # ts_headline highlights the matched terms in the raw description.
        # Computed on the final page only (it re-parses text per row), so
        # it never touches the candidate slab.
        extra_cols.append(
            func.ts_headline(
                "italian",
                func.coalesce(ImagingStudy.study_description, ""),
                ts_query,
                "StartSel=<mark>, StopSel=</mark>, MaxWords=18, MinWords=5, MaxFragments=2",
            ).label("snippet")
        )

    if ranked:
        # Recency decay keeps the factor in (0, 1] so it never
        # dominates the text relevance score.
        age_days = func.extract("epoch", func.now() - ImagingStudy.created_at) / literal(86400.0)
        decay = literal(1.0) / (literal(1.0) + age_days / literal(365.0))
        rank_expr = (func.ts_rank_cd(ImagingStudy.study_description_tsv, ts_query) * decay).label(
            "rank"
        )
        extra_cols.append(rank_expr)
        items_query = items_query.order_by(rank_expr.desc(), ImagingStudy.created_at.desc())
    elif effective_sort == "oldest":
        items_query = items_query.order_by(ImagingStudy.created_at.asc())
    else:
        items_query = items_query.order_by(ImagingStudy.created_at.desc())

    if extra_cols:
        items_query = items_query.add_columns(*extra_cols)
    items_query = items_query.limit(limit).offset(offset)

    items_result = await db.execute(items_query)
    out_items: list[StudyOut] = []
    if extra_cols:
        # add_columns makes each row a tuple; study is column 0, snippet
        # (if requested) is column 1.
        for row in items_result.all():
            study_out = StudyOut.model_validate(row[0])
            if has_snippet:
                study_out.snippet = row[1]
            out_items.append(study_out)
    else:
        out_items = [StudyOut.model_validate(s) for s in items_result.scalars().all()]

    if include_index_status and out_items:
        # One bounded, index-backed query over just this page's study ids —
        # cheaper than a correlated EXISTS per row and it never perturbs the
        # main query's plan. Opt-in so the general /search path pays nothing.
        indexed = await indexed_study_ids(db, [s.id for s in out_items])
        for item in out_items:
            item.indexed = item.id in indexed

    facet_payload = await _compute_facets(db, filtered_ids_subq) if facets else None

    return PaginatedStudies(
        items=out_items,
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
    diversify: bool = False,
    mmr_lambda: float = 0.7,
) -> list[SimilarStudyOut]:
    """Core similarity search, callable from both the HTTP handler and
    from in-process callers (A2A skills, MCP tools). The HTTP handler
    wraps this with a rate-limit decorator; callers that already go
    through their own quota (agent tokens, for instance) can call this
    directly without paying the IP-based budget twice.

    ``diversify`` re-ranks the per-study representatives with MMR so the
    panel does not fill with near-identical studies; it is opt-in because
    it deliberately trades the pure-distance ordering for diversity.
    """
    # Resolve the query vector — try series first, then study
    source_emb = (
        await db.execute(
            select(Embedding).where(
                Embedding.target_kind == "series",
                Embedding.target_id == target_id,
                Embedding.model_id == SERIES_EMBED_MODEL_ID,
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
                    Embedding.model_id == SERIES_EMBED_MODEL_ID,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    if source_emb is None:
        # Three distinct outcomes, NOT collapsed (the old code returned [] for
        # both not-indexed and genuine-404 to silence viewer-console 404s, which
        # made the FE NotIndexedCard branch dead and showed the misleading "no
        # similar cases / remove the modality filter" hint for the common
        # not-yet-indexed case):
        #   * id resolves to nothing -> genuine 404 (typo'd / deleted id).
        #   * id exists but has no image vector yet (async indexing, or a
        #     non-pixel SR/SEG series) -> 422 with a structured code so the FE
        #     renders a "not indexed yet" card and the viewer panel stays quiet.
        #   * indexed but zero neighbours -> handled below as 200 + [].
        target_exists = (
            await db.execute(
                select(
                    select(Series.id).where(Series.id == target_id).exists()
                    | select(ImagingStudy.id).where(ImagingStudy.id == target_id).exists()
                )
            )
        ).scalar()
        if not target_exists:
            raise HTTPException(status_code=404, detail="not found")
        # RFC 7807 problem: the machine-readable kind is the ``type`` slug
        # (".../study_not_indexed"), which the FE reads to render the
        # "not indexed yet" card and keep the viewer panel quiet.
        raise problem(
            422,
            "study_not_indexed",
            "This study is not yet indexed for visual search.",
            title="Not indexed for visual search",
        )

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

    # Restrict the ANN ranking to series the caller may see *before*
    # ordering by distance, instead of over-fetching then trimming. An
    # invisible neighbour must never consume a candidate slot (so the
    # visible top-k is not silently starved) nor leak its existence via
    # the result count. ``tune_vector_query(filtered=True)`` turns on
    # iterative scan so the k*3 slab is filled with visible rows even
    # when the caller can see only a small fraction of the corpus.
    visible_base = await visible_studies_filter(db, user)
    visible_study_ids = visible_base.with_only_columns(ImagingStudy.id).subquery()

    query_vec = source_emb.vector
    # Over-fetch wider when diversifying so MMR has material to work with.
    over = k * 5 if diversify else k * 3
    await tune_vector_query(db, k=over, filtered=True)
    candidates = (
        await db.execute(
            select(
                Embedding.target_id,
                Embedding.vector,
                Embedding.vector.cosine_distance(query_vec).label("distance"),
            )
            .join(Series, Series.id == Embedding.target_id)
            .where(
                Embedding.target_kind == "series",
                Embedding.target_id != source_emb.target_id,
                Embedding.model_id == SERIES_EMBED_MODEL_ID,
                Series.study_id.in_(select(visible_study_ids.c.id)),
            )
            .order_by("distance")
            .limit(over)
        )
    ).all()

    if not candidates:
        return []

    # Candidates are already visibility-scoped; resolve series → study
    # for hydration + the modality filter below.
    candidate_series_ids = [c[0] for c in candidates]
    distance_map = {c[0]: float(c[2]) for c in candidates}
    vector_map = {c[0]: c[1] for c in candidates}

    series_study_rows = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id.in_(candidate_series_ids))
        )
    ).all()

    # Lowest-distance representative series per study, modality-filtered,
    # best first. (similar-to returns distinct studies, one series each.)
    series_study_rows.sort(key=lambda r: distance_map.get(r[0].id, 999))
    reps: list[tuple[Series, ImagingStudy]] = []
    seen_study_ids: set[uuid.UUID] = set()
    for series, study in series_study_rows:
        if study.id in seen_study_ids:
            continue
        if modality and modality.upper() not in (study.modalities or []):
            continue
        seen_study_ids.add(study.id)
        reps.append((series, study))

    if diversify:
        rep_by_study = {study.id: (series, study) for series, study in reps}
        mmr_cands = [
            MMRCandidate(
                id=study.id,
                vector=list(vector_map[series.id]),
                relevance=max(0.0, 1.0 - distance_map.get(series.id, 0.0)),
                group=study.id,
            )
            for series, study in reps
            if vector_map.get(series.id) is not None
        ]
        ordered_reps = [rep_by_study[c.id] for c in mmr_rerank(mmr_cands, k=k, lambda_=mmr_lambda)]
    else:
        ordered_reps = reps[:k]

    results: list[SimilarStudyOut] = []
    for series, study in ordered_reps:
        # cosine_distance is in [0, 2]; clamp so a dissimilar pair never
        # surfaces a negative "similarity" score to the caller / UI.
        score = max(0.0, 1.0 - distance_map.get(series.id, 0.0))
        results.append(
            SimilarStudyOut(
                study=StudyOut.model_validate(study),
                score=round(score, 4),
                matched_series_id=str(series.id),
            )
        )

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
    diversify: bool = Query(
        False,
        description="Re-rank with MMR so the results are visually diverse "
        "instead of a cluster of near-identical studies.",
    ),
) -> list[SimilarStudyOut]:
    """Find studies with visually similar series using BiomedCLIP embeddings.

    ``target_id`` can be a series or study UUID. If it's a study, the first
    series with an embedding is used as the query vector.
    """
    return await find_similar_studies(
        db=db, user=user, target_id=target_id, k=k, modality=modality, diversify=diversify
    )
