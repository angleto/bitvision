"""PhaseEnhancementSet — a persisted cross-phase wash-out measurement.

One ROI sampled across the contrast phases of a single study, with the
per-phase HU and the derived wash-out indices (APW / RPW). It is a
*derived, recomputable* record (the numbers come from
``services.washout.compute_washout`` over the per-phase samples) persisted
as an auditable, provenance-stamped measurement that can land in a report.

Unlike ``LesionTrack`` / ``ResponseAssessment`` this carries NO separate
revision table: a wash-out measurement is a snapshot of a physical ROI at
one study, not an editable longitudinal assessment — to change it you take
a new measurement. It keeps the rest of the write contract: provenance
(author_kind / model / agent token), an ``etag``, soft-delete + restore,
and patient_id for cross-patient-by-construction safety.

See ``backend/alembic/versions/0031_phase_enhancement_sets.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

PHASE_ENH_ROI_KINDS: tuple[str, ...] = ("sphere", "bbox")
PHASE_ENH_AUTHOR_KINDS: tuple[str, ...] = ("human", "agent", "system")


class PhaseEnhancementSet(Base):
    __tablename__ = "phase_enhancement_sets"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    study_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("imaging_studies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str | None] = mapped_column(Text)

    # The ROI in patient space (LPS): kind + the kind-specific coordinates
    # (center_lps + radius_mm | min_lps + max_lps) + frame_of_reference_uid.
    roi_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    roi: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Per-phase HU samples: [{series_id, acquisition_phase, hu_mean, hu_std, voxel_count}].
    samples: Mapped[dict | None] = mapped_column(JSONB)
    # Full computed wash-out result (curve + indices + threshold flags).
    washout: Mapped[dict | None] = mapped_column(JSONB)
    # Headline indices promoted to columns for querying / report roll-up.
    apw: Mapped[float | None] = mapped_column(Float)
    rpw: Mapped[float | None] = mapped_column(Float)
    enhanced_phase: Mapped[str | None] = mapped_column(String(24))
    delayed_phase: Mapped[str | None] = mapped_column(String(24))

    # Provenance (same shape as Finding / LesionTrack / ResponseAssessment).
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
            "roi_kind IN (" + ",".join(f"'{k}'" for k in PHASE_ENH_ROI_KINDS) + ")",
            name="ck_phase_enh_roi_kind",
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in PHASE_ENH_AUTHOR_KINDS) + ")",
            name="ck_phase_enh_author_kind",
        ),
        Index("ix_phase_enh_study", "study_id"),
        Index("ix_phase_enh_patient", "patient_id"),
    )
