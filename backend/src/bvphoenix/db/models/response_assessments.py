"""Response assessments — patient-level RECIST / volumetric tumour response.

The roll-up above lesion tracks: at a follow-up timepoint, aggregate the
patient's *target* lesions (``lesion_tracks.recist_role='target'``) into a
sum of longest diameters (RECIST 1.1) or a total volume, compare against
baseline and the nadir, and classify the response (CR / PR / SD / PD). It
is a *derived* record — recomputable from the findings — persisted as an
auditable, versioned clinical assessment (a human or agent authors it; the
``basis`` JSONB snapshots which lesions contributed and their deltas).

Cross-patient: ``patient_id`` is required and the compute service only
reads that patient's tracks; the API lives under ``/patients/{id}/`` and
validates both studies belong to the patient.

See ``backend/alembic/versions/0029_response_assessments.py``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# Response criteria. ``recist_1_1`` is unidimensional (sum of longest
# diameters); ``volumetric`` uses total volume with research thresholds
# (NOT a standardised criterion — labelled as such); ``percist`` is the
# PET metabolic analogue (SUV-based).
RESPONSE_CRITERIA: tuple[str, ...] = ("recist_1_1", "volumetric", "percist")

# Overall response categories (NE = not evaluable).
RESPONSE_CATEGORIES: tuple[str, ...] = ("CR", "PR", "SD", "PD", "NE")

# Non-target lesion status (RECIST 1.1).
NON_TARGET_STATUSES: tuple[str, ...] = ("CR", "non_CR_non_PD", "PD", "NE")

RESPONSE_AUTHOR_KINDS: tuple[str, ...] = ("human", "agent", "system")

RESPONSE_REVISION_CHANGE_KINDS: tuple[str, ...] = (
    "create",
    "update",
    "recompute",
    "delete",
    "restore",
)


class ResponseAssessment(Base):
    __tablename__ = "response_assessments"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    assessment_date: Mapped[date | None] = mapped_column(Date)
    baseline_study_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("imaging_studies.id", ondelete="SET NULL")
    )
    current_study_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("imaging_studies.id", ondelete="SET NULL")
    )

    criterion: Mapped[str] = mapped_column(String(16), nullable=False, server_default="recist_1_1")

    # Unidimensional (RECIST): sum of target longest diameters (mm).
    target_sum_mm: Mapped[float | None] = mapped_column(Float)
    baseline_sum_mm: Mapped[float | None] = mapped_column(Float)
    nadir_sum_mm: Mapped[float | None] = mapped_column(Float)
    target_sum_pct_change: Mapped[float | None] = mapped_column(Float)
    # Volumetric variant (research thresholds).
    volume_total_ml: Mapped[float | None] = mapped_column(Float)
    volume_pct_change: Mapped[float | None] = mapped_column(Float)

    category: Mapped[str] = mapped_column(String(4), nullable=False)
    new_lesions: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    non_target_status: Mapped[str | None] = mapped_column(String(16))

    # Auditable derivation: which tracks/findings contributed + per-lesion
    # deltas, so the category can be justified and recomputed.
    basis: Mapped[dict | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)

    # Provenance (same shape as Finding / LesionTrack).
    author_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("subjects.id", ondelete="SET NULL")
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="human")
    model_id: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_tokens.id", ondelete="SET NULL")
    )

    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "criterion IN (" + ",".join(f"'{c}'" for c in RESPONSE_CRITERIA) + ")",
            name="ck_response_assessments_criterion",
        ),
        CheckConstraint(
            "category IN (" + ",".join(f"'{c}'" for c in RESPONSE_CATEGORIES) + ")",
            name="ck_response_assessments_category",
        ),
        CheckConstraint(
            "non_target_status IS NULL OR non_target_status IN ("
            + ",".join(f"'{s}'" for s in NON_TARGET_STATUSES)
            + ")",
            name="ck_response_assessments_non_target",
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in RESPONSE_AUTHOR_KINDS) + ")",
            name="ck_response_assessments_author_kind",
        ),
        Index("ix_response_assessments_patient", "patient_id"),
        Index(
            "ix_response_assessments_live",
            "patient_id",
            "current_study_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class ResponseAssessmentRevision(Base):
    """Immutable snapshot per act (mirrors ``finding_revision``)."""

    __tablename__ = "response_assessment_revision"

    id: Mapped[uuid.UUID] = uuid_pk()
    response_assessment_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    diff_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["response_assessment_id"],
            ["response_assessments.id"],
            name="fk_response_assessment_revision_ra",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "change_kind IN (" + ",".join(f"'{k}'" for k in RESPONSE_REVISION_CHANGE_KINDS) + ")",
            name="ck_response_assessment_revision_kind",
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in RESPONSE_AUTHOR_KINDS) + ")",
            name="ck_response_assessment_revision_author_kind",
        ),
        CheckConstraint("revision_no >= 1", name="ck_response_assessment_revision_no_pos"),
        UniqueConstraint(
            "response_assessment_id",
            "revision_no",
            name="uq_response_assessment_revision_ra_no",
        ),
        Index(
            "ix_response_assessment_revision_ra",
            "response_assessment_id",
            "revision_no",
        ),
        Index("ix_response_assessment_revision_patient", "patient_id"),
    )


__all__ = [
    "NON_TARGET_STATUSES",
    "RESPONSE_AUTHOR_KINDS",
    "RESPONSE_CATEGORIES",
    "RESPONSE_CRITERIA",
    "RESPONSE_REVISION_CHANGE_KINDS",
    "ResponseAssessment",
    "ResponseAssessmentRevision",
]
