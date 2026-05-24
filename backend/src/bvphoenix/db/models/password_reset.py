"""Password reset token model — forgot-password flow.

Tokens are single-use and short-lived (15 min). Only the SHA-256 hash is
persisted, so a DB leak cannot be replayed into account takeover; the
plaintext token is delivered exactly once via email. See
``docs/security-password-reset.md`` for the threat model.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.subject_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SHA-256 hex digest of the URL-safe token handed out to the user.
    # Plaintext never hits the database.
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # NULL until the reset is performed; once set, the token is dead.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Best-effort client IP captured at request time. Useful for audit
    # review when an account owner reports they didn't initiate the reset.
    requested_ip: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
