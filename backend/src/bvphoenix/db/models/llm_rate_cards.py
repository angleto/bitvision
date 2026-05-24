"""Editable per-model LLM rate cards (multi-vendor billing).

One row per ``model_id``. Replaces the static dict in
``services/llm_cost.py`` so the operator can edit prices, markup, and
provider activation from the admin UI without redeploying. See the
0096 migration for the seed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base

PROVIDERS: tuple[str, ...] = (
    "anthropic",
    "openai",
    "scaleway",
    "gemini",
    "ollama-local",
    "in-house",
    "stub",
)
TIER_HINTS: tuple[str, ...] = ("free", "standard", "premium")


class LLMRateCard(Base):
    __tablename__ = "llm_rate_cards"

    model_id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)

    input_usd_per_mtok: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    output_usd_per_mtok: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    cache_read_usd_per_mtok: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default="0"
    )
    cache_creation_usd_per_mtok: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default="0"
    )

    # Per-model markup override (percent, 0..500). NULL means "inherit
    # the tier default" — preserves legacy behaviour for any row added
    # without an explicit per-model margin policy.
    markup_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    tier_hint: Mapped[str] = mapped_column(Text, nullable=False, server_default="standard")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    is_in_house: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )

    __table_args__ = (
        CheckConstraint("input_usd_per_mtok >= 0", name="ck_llm_rate_input_nonneg"),
        CheckConstraint("output_usd_per_mtok >= 0", name="ck_llm_rate_output_nonneg"),
        CheckConstraint(
            "markup_pct IS NULL OR (markup_pct >= 0 AND markup_pct <= 500)",
            name="ck_llm_rate_markup_bounds",
        ),
        CheckConstraint(
            "tier_hint IN ('free','standard','premium')",
            name="ck_llm_rate_tier_hint",
        ),
        Index("ix_llm_rate_cards_provider_active", "provider", "is_active"),
    )
