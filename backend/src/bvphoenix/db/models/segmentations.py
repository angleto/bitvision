"""Segmentation outputs registry (Sprint 6, ADR 0013).

A row per (series, producer) tuple. The producer can be:

* ``totalsegmentator`` — the AI segmentation worker (deferred until
  the ARM64 wheel spike per ADR 0013 is closed).
* ``manual`` — a human-drawn mask uploaded via the viewer.
* ``imported`` — a NIfTI from an external pipeline.

The pixel data lives in S3 as a single NIfTI (compressed) blob; this
table only stores the pointer and the label-map JSON so consumers
can interpret the integer labels without a side-channel.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

SEGMENTATION_PRODUCERS: tuple[str, ...] = (
    "totalsegmentator",
    "medsam",
    "manual",
    "imported",
)

# Acting principal that authored the mask (shared shape with Marker /
# Finding). Distinct from ``producer`` (the algorithm/engine).
SEGMENTATION_AUTHOR_KINDS: tuple[str, ...] = ("human", "agent", "system")


class Segmentation(Base):
    __tablename__ = "segmentations"

    id: Mapped[uuid.UUID] = uuid_pk()
    series_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
    )
    producer: Mapped[str] = mapped_column(String(32), nullable=False)
    producer_version: Mapped[str | None] = mapped_column(String(48))

    # Free-form name (e.g. ``total-organs``, ``lungs-left-right``).
    label: Mapped[str] = mapped_column(String(128), nullable=False)

    # NIfTI blob pointer in S3.
    s3_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column()

    # ``label_map`` carries integer-to-tissue mappings for the NIfTI:
    # ``{"1": "liver", "2": "spleen", ...}``. Consumers join this with
    # the voxel intensity to render anatomy-aware overlays.
    label_map: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    # Denormalized patient scope (mirrors Marker / Finding) for
    # patient-scoped queries + cascade. Nullable like imaging_studies.patient_id.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
    )

    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    # Acting principal (human / agent / system). ``producer`` is the
    # algorithm; ``author_kind`` is who triggered it, so an AI-authored
    # mask is never indistinguishable from a human-uploaded one.
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="human")
    model_id: Mapped[str | None] = mapped_column(Text)
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    # Foreground voxel count (the trainable size metric); persisted from
    # the import / predict path instead of being computed and discarded.
    nonzero_voxels: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "series_id", "producer", "label", name="uq_segmentations_series_producer_label"
        ),
        CheckConstraint(
            "author_kind IN (" + ",".join(f"'{a}'" for a in SEGMENTATION_AUTHOR_KINDS) + ")",
            name="ck_segmentations_author_kind",
        ),
        Index("ix_segmentations_series", "series_id"),
        Index("ix_segmentations_producer", "producer", "created_at"),
        Index("ix_segmentations_patient", "patient_id"),
    )
