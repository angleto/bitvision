"""ReportContent + the n:m link to documents + citations.

``ReportContent`` is the v3 Expression layer (FRBR analogy): a
structured narrative about a clinical event, distinct from both the
event itself and from any specific artefact (PDF / scan / photo)
that carries it. The same content can be backed by N documents
(``content_document_links`` with ``role='extracted_from'``) and the
same document can yield N contents (a discharge letter PDF parsed
into discharge_summary + diagnosis + therapy).

Authority ladder (FK ``authority_id`` into ``document_authorities``):

* ``original``           — narrative as authored by the issuing
                            clinician (the radiologist's own report)
* ``derived``            — re-extracted from a derived document
                            (OCR of a scan)
* ``canonical_synthesis`` — BitVision's curated synthesis citing the
                            other contents

Workflow (column ``status``), gated by a CHECK constraint matched to
the authority:

* ``original`` / ``derived``: ``extracted_auto → endorsed → stale``
* ``canonical_synthesis``: ``draft → final → signed → (stale | rejected)``

The ``synthesis:sign`` scope is HUMAN-ONLY at the API layer (no
agent_token may flip ``status='signed'``). Supersede chains link
``superseded_by_id`` so the lineage is followable without a
versioning side table.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

REPORT_CONTENT_AUTHORITIES: tuple[str, ...] = (
    "original",
    "derived",
    "canonical_synthesis",
    # ``stale`` is also a valid catalog row but never used as a
    # *starting* authority — it appears only when an entire content
    # gets retired, in which case the row's ``status`` is what carries
    # the terminal state. Kept here for type completeness.
    "stale",
)

REPORT_CONTENT_STATUSES: tuple[str, ...] = (
    "extracted_auto",
    "endorsed",
    "draft",
    "final",
    "signed",
    "rejected",
    "stale",
)

REPORT_CONTENT_AUTHOR_KINDS: tuple[str, ...] = ("human", "agent")

CONTENT_DOCUMENT_LINK_ROLES: tuple[str, ...] = (
    "extracted_from",
    "cites",
    "mentions",
)

CITATION_TARGET_KINDS: tuple[str, ...] = (
    "clinical_event",
    "imaging_study",
    "series",
    "report_content",
    "document",
    "marker",
    "lab_value",
)


class ReportContent(Base):
    __tablename__ = "report_contents"

    id: Mapped[uuid.UUID] = uuid_pk()
    clinical_event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clinical_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    authority_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("document_authorities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)

    # ---- common content ----------------------------------------------------
    language: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'it'"))
    title: Mapped[str | None] = mapped_column(String(255))
    narrative_md: Mapped[str | None] = mapped_column(Text)
    structured_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # ---- authoring ---------------------------------------------------------
    created_by_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    author_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    # Modern per-assistant client_secret path: legacy ``agent_token_id``
    # is NULL, this FK carries the assistant identity so the audit
    # chain stays traceable end-to-end.
    agent_assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_assistants.id", ondelete="SET NULL"),
    )
    model_id: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(64))

    # ---- extraction provenance (NULL for canonical_synthesis) -------------
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parser_version: Mapped[str | None] = mapped_column(String(64))

    # ---- endorsement -------------------------------------------------------
    endorsed_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    endorsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- synthesis-specific ------------------------------------------------
    findings_md: Mapped[str | None] = mapped_column(Text)
    recommendations_md: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    token_usage: Mapped[dict | None] = mapped_column(JSONB)
    deidentified_input: Mapped[bool | None] = mapped_column(Boolean)
    consent_snapshot: Mapped[list | None] = mapped_column(JSONB)

    # ---- signature workflow ------------------------------------------------
    signed_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_reason: Mapped[str | None] = mapped_column(Text)

    # ---- supersede chain ---------------------------------------------------
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("report_contents.id", ondelete="SET NULL"),
    )
    supersede_reason: Mapped[str | None] = mapped_column(Text)

    # ---- bookkeeping -------------------------------------------------------
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "author_kind IN ('human','agent')",
            name="ck_report_contents_author_kind",
        ),
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in REPORT_CONTENT_STATUSES) + ")",
            name="ck_report_contents_status",
        ),
        CheckConstraint(
            "("
            "  authority_id IN ('original','derived')"
            "  AND status IN ('extracted_auto','endorsed','stale')"
            ") OR ("
            "  authority_id = 'canonical_synthesis'"
            "  AND status IN ('draft','final','signed','rejected','stale')"
            ")",
            name="ck_report_contents_authority_status",
        ),
        CheckConstraint(
            "(status <> 'signed') OR ("
            "  signed_by_subject_id IS NOT NULL AND signed_at IS NOT NULL"
            ")",
            name="ck_report_contents_signed_complete",
        ),
        CheckConstraint(
            "(status <> 'rejected') OR (rejected_reason IS NOT NULL)",
            name="ck_report_contents_rejected_reason",
        ),
        Index("ix_report_contents_event", "clinical_event_id"),
        Index("ix_report_contents_authority", "authority_id"),
        Index("ix_report_contents_status", "status"),
        Index(
            "ix_report_contents_event_authority",
            "clinical_event_id",
            "authority_id",
        ),
        Index(
            "ix_report_contents_active_canonical",
            "clinical_event_id",
            postgresql_where=text("authority_id = 'canonical_synthesis' AND status = 'signed'"),
        ),
    )


class ContentDocumentLink(Base):
    """n:m bridge between ``report_contents`` and ``documents``.

    ``role='extracted_from'`` records the strongest link: this content
    was parsed out of that document. ``cites`` is the weaker
    "references as evidence" used by canonical_syntheses pointing at
    the originals they consolidate. ``mentions`` is for transient
    references that should not promote the document into the content's
    derivation chain.
    """

    __tablename__ = "content_document_links"

    id: Mapped[uuid.UUID] = uuid_pk()
    report_content_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("report_contents.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "role IN (" + ",".join(f"'{r}'" for r in CONTENT_DOCUMENT_LINK_ROLES) + ")",
            name="ck_content_document_links_role",
        ),
        UniqueConstraint(
            "report_content_id",
            "document_id",
            "role",
            name="uq_content_document_links_triple",
        ),
        Index("ix_content_document_links_content", "report_content_id"),
        Index("ix_content_document_links_document", "document_id"),
    )


class ReportContentCitation(Base):
    """Fine-grained citation pointer from a ReportContent to evidence.

    ``target_kind`` plus ``target_id`` polymorphically address any
    artefact in the v3 model. The fine-grained columns
    (``page``/``bbox``/``file_id``/``slice_idx``/
    ``annotation_marker_idx``/``lab_value_id``) pin the citation to
    a specific pixel / paragraph / row when the target is large
    enough to warrant pinpointing. Cross-patient invariant (citation
    target's patient must equal report_content's patient) is
    application-level — see service layer.
    """

    __tablename__ = "report_content_citations"

    id: Mapped[uuid.UUID] = uuid_pk()
    report_content_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("report_contents.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict | None] = mapped_column(JSONB)
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_files.id", ondelete="SET NULL"),
    )
    slice_idx: Mapped[int | None] = mapped_column(Integer)
    annotation_marker_idx: Mapped[int | None] = mapped_column(Integer)
    lab_value_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "target_kind IN (" + ",".join(f"'{k}'" for k in CITATION_TARGET_KINDS) + ")",
            name="ck_report_content_citations_target_kind",
        ),
        Index("ix_report_content_citations_content", "report_content_id"),
        Index(
            "ix_report_content_citations_target",
            "target_kind",
            "target_id",
        ),
    )


__all__ = [
    "CITATION_TARGET_KINDS",
    "CONTENT_DOCUMENT_LINK_ROLES",
    "REPORT_CONTENT_AUTHORITIES",
    "REPORT_CONTENT_AUTHOR_KINDS",
    "REPORT_CONTENT_STATUSES",
    "ContentDocumentLink",
    "ReportContent",
    "ReportContentCitation",
]
