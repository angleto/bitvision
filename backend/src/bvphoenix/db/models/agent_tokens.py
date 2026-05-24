"""AI assistant + agent token registry.

A user (medico) configures one or more *AI assistants* (e.g. "Claude in
studio", "GPT comparison"); each assistant carries an optional
provider/model_id label, a permission set, and a list of patients the
user has explicitly shared with it. The same patient can be shared with
multiple assistants — that's the benchmark / second-opinion workflow.

Per assistant there is at most one active token at a time. "Rotate
token" means: mark the current row revoked + insert a fresh row with a
new hash. The assistant identity (label, permissions, patient list)
survives a rotation untouched, so an MCP client only needs to swap the
bearer string in its config.

Authorization at runtime
------------------------
A request carrying an agent JWT resolves through ``auth/deps.py`` to:

1. SHA-256(raw_jwt) → ``AgentToken`` row;
2. row not revoked & not expired;
3. ``AgentAssistantPatient`` row exists for ``(assistant_id, patient_id)``
   if the request targets a specific patient;
4. requested permission ∈ ``AgentAssistant.permissions``.

Only the SHA-256 digest of the JWT ever hits the database — a DB dump
does not leak usable tokens, and revocation is a hash lookup.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk


class AgentAssistant(Base):
    """A single AI assistant the user has configured.

    Identity-level metadata. The token + patient list live in their own
    tables so a token rotation doesn't disrupt the rest.
    """

    __tablename__ = "agent_assistants"

    id: Mapped[uuid.UUID] = uuid_pk()
    owner_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Human-readable, unique-ish per user. The UI shows it everywhere.
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # Free-form provider label ("anthropic", "openai", "ollama", ...).
    # Informational only — the backend doesn't dispatch on this; our MCP
    # server is the same regardless of the remote LLM the user pairs.
    provider: Mapped[str | None] = mapped_column(String(64))
    # Free-form model identifier ("claude-sonnet-4-6", "gpt-4o", ...).
    model_id: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)
    # Capabilities the assistant can exercise on every patient it has
    # been granted. Same vocabulary as before — patient:read, patient:images,
    # consultation:read, consultation:write — but applied per-assistant
    # rather than per-token-per-patient.
    permissions: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    # PHI scrubbing default; can be overridden per-token if a future
    # rotation wants different behaviour.
    deidentify_on_use: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Per-assistant machine-to-machine credentials (v2.1.11). Each
    # assistant has a stable ``client_id`` and a server-generated
    # ``client_secret``. The secret is *only* persisted as its sha256
    # hash; the plaintext is shown to the operator exactly once at
    # create / rotate time. Bearer-token auth on the MCP HTTP
    # transport sha256s the incoming token and looks the row up by
    # ``client_secret_hash``.
    client_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # NULL for assistants that have not had a secret minted yet
    # (legacy rows pre-dating migration 0068, or after a "rotate
    # secret" preview that the operator cancelled).
    client_secret_hash: Mapped[str | None] = mapped_column(String(64))
    # First ~8 chars of the plaintext secret, kept so the UI can show
    # "secret …Z9_kxR3p" for identification without revealing the
    # whole credential.
    client_secret_prefix: Mapped[str | None] = mapped_column(String(16))
    # Soft-revocation: when False the bearer-hash lookup refuses to
    # promote the request to an agent token even if the hash matches.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Explicit revocation timestamp, set by ``POST /api/ai-assistants/
    # {id}/revoke``. Distinct from ``is_active`` because the latter
    # is also used for soft-delete / pause flows; ``revoked_at`` is
    # the security-event one and is checked unconditionally in
    # ``_resolve_assistant_secret``. A row with ``revoked_at`` set
    # and ``client_secret_hash = NULL`` is the "compromised secret,
    # operator hit revoke" state — even flipping ``is_active`` back
    # to True can't re-authorise the leaked plaintext.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_agent_assistants_owner", "owner_subject_id"),)


class AgentAssistantPatient(Base):
    """Join table: which patients an assistant may see.

    A patient may appear under multiple assistants (the benchmark
    workflow). When the assistant is deleted the rows cascade.
    """

    __tablename__ = "agent_assistant_patients"

    assistant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_assistants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_by_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
        nullable=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_aap_assistant", "assistant_id"),
        Index("ix_aap_patient", "patient_id"),
    )


class AgentToken(Base):
    """A bearer credential bound to one assistant.

    Multiple rows can exist per assistant over time (rotation history);
    at most one is non-revoked and non-expired at any moment. The active
    token's ``token_hash`` is what the auth layer matches.
    """

    __tablename__ = "agent_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    assistant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_assistants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SHA-256 of the JWT. Plaintext is returned exactly once at create.
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # Last 8 chars of the JWT — non-sensitive, used to disambiguate
    # rotated copies in the UI ("xxx...a1b2c3d4").
    token_tail: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_agent_tokens_assistant", "assistant_id"),
        Index("ix_agent_tokens_expires_at", "expires_at"),
    )
