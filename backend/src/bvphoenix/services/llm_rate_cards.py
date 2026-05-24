"""DB-backed loader + admin CRUD for LLM rate cards.

The runtime hot path (``services.llm_cost.billed_cents``) stays
synchronous and reads from in-memory dicts. This module is what
populates those dicts from the ``llm_rate_cards`` table, and what the
admin endpoints call to mutate them.

Lifecycle:

* ``refresh_rate_cards(db)`` — pull every active row, replace the
  module-level overrides in ``llm_cost``. Called at FastAPI startup
  and after each admin PATCH so a price tweak takes effect immediately.
* ``upsert_rate_card(db, ...)`` and ``delete_rate_card(db, ...)`` —
  admin mutations. They write the DB row, then invoke the refresh.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import LLMRateCard
from bvphoenix.services.llm_cost import (
    ModelRate,
    clear_dynamic_rate,
    reset_dynamic_rates,
    set_dynamic_rate,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RateCardOut:
    model_id: str
    provider: str
    display_name: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    cache_creation_usd_per_mtok: float
    markup_pct: float | None
    tier_hint: str
    is_active: bool
    is_in_house: bool
    notes: str | None
    updated_at: datetime
    updated_by_subject_id: uuid.UUID | None

    @classmethod
    def from_row(cls, row: LLMRateCard) -> RateCardOut:
        return cls(
            model_id=row.model_id,
            provider=row.provider,
            display_name=row.display_name,
            input_usd_per_mtok=float(row.input_usd_per_mtok),
            output_usd_per_mtok=float(row.output_usd_per_mtok),
            cache_read_usd_per_mtok=float(row.cache_read_usd_per_mtok),
            cache_creation_usd_per_mtok=float(row.cache_creation_usd_per_mtok),
            markup_pct=float(row.markup_pct) if row.markup_pct is not None else None,
            tier_hint=row.tier_hint,
            is_active=row.is_active,
            is_in_house=row.is_in_house,
            notes=row.notes,
            updated_at=row.updated_at,
            updated_by_subject_id=row.updated_by_subject_id,
        )


async def list_rate_cards(db: AsyncSession, *, only_active: bool = False) -> list[RateCardOut]:
    stmt = select(LLMRateCard).order_by(LLMRateCard.provider, LLMRateCard.model_id)
    if only_active:
        stmt = stmt.where(LLMRateCard.is_active.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return [RateCardOut.from_row(r) for r in rows]


async def refresh_rate_cards(db: AsyncSession) -> int:
    """Replace the in-memory overrides with the current DB state.

    Returns the number of active rows applied. Inactive rows are
    pruned from the cache so a freshly-deactivated model falls back to
    the static legacy dict (or :class:`UnknownModelError` if it had no
    legacy entry)."""
    rows = (
        (await db.execute(select(LLMRateCard).where(LLMRateCard.is_active.is_(True))))
        .scalars()
        .all()
    )
    reset_dynamic_rates()
    for r in rows:
        set_dynamic_rate(
            r.model_id,
            rate=ModelRate(
                input_usd_per_mtok=float(r.input_usd_per_mtok),
                output_usd_per_mtok=float(r.output_usd_per_mtok),
                cache_read_usd_per_mtok=float(r.cache_read_usd_per_mtok),
                cache_creation_usd_per_mtok=float(r.cache_creation_usd_per_mtok),
            ),
            tier_hint=r.tier_hint,
            markup_pct=float(r.markup_pct) if r.markup_pct is not None else None,
        )
    logger.info("llm_rate_cards refreshed: %d active rows", len(rows))
    return len(rows)


async def upsert_rate_card(
    db: AsyncSession,
    *,
    actor_subject_id: uuid.UUID | None,
    model_id: str,
    provider: str,
    display_name: str,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
    cache_read_usd_per_mtok: float = 0.0,
    cache_creation_usd_per_mtok: float = 0.0,
    markup_pct: float | None = None,
    tier_hint: str = "standard",
    is_active: bool = True,
    is_in_house: bool = False,
    notes: str | None = None,
) -> LLMRateCard:
    """Insert or update one row. Refreshes the runtime cache so the
    admin sees the change applied on the very next debit call."""
    if input_usd_per_mtok < 0 or output_usd_per_mtok < 0:
        raise ValueError("rates must be non-negative")
    if markup_pct is not None and (markup_pct < 0 or markup_pct > 500):
        raise ValueError("markup_pct must be in [0, 500]")
    if tier_hint not in ("free", "standard", "premium"):
        raise ValueError(f"unknown tier_hint: {tier_hint}")

    row = await db.get(LLMRateCard, model_id)
    now = datetime.now(UTC)
    if row is None:
        row = LLMRateCard(
            model_id=model_id,
            provider=provider,
            display_name=display_name,
            input_usd_per_mtok=Decimal(str(input_usd_per_mtok)),
            output_usd_per_mtok=Decimal(str(output_usd_per_mtok)),
            cache_read_usd_per_mtok=Decimal(str(cache_read_usd_per_mtok)),
            cache_creation_usd_per_mtok=Decimal(str(cache_creation_usd_per_mtok)),
            markup_pct=Decimal(str(markup_pct)) if markup_pct is not None else None,
            tier_hint=tier_hint,
            is_active=is_active,
            is_in_house=is_in_house,
            notes=notes,
            updated_by_subject_id=actor_subject_id,
        )
        db.add(row)
    else:
        row.provider = provider
        row.display_name = display_name
        row.input_usd_per_mtok = Decimal(str(input_usd_per_mtok))
        row.output_usd_per_mtok = Decimal(str(output_usd_per_mtok))
        row.cache_read_usd_per_mtok = Decimal(str(cache_read_usd_per_mtok))
        row.cache_creation_usd_per_mtok = Decimal(str(cache_creation_usd_per_mtok))
        row.markup_pct = Decimal(str(markup_pct)) if markup_pct is not None else None
        row.tier_hint = tier_hint
        row.is_active = is_active
        row.is_in_house = is_in_house
        row.notes = notes
        row.updated_at = now
        row.updated_by_subject_id = actor_subject_id

    await db.flush()
    await db.commit()
    await refresh_rate_cards(db)
    return row


async def delete_rate_card(
    db: AsyncSession,
    *,
    model_id: str,
) -> bool:
    """Hard-delete a row. Prefer ``is_active=false`` when you may want
    to bring a model back; this is for genuine cleanups (e.g. a
    decommissioned in-house model with no historical traffic)."""
    res = await db.execute(delete(LLMRateCard).where(LLMRateCard.model_id == model_id))
    if (res.rowcount or 0) == 0:  # type: ignore[attr-defined]
        return False
    await db.commit()
    clear_dynamic_rate(model_id)
    return True


__all__ = [
    "RateCardOut",
    "delete_rate_card",
    "list_rate_cards",
    "refresh_rate_cards",
    "upsert_rate_card",
]
