"""User-supplied LLM API keys (BYOK, F7).

Each row encrypts a single provider API key at rest using AES-GCM.
The plaintext never lands in the DB: only the GCM nonce and the
ciphertext (which embeds the auth tag) are stored, so a read-only
snapshot of the table cannot be replayed without the master key.

Lifecycle
---------

* ``granted_at`` — when the user added the key.
* ``last_used_at`` — touched on every successful LLM call; useful for
  cleanup dashboards ("this key hasn't been used in 90 days, suggest
  rotation").
* ``revoked_at`` — append-only semantics. Revoking clears the active
  (user, provider) row by setting ``revoked_at``; inserting a new one
  (rotation) creates a fresh row and closes the previous one in the
  same transaction. We never delete: the row's *existence* is part of
  the audit trail.

At most one row with ``revoked_at IS NULL`` per (user_subject_id,
provider) is enforced by a unique partial index.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

# Providers whose keys the backend can actually use. Kept conservative:
# adding a new provider means touching the LLM factory anyway, so an
# unknown entry in the DB would be a latent bug.
USER_API_KEY_PROVIDERS: tuple[str, ...] = ("anthropic",)


class UserAPIKey(Base):
    __tablename__ = "user_api_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # AES-GCM nonce (12 random bytes per row).
    key_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    # AES-GCM ciphertext including the 16-byte auth tag at the tail.
    key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_user_api_keys_active",
            "user_subject_id",
            "provider",
            unique=True,
            postgresql_where="revoked_at IS NULL",
        ),
        Index("ix_user_api_keys_user_provider", "user_subject_id", "provider"),
    )
