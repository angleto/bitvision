"""Grant model — atomic capability-based ACL record (authorization.md §2)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class Grant(Base):
    __tablename__ = "grants"

    id: Mapped[uuid.UUID] = uuid_pk()
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    grantor_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    grantee_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_grant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("grants.id", ondelete="CASCADE"),
    )
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    is_commercial: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # When true, DICOM downloads authorised by this grant are passed
    # through ``services.deidentify.deidentify_dicom_bytes`` before hand-off.
    # Used for share links sent to researchers who need image data but
    # not patient identity.
    deidentify: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    purpose: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "resource_kind IN ("
            "'study','series','instance','annotation','dataset','patient','folder'"
            ")",
            name="ck_grants_resource_kind",
        ),
        Index(
            "ix_grants_grantee_resource",
            "grantee_subject_id",
            "resource_kind",
            "resource_id",
        ),
        Index("ix_grants_resource", "resource_kind", "resource_id"),
    )
