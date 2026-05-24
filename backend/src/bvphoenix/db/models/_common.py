"""Shared primitives for ORM models: UUID PKs, timestamp mixin, enum names.

Enum *names* (e.g. ``SUBJECT_KIND_ENUM_NAME``) are kept in one place so the
ORM side and the Alembic migration side always agree on the Postgres type
name — PG enums are global objects and a mismatch is silent until runtime.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

SUBJECT_KIND_ENUM_NAME = "subject_kind"
SUBJECT_KIND_VALUES: tuple[str, ...] = (
    "user",
    "organization",
    "group",
    "public",
    "agent",
)

CONTRIBUTION_TIER_ENUM_NAME = "contribution_tier"
CONTRIBUTION_TIER_VALUES: tuple[str, ...] = ("t1", "t2", "t3", "t4")


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class UpdatedAtMixin:
    """Adds an ``updated_at`` column auto-bumped on row update.

    Kept separate from ``TimestampMixin`` because several existing
    models (Consultation, ClinicalNote, Summary) define their own
    ``updated_at`` inline; folding it into ``TimestampMixin`` would
    collide. Mix in only on tables that need server-side
    last-modified tracking (e.g. Folder, Study, PatientDocument so
    the fascicolo card can show the "ultima modifica" stamp).
    """

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
