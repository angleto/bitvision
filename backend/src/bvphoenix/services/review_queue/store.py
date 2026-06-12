"""Store contract — the columns every reviewable item table shares.

Two stores, one engine: ``inbox_items`` (patient inbox, fbbf5270) and
``submissions`` (public contribution, 133349a9) each include
:class:`ReviewableItemMixin` in their own model. The engine never owns
those tables — it operates on any ORM object exposing the mixin
columns (typed as :class:`ReviewableItem` for the service layer).

The ``status`` column uses the shared Postgres ENUM ``review_status``
(created by migration ``0024_review_queue_shared``; ``create_type=False``
here so the ORM never races the migration). A shared native enum —
instead of the per-table CHECK convention used elsewhere — is
deliberate: the two consumer tables must agree on the status domain
*by construction*, and a single ALTER TYPE amends both in lockstep.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from bvphoenix.services.review_queue.states import REVIEW_STATUSES

REVIEW_STATUS_ENUM_NAME = "review_status"

# Aggregated verdict of the auto-check pass, worst-of across plugins.
# ``error`` means at least one check could not run (scanner down, ...):
# never silently treated as a pass — it aggregates like ``fail`` and the
# item still needs a human eye.
REVIEW_AUTO_VERDICTS: tuple[str, ...] = ("pass", "warn", "fail", "block", "error")


def review_status_enum() -> PG_ENUM:
    """The shared ``review_status`` PG enum, never creating the type —
    migrations own DDL (``0024_review_queue_shared``)."""
    return PG_ENUM(
        *REVIEW_STATUSES,
        name=REVIEW_STATUS_ENUM_NAME,
        create_type=False,
    )


class ReviewableItemMixin:
    """Columns shared by every staged-item table the engine drives.

    Consumers add their own scope columns (``patient_id`` for the inbox,
    contributor + licence for submissions) and their ingress payload
    pointers; the engine reads/writes only what is declared here.
    Consumers should also mix in ``TimestampMixin`` + ``UpdatedAtMixin``
    (``db/models/_common``) — the stale-item recovery sweep
    (:func:`~bvphoenix.services.review_queue.jobs.requeue_stale_processing`)
    keys off ``updated_at``.
    """

    status: Mapped[str] = mapped_column(
        review_status_enum(),
        nullable=False,
        server_default=text("'received'::review_status"),
    )
    # Aggregated plugin output: {"version": 1, "checks": {name: {...}}}.
    # Keyed by check name so a re-run overwrites in place (idempotent).
    auto_checks: Mapped[dict | None] = mapped_column(JSONB)
    auto_verdict: Mapped[str | None] = mapped_column(String(8))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)

    @declared_attr
    def reviewed_by_subject_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        # NULL for agent decisions — the agent identity lives in the
        # provenance chain (agent_token_id / agent_assistant_id), same
        # split as provenance_events.agent_subject_id.
        return mapped_column(
            PG_UUID(as_uuid=True),
            ForeignKey("subjects.id", ondelete="SET NULL"),
        )

    # Optimistic-concurrency token, bumped by the engine on every
    # transition (the report_contents pattern): a reviewer holding a
    # stale view cannot decide over a state that has moved underneath.
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )


__all__ = [
    "REVIEW_AUTO_VERDICTS",
    "REVIEW_STATUS_ENUM_NAME",
    "ReviewableItemMixin",
    "review_status_enum",
]
