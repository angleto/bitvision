"""Short-lived OAuth authorization-code store.

The remote MCP transport (mcp-http) speaks OAuth 2.1 + PKCE so that
hosts like Claude.ai's custom-connector dialog can complete the
standard handshake before they start carrying the per-assistant
``client_secret`` as a Bearer header (see ADR 0019 +
``mcp/src/bvmcp/oauth_shim.py``).

The shim mints an authorization code at ``GET /authorize`` and
consumes it on ``POST /token``. With multiple mcp-http replicas an
in-memory store is the wrong primitive: ``/authorize`` may land on
pod A and ``/token`` on pod B for the same client. We persist the
codes in phoenix's database instead — both endpoints call
``/api/internal/oauth-code/{mint,consume}`` on the backend.

Codes are single-use, short-lived (default TTL 10 min), and trimmed
to a max length of 64 chars to stay below the GIN-index threshold.
The PKCE ``code_challenge`` (S256, 43-character base64url) is bound
at mint and verified by the shim at consume; the backend just
returns the metadata it stored.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base


class OAuthCode(Base):
    """A pending authorization code waiting for ``/token`` redemption."""

    __tablename__ = "oauth_codes"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # GC sweep on TTL expiry (also used by the resolve-by-code lookup).
        Index("ix_oauth_codes_expires_at", "expires_at"),
    )


__all__ = ["OAuthCode"]
