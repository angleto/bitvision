"""Public iCal subscription handle (revocable, HMAC-signed feed).

One row == one shareable, non-expiring calendar feed URL for a single
patient. The URL itself carries no secret beyond the HMAC token derived
from this row's ``id`` + ``patient_id`` (see
``services.calendar_subscription_token``); this row is the revocation
and audit anchor:

- ``revoked_at`` set    -> the public feed endpoint 403s immediately,
  even though the token is still cryptographically valid.
- ``expires_at``        -> nullable; today every subscription is
  non-expiring (product decision 2026-05-18). The column is kept so a
  per-link TTL can be introduced later without a migration; the feed
  endpoint already enforces it when set.
- ``last_accessed_at`` / ``access_count`` -> best-effort usage trail so
  the owner can tell whether a leaked link is being polled.

``author_kind`` records whether the row was minted by a human or via an
MCP/agent session (memory ``feedback_ai_provenance_must_be_visible``);
``created_by_subject_id`` is the human owner (NULL for agent mints).

Cross-patient safety: a subscription is bound to exactly one
``patient_id`` and the token's signature covers it, so a feed token can
never be made to resolve to a different fascicolo.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class CalendarSubscription(Base):
    __tablename__ = "calendar_subscriptions"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str | None] = mapped_column(String(255))
    # Idempotency of the create is handled at the HTTP layer by the
    # canonical ``require_idempotency_key`` replay cache (ADR 0002), not
    # by a column here — keeps a single mechanism across the codebase.
    author_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'human'")
    )
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        # Hot path: "active subscriptions for this patient".
        Index(
            "ix_calendar_subscriptions_patient_active",
            "patient_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
