"""Hybrid study search — tag + full-text + semantic fused with RRF.

``GET /api/search/hybrid?q=<text>&k=<int>&weights=tag:2,text:1,image:2``

The endpoint fans out three independent ranked queries against the
same working set of studies the caller is allowed to see:

1. **tag** — split ``q`` into keywords, look them up in ``tags``
   (case-insensitive LIKE on ``namespace:value``), count how many
   distinct matching tags each study has, return top 50 ordered by
   that count descending.
2. **text** — Postgres ``ts_rank_cd`` of the dual-config
   (italian || simple) query over the generated tsvector columns on
   study + series descriptions (their GIN indexes, migration 0010). Top 50.
3. **image** — BiomedCLIP text encoder on ``q`` → pgvector cosine
   similarity against series-level image embeddings
   (``model_id='biomedclip-v1'``, as written by the embed_series worker).

Each list is fed into Reciprocal Rank Fusion
(``services.rrf.rrf_fuse``) with the caller-supplied weights. The
result is the top-k studies by fused score plus the per-signal
contributions for UI display / debugging.

Failure isolation: any individual signal that raises is logged as a
warning and contributes an empty list to the fusion — the endpoint
never surfaces ML infra errors to the caller.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._schemas import StudyOut
from bvphoenix.auth import optional_user
from bvphoenix.db.models import Embedding, ImagingStudy, Series, Tag, User
from bvphoenix.db.session import get_db
from bvphoenix.services.permissions import apply_scope_filter, visible_studies_filter
from bvphoenix.services.rate_limit import SEARCH_LIMIT, limiter
from bvphoenix.services.rrf import rrf_fuse, rrf_signal_contribution
from bvphoenix.services.thesaurus import expand_tsquery
from bvphoenix.services.vector_search import tune_vector_query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

# RRF damping constant — see services/rrf.py docstring.
RRF_K = 60

# Per-signal candidate count. Wider than the default k=20 so the fusion
# has enough overlap between lists to find meaningful intersections.
PER_SIGNAL_LIMIT = 50

# model_id under which the series image embeddings are stored. The
# indexer (workers/embed_series.py) writes ``biomedclip-v1``; the
# registry *names* the same model ``biomedclip-image-v1`` but that name
# is not what lands in ``embeddings.model_id``. Querying a single id
# (rather than an ``IN`` over several) avoids ever fusing two distinct
# embedding spaces into one cosine ranking.
SERIES_EMBED_MODEL_ID = "biomedclip-v1"


# ---- Response shape --------------------------------------------------------


class HybridSignalScores(BaseModel):
    tag: float
    text: float
    image: float


class HybridSearchItem(BaseModel):
    study: StudyOut
    score: float
    signals: HybridSignalScores


class HybridSearchOut(BaseModel):
    items: list[HybridSearchItem]
    weights_used: dict[str, float]
    query: str


# ---- Weights parser --------------------------------------------------------


DEFAULT_WEIGHTS = {"tag": 2.0, "text": 1.0, "image": 2.0}
_ALLOWED_SIGNALS = frozenset(DEFAULT_WEIGHTS)


def _parse_weights(raw: str | None) -> dict[str, float]:
    """Parse ``tag:2,text:1,image:2`` into ``{'tag':2.0,...}``.

    Unknown signals are silently dropped; missing signals inherit the
    default. Malformed entries (non-float value, missing colon) are
    logged and skipped rather than 400'd — the endpoint prefers a
    degraded answer to a hard failure.
    """
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    parsed: dict[str, float] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        name, value = chunk.split(":", 1)
        name = name.strip().lower()
        if name not in _ALLOWED_SIGNALS:
            continue
        try:
            parsed[name] = float(value.strip())
        except ValueError:
            logger.warning("hybrid-search: ignoring non-numeric weight %r", chunk)
            continue
    # Merge with defaults so every signal has a weight.
    merged = dict(DEFAULT_WEIGHTS)
    merged.update(parsed)
    return merged


# ---- Per-signal retrieval --------------------------------------------------


_KEYWORD_RE = re.compile(r"[A-Za-z0-9]{2,}")


def _extract_keywords(q: str) -> list[str]:
    """Split ``q`` into lowercase alphanumeric tokens of length ≥ 2.

    Single characters are dropped because they produce noisy LIKE
    patterns against the tag table.
    """
    return [m.group(0).lower() for m in _KEYWORD_RE.finditer(q)]


async def _tag_signal(
    db: AsyncSession,
    *,
    q: str,
    visible_ids_sq,
) -> list[uuid.UUID]:
    """Rank studies by number of distinct tag rows matching ``q``.

    Returns an ordered list (best first) of at most ``PER_SIGNAL_LIMIT``
    study ids. Empty on no keywords or no matches.
    """
    keywords = _extract_keywords(q)
    if not keywords:
        return []
    # Case-insensitive LIKE on both namespace and value so "lung"
    # matches ``anatomy:lung`` (value) and "anatomy" matches the
    # namespace itself.
    clauses = []
    for kw in keywords:
        pattern = f"%{kw}%"
        clauses.append(Tag.namespace.ilike(pattern))
        clauses.append(Tag.value.ilike(pattern))
    match_count = func.count(func.distinct(Tag.id)).label("match_count")
    stmt = (
        select(Tag.target_id, match_count)
        .where(
            Tag.target_kind == "study",
            Tag.target_id.in_(visible_ids_sq),
            or_(*clauses),
        )
        .group_by(Tag.target_id)
        .order_by(match_count.desc())
        .limit(PER_SIGNAL_LIMIT)
    )
    rows = (await db.execute(stmt)).all()
    return [row[0] for row in rows]


async def _text_signal(
    db: AsyncSession,
    *,
    q: str,
    visible_ids_sq,
) -> list[uuid.UUID]:
    """Rank studies by Postgres ``ts_rank_cd`` on descriptions.

    Mirrors the tsvector expressions used by the existing ``/search``
    endpoint so we hit the same GIN indexes.
    """
    ts_query = expand_tsquery(q)
    # Dual-config (italian || simple) generated columns; see services/fts.py.
    study_vec = ImagingStudy.study_description_tsv
    series_vec = Series.series_description_tsv
    # Combine the two tsvectors so a single ts_rank reflects both.
    # ``||`` concatenates tsvectors (with position preserved).
    combined_vec = study_vec.op("||")(series_vec)
    # Aggregate the rank across joined series rows. Postgres rejects the
    # un-aggregated form because ``series.series_description`` is not in
    # GROUP BY (the imaging_studies PK FD only covers same-table columns).
    rank_max = func.max(func.ts_rank_cd(combined_vec, ts_query)).label("rank_max")
    stmt = (
        select(ImagingStudy.id, rank_max)
        .select_from(ImagingStudy.__table__.outerjoin(Series, Series.study_id == ImagingStudy.id))
        .where(
            ImagingStudy.id.in_(visible_ids_sq),
            or_(
                study_vec.op("@@")(ts_query),
                series_vec.op("@@")(ts_query),
            ),
        )
        .group_by(ImagingStudy.id)
        .order_by(rank_max.desc())
        .limit(PER_SIGNAL_LIMIT)
    )
    rows = (await db.execute(stmt)).all()
    return [row[0] for row in rows]


def _embed_text_query(q: str) -> list[float] | None:
    """Run BiomedCLIP text encoder on ``q`` → 512-dim normalized vector.

    Returns ``None`` if the dependency isn't installed or inference
    fails — the caller treats that as "image signal unavailable".
    """
    try:
        import open_clip  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("hybrid-search: open_clip/torch unavailable, skipping image signal")
        return None

    try:
        # Module-level cache — loading BiomedCLIP is ~1.5s and ~500MB.
        # We stash the model on this function via a closure attribute
        # rather than a real global so tests can still monkeypatch
        # ``_embed_text_query`` wholesale.
        cache = _embed_text_query.__dict__
        if "model" not in cache:
            model, _, _ = open_clip.create_model_and_transforms(
                "hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224"
            )
            tokenizer = open_clip.get_tokenizer(
                "hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224"
            )
            model.eval()
            cache["model"] = model
            cache["tokenizer"] = tokenizer
        model = cache["model"]
        tokenizer = cache["tokenizer"]

        tokens = tokenizer([q])
        with torch.no_grad():
            features = model.encode_text(tokens)
            features = features / features.norm(dim=-1, keepdim=True)
        return features.squeeze(0).tolist()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hybrid-search: text embedding failed: %s", exc)
        return None


async def semantic_search_series(
    db: AsyncSession,
    *,
    q: str,
    k: int = PER_SIGNAL_LIMIT,
    visible_ids_sq=None,
) -> list[uuid.UUID]:
    """Return studies whose series are most similar to ``q`` in BiomedCLIP space.

    Embeds ``q`` via the text encoder, runs a pgvector cosine-distance
    query against series embeddings, dedupes to studies preserving
    rank. Returns at most ``k`` study ids.
    """
    # Prefer the out-of-process inference service (BVP_INFERENCE_SVC_URL);
    # else BiomedCLIP runs in-process, CPU-bound and blocking (~100ms
    # inference, ~1.5s cold load), offloaded so we don't stall the loop.
    from bvphoenix.services.inference_client import encode_text

    vector = await encode_text(q)
    if vector is None:
        vector = await asyncio.to_thread(_embed_text_query, q)
    if vector is None:
        return []

    # pgvector's Python binding accepts a list[float] on the right-hand
    # side of ``<=>``; see similar-to endpoint in search.py.
    distance = Embedding.vector.cosine_distance(vector).label("distance")
    stmt = (
        select(Series.study_id, distance)
        .select_from(Embedding.__table__.join(Series, Series.id == Embedding.target_id))
        .where(
            Embedding.target_kind == "series",
            Embedding.model_id == SERIES_EMBED_MODEL_ID,
        )
        .order_by("distance")
        # Pull a wider slab so we can dedupe to study without losing depth.
        .limit(k * 3)
    )
    if visible_ids_sq is not None:
        stmt = stmt.where(Series.study_id.in_(visible_ids_sq))

    await tune_vector_query(db, k=k * 3, filtered=visible_ids_sq is not None)
    rows = (await db.execute(stmt)).all()
    seen: set[uuid.UUID] = set()
    ordered: list[uuid.UUID] = []
    for study_id, _ in rows:
        if study_id in seen:
            continue
        seen.add(study_id)
        ordered.append(study_id)
        if len(ordered) >= k:
            break
    return ordered


# ---- Endpoint --------------------------------------------------------------


@router.get("/search/hybrid", response_model=HybridSearchOut)
@limiter.limit(SEARCH_LIMIT)
async def search_hybrid(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
    q: str = Query(..., min_length=1, max_length=128),
    k: int = Query(20, ge=1, le=100),
    weights: str | None = Query(
        None,
        description="Comma-separated ``signal:weight`` (e.g. ``tag:2,text:1,image:2``).",
        max_length=128,
    ),
    scope: Literal["all", "public", "mine"] | None = Query(
        None,
        description=(
            "Visibility scope. 'public' = OpenData library + studies marked "
            "is_public. 'mine' = studies owned by the caller. Default 'all'."
        ),
    ),
) -> HybridSearchOut:
    # Statement timeout mirrors ``/search``: a pathological ``q`` plus
    # the outer join + pgvector call can otherwise stall a worker.
    await db.execute(text("SET LOCAL statement_timeout = '5s'"))

    parsed_weights = _parse_weights(weights)

    visible_base = await visible_studies_filter(db, user)
    # UX scope narrowing (see apply_scope_filter): the auth filter is
    # the ceiling, scope can only further restrict. Each of the three
    # signal queries below joins against ``visible_ids_sq`` so applying
    # scope here is enough to constrain all of them.
    visible_base = apply_scope_filter(visible_base, scope, user)
    visible_ids_sq = visible_base.with_only_columns(ImagingStudy.id).subquery().select()

    async def _safe(name: str, coro):
        # Each signal runs inside a SAVEPOINT so a failed DB statement
        # (e.g. a malformed query, a pgvector dim mismatch) only aborts
        # its own sub-transaction. Without this, asyncpg leaves the
        # outer transaction in ``InFailedSqlTransactionError`` and every
        # subsequent statement — including the hydrate query below — 500s
        # even though the failure was already caught in Python land.
        savepoint = await db.begin_nested()
        try:
            result = await coro
            await savepoint.commit()
            return result
        except Exception as exc:  # pragma: no cover — defensive
            await savepoint.rollback()
            logger.warning("hybrid-search: %s signal failed: %s", name, exc)
            return []

    tag_ids = await _safe("tag", _tag_signal(db, q=q, visible_ids_sq=visible_ids_sq))
    text_ids = await _safe("text", _text_signal(db, q=q, visible_ids_sq=visible_ids_sq))
    image_ids = await _safe(
        "image",
        semantic_search_series(db, q=q, k=PER_SIGNAL_LIMIT, visible_ids_sq=visible_ids_sq),
    )

    fused = rrf_fuse(
        [
            (tag_ids, parsed_weights["tag"]),
            (text_ids, parsed_weights["text"]),
            (image_ids, parsed_weights["image"]),
        ],
        k=RRF_K,
    )

    if not fused:
        return HybridSearchOut(items=[], weights_used=parsed_weights, query=q)

    top_ids = sorted(fused.keys(), key=lambda sid: fused[sid], reverse=True)[:k]

    study_rows = (
        (await db.execute(select(ImagingStudy).where(ImagingStudy.id.in_(top_ids)))).scalars().all()
    )
    study_by_id = {s.id: s for s in study_rows}

    ranks_by_signal = {
        "tag": {sid: i + 1 for i, sid in enumerate(tag_ids)},
        "text": {sid: i + 1 for i, sid in enumerate(text_ids)},
        "image": {sid: i + 1 for i, sid in enumerate(image_ids)},
    }

    items: list[HybridSearchItem] = []
    for sid in top_ids:
        study = study_by_id.get(sid)
        if study is None:
            # Row deleted between the rank query and the hydrate. Skip
            # rather than 500 on the (extremely unlikely) race.
            continue
        signals = HybridSignalScores(
            **{
                name: round(
                    rrf_signal_contribution(
                        ranks_by_signal[name].get(sid), parsed_weights[name], RRF_K
                    ),
                    6,
                )
                for name in ("tag", "text", "image")
            }
        )
        items.append(
            HybridSearchItem(
                study=StudyOut.model_validate(study),
                score=round(fused[sid], 6),
                signals=signals,
            )
        )

    return HybridSearchOut(items=items, weights_used=parsed_weights, query=q)
