"""Share link model — public entry point to a grant (docs/sharing.md §2).

A share link is a URL-safe token that activates a specific grant for
anyone who possesses it, optionally protected by a password. The grant
itself controls what permissions the token confers and when it expires.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

PUBLIC_SUBJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Share-link delivery mode. ``claim`` is the default: the link is a
# magic-link onto a claimable account (B). ``anonymous`` is a
# deliberately opt-in mode where the link itself is the credential
# (A); writes are attributed via ActorContext.kind='link' and are
# always badged as such in the versioning history.
SHARE_LINK_MODES: tuple[str, ...] = ("claim", "anonymous")


class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[uuid.UUID] = uuid_pk()
    grant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("grants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    # Recipient identification, captured at creation for audit and
    # OOB password delivery. Required (at the application layer) when
    # ``mode='anonymous'``; optional in ``mode='claim'`` where the
    # email is collected as part of account claim instead.
    recipient_name: Mapped[str | None] = mapped_column(String(255))
    recipient_email: Mapped[str | None] = mapped_column(String(255))
    recipient_phone: Mapped[str | None] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'claim'"))
    max_uses: Mapped[int | None] = mapped_column(Integer)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Set when an anonymous link is converted to a real account via
    # ``POST /api/share-links/{token}/claim``. The historical
    # ``commits.share_link_id`` rows stay attributed to the link
    # (so the "modality A" badge survives the conversion); future
    # writes from the same person flow through the normal user path
    # because the claim mints a real session JWT.
    claimed_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    # Set the first time the recipient explicitly confirms receipt of
    # the shared resource (POST /shared/{token}/confirm-receipt).
    # Closes the audit chain for the grantor: created → emailed →
    # accessed → received. NULL until confirmed; subsequent confirms
    # are idempotent (the timestamp doesn't move).
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    # Cached export job for fast first-click delivery. Set when the
    # share is created (study scope only today; patient/folder
    # shares re-enqueue per click as they always have). Null after
    # the job's cleanup cron runs — the share falls back to a
    # fresh enqueue at click time. See migration 0095.
    prepared_job_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Number of *complete* downloads. Bumped on a successful 200
    # full-body delivery, NOT on 206 Partial Content nor on landing
    # opens (the latter goes to ``use_count``). Lets the grantor
    # see real consumption.
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # When set, claiming this link (or sharing with a known subject)
    # auto-creates a ``WalletSponsorship`` row so the recipient can
    # run AI on the shared record at the sponsor's expense, up to
    # this many cents. NULL = recipient pays from their own wallet.
    ai_sponsorship_cap_cents: Mapped[int | None] = mapped_column(BigInteger)
    ai_sponsorship_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("wallet_sponsorships.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
