"""ClinicalNote — free-text clinical commentary attached to any item
in a patient's fascicolo (study, series, document, consultation,
patient itself).

Distinct from the radiology-anchored ``Annotation`` model: that one is
reserved for measurements, segmentations, AI findings — payload is
structured JSONB. ``ClinicalNote.body`` is plain markdown the clinician
typed by hand, with a date stamp, author, and a polymorphic reference
back to whatever they were looking at.

The aggregated view (``GET /api/patients/{id}/notes``) joins these
notes into a single chronological timeline so the next clinician can
read all the running commentary in one place.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import TimestampMixin, uuid_pk

CLINICAL_NOTE_TARGET_KINDS: tuple[str, ...] = (
    "study",
    "series",
    "document",
    "consultation",
    "patient",
)


class ClinicalNote(TimestampMixin, Base):
    __tablename__ = "clinical_notes"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    author_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional spatial anchor for notes pinned to a specific voxel of
    # an image. Populated when the note is created from inside the
    # viewer; ``{x, y, z}`` are zero-based indices into the active
    # series volume. Null for plain notes (the default for everything
    # outside the viewer). The unified ``MarkerListPanel`` reads this
    # column to group notes by axial slice and offer click-to-jump.
    anchor: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # AI provenance. ``author_kind = 'agent'`` means an LLM/automation
    # produced this note; the frontend MUST surface it with a visual
    # treatment that cannot be confused with human-authored content.
    # ``model_id`` (e.g. 'claude-opus-4-7') and ``provider`` (e.g.
    # 'anthropic') let the clinician retire / hide notes from a
    # specific model without touching others.
    author_kind: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'human'")
    )
    model_id: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(64))
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "target_kind IN (" + ",".join(f"'{k}'" for k in CLINICAL_NOTE_TARGET_KINDS) + ")",
            name="ck_clinical_notes_target_kind",
        ),
        CheckConstraint("length(btrim(body)) > 0", name="ck_clinical_notes_body_nonempty"),
    )
