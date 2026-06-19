"""Public-contribution submissions — the public-egress consumer of the shared
review/staging engine (``services/review_queue``), sibling to the patient inbox.

A study owner offers a study to the OpenData library (tier T3/T4). Instead of
publishing directly, the study is staged as a :class:`Submission` and driven
through the ``public_contribution`` review profile: header de-id verification,
burned-in-pixel screening (``services.pixel_deid``), malware + CSAM screening.
Because publishing PHI-bearing imaging to the public web is irreversible, the
profile's decision gate is **human-only** (unlike the agent-capable inbox) —
the accept transition is refused for agent actors by construction.

One table; the staged (redacted) preview blobs live under ``staged_prefix`` on
S3, outside the canonical fascicolo keyspace, until the submission is promoted
(published) or rejected (purged).
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import TimestampMixin, UpdatedAtMixin, uuid_pk
from bvphoenix.services.review_queue.store import ReviewableItemMixin

# Tiers a submission can target — the commons tiers (T3 anonymised training pool,
# T4 public CC). Mirrors the contribution-tier values that gate public egress.
SUBMISSION_TARGET_TIERS: tuple[str, ...] = ("t3", "t4")


class Submission(Base, ReviewableItemMixin, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "submissions"

    id: Mapped[uuid.UUID] = uuid_pk()
    # The private study being contributed + its owning patient/contributor.
    # SET NULL on delete so a withdrawn source never orphans the audit trail of
    # what was reviewed.
    source_study_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("imaging_studies.id", ondelete="SET NULL"),
    )
    source_patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="SET NULL"),
    )
    contributor_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    target_tier: Mapped[str] = mapped_column(String(8), nullable=False)
    # The published public patient/study, set by ``on_accept`` (NULL until then).
    public_patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="SET NULL"),
    )
    # Per-instance manifest: [{study_id, series_id, instance_id, s3_bucket,
    # s3_key, pixel_phi_risk, staged_redacted_key?}]. Auto-check results live in
    # the mixin's ``auto_checks``; the staged redacted previews under
    # ``staged_prefix``.
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    staged_prefix: Mapped[str | None] = mapped_column(String(1024))
    promoted_refs: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "target_tier IN (" + ",".join(f"'{t}'" for t in SUBMISSION_TARGET_TIERS) + ")",
            name="ck_submissions_target_tier",
        ),
        # The review-queue list: open submissions, newest first.
        Index("ix_submissions_status_created", "status", "created_at"),
        # Stale-processing recovery sweep (services/review_queue/jobs.py).
        Index("ix_submissions_status_updated", "status", "updated_at"),
        Index("ix_submissions_contributor", "contributor_subject_id"),
    )


__all__ = ["SUBMISSION_TARGET_TIERS", "Submission"]
