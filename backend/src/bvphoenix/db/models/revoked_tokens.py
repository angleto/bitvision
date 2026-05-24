"""Token revocation registry (ADR 0016).

Every JWT minted by the backend now carries a ``jti`` claim (UUID). On
each authenticated request, ``require_user`` looks ``jti`` up in this
table; presence ⇒ 401 ``token_revoked``.

Schema decisions:

* ``jti`` is the PK because we expect O(1) lookups by ID.
* ``expires_at`` mirrors the JWT ``exp`` claim; the daily cleanup
  worker drops rows whose expiry has passed (the JWT signature check
  rejects them anyway).
* ``revoked_by_subject_id`` gives auditors a "who pulled the trigger"
  field separate from the token's owner.
* ``reason`` is free-form — typical values: ``user_logout``,
  ``incident_response``, ``rotation``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"

    jti: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    reason: Mapped[str | None] = mapped_column(Text)
    # Optional: the ``sub`` of the revoked token. Useful for "revoke
    # all tokens of subject X" workflows where we mass-insert by
    # iterating over an outstanding-token roster.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    typ: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        Index("ix_revoked_tokens_expires", "expires_at"),
        Index("ix_revoked_tokens_subject", "subject_id"),
    )
