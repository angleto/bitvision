"""Principal models — Subject (common base) plus User, Organization, Group,
and Membership edges.

Every entity that can hold a permission is a row in ``subjects``; the
concrete-subtype tables (``users``, ``organizations``, ``groups``) use the
subject id as their own primary key. This keeps foreign keys in ``grants``
uniform and lets us write a single RLS principal-set query later.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import (
    SUBJECT_KIND_ENUM_NAME,
    SUBJECT_KIND_VALUES,
    TimestampMixin,
    uuid_pk,
)


class Subject(TimestampMixin, Base):
    __tablename__ = "subjects"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(
        Enum(
            *SUBJECT_KIND_VALUES,
            name=SUBJECT_KIND_ENUM_NAME,
            create_type=False,
        ),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), unique=True)
    # Bcrypt hash for the local-password login (the default phoenix
    # auth path). NULL only for accounts provisioned exclusively via
    # external OIDC.
    password_hash: Mapped[str | None] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    # Timestamp of the most recent successful email verification. NULL
    # means the user has never confirmed ownership of ``email``; login
    # can be gated on this via BVP_REQUIRE_EMAIL_VERIFICATION.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    # TOTP secret (base32). Plaintext TEXT in this migration; production
    # should KMS-wrap at rest — see docs/security-mfa.md. NULL until the
    # user starts setup; becomes non-NULL once setup begins and stays set
    # after activation.
    mfa_secret: Mapped[str | None] = mapped_column(Text)
    # When the user completed MFA activation. NULL means MFA is either
    # not started or pending (secret present but not yet activated).
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bcrypt hashes of one-shot backup codes. Codes are shown once at
    # activation; consumed entries are removed from the array.
    backup_codes_hash: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # ---- Admin overrides (services/quota + jobs respect these) ----
    # Per-user storage cap in bytes for T1+T2. NULL = use the platform
    # default (``services.quota.STORAGE_FREE_TIER_BYTES``). Operators
    # bump this from the admin dashboard for institutional accounts.
    storage_quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Maximum number of jobs the user can have queued/running at once.
    # NULL = use the platform default in services.jobs (today 5). The
    # check fires inside ``enqueue_or_get`` and translates to a 429
    # client-side.
    max_concurrent_jobs: Mapped[int | None] = mapped_column(Integer)
    # Soft account lock. ``False`` blocks login (auth.require_user
    # raises 403) and prevents new uploads / consultations. The user
    # remains visible to the admin and can be unblocked. ``blocked_at``
    # is the timestamp captured for the audit trail; ``blocked_reason``
    # surfaces the human-readable note the admin entered.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_reason: Mapped[str | None] = mapped_column(String(255))


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    kind: Mapped[str | None] = mapped_column(String(50))
    homepage_url: Mapped[str | None] = mapped_column(Text)


class Group(TimestampMixin, Base):
    """A scoped sub-set of an organization. Parent org is denormalised here
    for fast lookups; membership edges in ``memberships`` remain the source
    of truth for who belongs where.
    """

    __tablename__ = "groups"

    subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    parent_org_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(120), nullable=False)

    __table_args__ = (UniqueConstraint("parent_org_subject_id", "slug", name="uq_groups_org_slug"),)


class Membership(Base):
    """Edge table for (user ∈ org), (user ∈ group), (group ⊆ org).

    ``role`` is TEXT + CHECK rather than a PG enum because the vocabulary
    is expected to grow (e.g. per-feature roles) and we don't want to pay
    for an ``ALTER TYPE`` per addition.
    """

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("subject_id", "parent_subject_id", name="uq_memberships_edge"),
        CheckConstraint(
            "role IN ('admin','member','viewer','nested')",
            name="ck_memberships_role",
        ),
        Index("ix_memberships_parent", "parent_subject_id"),
    )
