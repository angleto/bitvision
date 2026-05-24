"""Transparency endpoint — public aggregate stats (F11).

``GET /api/transparency`` returns platform-level counts designed for the
public transparency page: how many studies are on the platform, the
tier split, how many users are registered, how many grants are active,
and how much LLM activity has happened. No per-user, per-study, or
per-patient data is ever exposed.

The endpoint is public (no auth) because transparency is the point.
Rate-limited at the semantic-search tier to keep a hostile client from
hammering it into a DoS; the queries are cheap (indexed counts) but we
do not want to let an enumerator infer a per-minute upload cadence.

The design of the payload is intentionally narrow. Fields we might
*want* but that risk revealing operator identity or small-N tenant
activity (e.g., audit-log counts per day, per-modality active users)
are left out. Anything resembling a time series is omitted until we
have a proper k-anonymity rule to gate it (see F10 k-anon ≥ 5).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

try:
    from redis import asyncio as aioredis  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — arq pulls redis transitively
    aioredis = None  # type: ignore[assignment]

from bvphoenix import __version__
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    Grant,
    ImagingStudy,
    Summary,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.services.rate_limit import SEARCH_SEMANTIC_LIMIT, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["transparency"])

# --- 5-minute response cache ----------------------------------------------
#
# Transparency.md "Future work" called for a short cache once QPS grew
# non-trivial; the rate limit (30/min per IP) already makes accidental
# hammering unlikely, but a single adversary or a viral page link could
# still multiply the query cost. Caching for 5 minutes makes the steady
# state one SQL roundtrip per window per worker (Redis-shared across
# workers when available) and matches the "public counts don't need to
# be fresh to the second" posture of the payload.
#
# We serialise the Pydantic payload as its model_dump() dict so the
# cache stays decoder-version-agnostic; reconstructing the pydantic
# model on hit is cheap enough to not bother with a raw-bytes path.
#
# Key versioning (`_CACHE_KEY`): bump the suffix when the payload shape
# changes so an old worker can't serve stale rows with a new schema.

_CACHE_TTL_SECONDS = 300
_CACHE_KEY = "transparency:v1"


class _TransparencyCache:
    """Read-through cache with Redis backend + in-process fallback.

    Matches the shape of ``_LockoutBackend`` in services/rate_limit: try
    Redis first (shared across workers), fall back to a dict keyed by
    the same string. Fail-open if Redis throws — transparency staleness
    is not worth a 500.
    """

    def __init__(self) -> None:
        self._client: Any | None = None
        self._use_memory: bool = aioredis is None
        self._mem_value: dict[str, Any] | None = None
        self._mem_expires_at: float = 0.0

    async def _redis(self) -> Any | None:
        if self._use_memory:
            return None
        if self._client is None:
            settings = get_settings()
            try:
                self._client = aioredis.from_url(
                    settings.redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                )
                await self._client.ping()
            except Exception:
                self._client = None
                self._use_memory = True
        return self._client

    async def get(self) -> dict[str, Any] | None:
        client = await self._redis()
        if client is None:
            if self._mem_value is None:
                return None
            if self._mem_expires_at < time.time():
                self._mem_value = None
                return None
            return self._mem_value
        try:
            import json

            raw = await client.get(_CACHE_KEY)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            logger.warning("transparency cache Redis read failed; treating as miss")
            return None

    async def set(self, value: dict[str, Any]) -> None:
        client = await self._redis()
        if client is None:
            self._mem_value = value
            self._mem_expires_at = time.time() + _CACHE_TTL_SECONDS
            return
        try:
            import json

            await client.set(_CACHE_KEY, json.dumps(value), ex=_CACHE_TTL_SECONDS)
        except Exception:
            logger.warning("transparency cache Redis write failed; keeping memory copy")
            self._mem_value = value
            self._mem_expires_at = time.time() + _CACHE_TTL_SECONDS


_cache = _TransparencyCache()
# Guard against stampede: if the cache misses on N concurrent requests,
# only one of them should run the queries. The lock is per-process; the
# Redis round-trip is cheap enough that cross-worker stampedes are
# acceptable (a worker that comes up cold re-fetches once).
_build_lock = asyncio.Lock()


class StudiesStats(BaseModel):
    total: int
    by_tier: dict[str, int] = Field(
        description=(
            "Count by contribution tier. Keys are 't1' (private), 't2' "
            "(shared controlled), 't3' (training opt-in anonymised), "
            "'t4' (public CC)."
        ),
    )
    public: int = Field(
        description=(
            "Count of studies with is_public=true (subset of the tier "
            "split; a t4 study is always public, a t3 may or may not be)."
        ),
    )
    by_modality: dict[str, int] = Field(
        description=(
            "Top modalities across the public + training-opt-in subset "
            "(t3 + t4). Private and shared-controlled studies are not "
            "counted here to avoid leaking tenant activity."
        ),
    )


class UsersStats(BaseModel):
    total: int = Field(description="Registered users (all roles).")


class SharingStats(BaseModel):
    grants_active: int = Field(
        description="Grants that are neither revoked nor expired right now.",
    )
    grants_deidentified: int = Field(
        description="Subset of active grants that enforce de-identification on read.",
    )
    grants_commercial: int = Field(
        description="Subset of active grants flagged commercial (irrevocable once accepted).",
    )


class LLMStats(BaseModel):
    consultations_total: int = Field(
        description="Completed LLM consultations (patient- and study-scoped).",
    )
    summaries_total: int = Field(
        description="Generated cascade summaries (series / study / patient).",
    )


class GovernanceLinks(BaseModel):
    license: str = "AGPL-3.0-or-later"


class TransparencyOut(BaseModel):
    generated_at: str
    version: str
    studies: StudiesStats
    users: UsersStats
    sharing: SharingStats
    llm: LLMStats
    governance: GovernanceLinks


async def _studies_stats(db: AsyncSession) -> StudiesStats:
    total = int((await db.execute(select(func.count(ImagingStudy.id)))).scalar_one())
    public = int(
        (
            await db.execute(select(func.count()).where(ImagingStudy.is_public.is_(True)))
        ).scalar_one()
    )

    tier_rows = (
        await db.execute(
            select(ImagingStudy.contribution_tier, func.count()).group_by(
                ImagingStudy.contribution_tier
            )
        )
    ).all()
    by_tier = dict.fromkeys(("t1", "t2", "t3", "t4"), 0)
    for tier, n in tier_rows:
        by_tier[tier] = int(n)

    # Modalities are stored as a Postgres text[] on imaging_studies.
    # v3: the legacy ``studies`` table was renamed to
    # ``imaging_studies`` in migration 0073; the raw SQL here is
    # rebased on the new name. unnest() lets us aggregate by modality
    # without pulling every row into Python; restricted to t3/t4 so
    # the distribution is over the set the platform is willing to
    # be transparent about.
    modality_rows = (
        await db.execute(
            text(
                """
                SELECT m, COUNT(*) AS n
                FROM imaging_studies s, LATERAL unnest(s.modalities) AS m
                WHERE s.contribution_tier IN ('t3', 't4')
                GROUP BY m
                ORDER BY n DESC
                LIMIT 20
                """
            )
        )
    ).all()
    by_modality = {row[0]: int(row[1]) for row in modality_rows}

    return StudiesStats(total=total, by_tier=by_tier, public=public, by_modality=by_modality)


async def _users_stats(db: AsyncSession) -> UsersStats:
    total = int((await db.execute(select(func.count(User.subject_id)))).scalar_one())
    return UsersStats(total=total)


async def _sharing_stats(db: AsyncSession) -> SharingStats:
    now = datetime.now(UTC)
    active_q = select(func.count(Grant.id)).where(
        Grant.revoked_at.is_(None),
        (Grant.valid_until.is_(None)) | (Grant.valid_until > now),
    )
    active = int((await db.execute(active_q)).scalar_one())
    deid = int((await db.execute(active_q.where(Grant.deidentify.is_(True)))).scalar_one())
    commercial = int((await db.execute(active_q.where(Grant.is_commercial.is_(True)))).scalar_one())
    return SharingStats(
        grants_active=active,
        grants_deidentified=deid,
        grants_commercial=commercial,
    )


async def _llm_stats(db: AsyncSession) -> LLMStats:
    # v3: count canonical_synthesis report_contents in place of the
    # legacy ``consultations`` table (the public stat name is kept for
    # back-compat with the published transparency-report shape).
    from bvphoenix.db.models import ReportContent

    consultations = int(
        (
            await db.execute(
                select(func.count(ReportContent.id)).where(
                    ReportContent.authority_id == "canonical_synthesis"
                )
            )
        ).scalar_one()
    )
    summaries = int((await db.execute(select(func.count(Summary.id)))).scalar_one())
    return LLMStats(consultations_total=consultations, summaries_total=summaries)


async def _build_payload(db: AsyncSession) -> TransparencyOut:
    studies = await _studies_stats(db)
    users = await _users_stats(db)
    sharing = await _sharing_stats(db)
    llm = await _llm_stats(db)
    return TransparencyOut(
        generated_at=datetime.now(UTC).isoformat(),
        version=__version__,
        studies=studies,
        users=users,
        sharing=sharing,
        llm=llm,
        governance=GovernanceLinks(),
    )


@router.get(
    "/transparency",
    response_model=TransparencyOut,
    summary="Public aggregate platform stats",
)
@limiter.limit(SEARCH_SEMANTIC_LIMIT)
async def transparency(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TransparencyOut:
    """Return platform-level aggregate stats for the transparency page.

    Served from a 5-minute cache (Redis when available, in-process
    otherwise). Freshness is intentional: the payload is a summary of
    the world and the transparency page doesn't need to-the-second
    numbers. Revalidation is opportunistic — the first request after
    the TTL recomputes and refills the cache.
    """
    cached = await _cache.get()
    if cached is not None:
        return TransparencyOut.model_validate(cached)

    async with _build_lock:
        # Double-check: another request may have populated the cache
        # while we were waiting for the lock.
        cached = await _cache.get()
        if cached is not None:
            return TransparencyOut.model_validate(cached)
        payload = await _build_payload(db)
        await _cache.set(payload.model_dump())
    return payload
