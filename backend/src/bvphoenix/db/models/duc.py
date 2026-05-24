"""Data Use Committee ORM models (F10.3).

Three tables:

* :class:`DUCMember` — committee roster. ``role`` is one of
  ``chair``, ``member``, ``external_advisor`` — the quorum rule
  (F10.3 service) decides how to weight each.
* :class:`DUCRequest` — one row per review. Lifecycle:
  ``pending → approved / rejected / expired / withdrawn``. A
  partial unique index keeps a licence from opening a second
  review while one is still in flight.
* :class:`DUCVote` — one row per (request, member).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

DUC_ROLES: tuple[str, ...] = ("chair", "member", "external_advisor")
DUC_REQUEST_STATUSES: tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "expired",
    "withdrawn",
)
DUC_DECISIONS: tuple[str, ...] = ("approve", "reject", "abstain")


class DUCMember(Base):
    __tablename__ = "duc_members"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, server_default="member")
    active_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "role IN ('chair','member','external_advisor')",
            name="ck_duc_members_role",
        ),
        Index(
            "uq_duc_members_user_active",
            "user_subject_id",
            unique=True,
            postgresql_where="revoked_at IS NULL",
        ),
    )


class DUCRequest(Base):
    __tablename__ = "duc_requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    license_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("training_licenses.id", ondelete="CASCADE"),
        nullable=False,
    )
    submitted_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired','withdrawn')",
            name="ck_duc_requests_status",
        ),
        Index("ix_duc_requests_status", "status"),
        Index(
            "uq_duc_requests_open_per_license",
            "license_id",
            unique=True,
            postgresql_where="status = 'pending'",
        ),
    )


class DUCVote(Base):
    __tablename__ = "duc_votes"

    request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("duc_requests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("duc_members.id", ondelete="CASCADE"),
        primary_key=True,
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    voted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "decision IN ('approve','reject','abstain')",
            name="ck_duc_votes_decision",
        ),
    )
