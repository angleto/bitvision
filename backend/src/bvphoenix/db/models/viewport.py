"""Viewport state model — per-user, per-series UI state blob.

Each row persists the client-side viewer state (camera, window/level,
slice crosshair, panel layout, …) for a single user looking at a single
series. Opening the same series later restores the state. The state
shape is owned by the frontend — the backend stores opaque JSON.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class ViewportState(Base):
    __tablename__ = "viewport_states"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    series_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_subject_id",
            "series_id",
            name="uq_viewport_states_user_series",
        ),
    )
