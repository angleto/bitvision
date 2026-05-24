"""Controlled-vocabulary catalog tables for the document 3-axis taxonomy.

Three small lookup tables back the v3 ``documents`` triple of FK
columns (``kind_id``, ``provenance_id``, ``authority_id``). The
vocabulary is data-driven so adding a new term ships as a YAML seed
under ``backend/seeds/`` rather than as an alembic migration; the
seed is applied idempotently on every deploy.

Each row carries an i18n display map (``display_name['it']``,
``display_name['en']``) and an ``is_active`` flag for soft
deprecation: historical documents that still reference a retired
kind / provenance / authority remain readable, the value just
disappears from the create / update pickers.

Migration 0072 ships the bootstrap set; see its docstring for the
full list and design rationale.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base


class _CatalogRow:
    """Shared columns for the three catalog tables."""

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class DocumentKind(_CatalogRow, Base):
    """What the document is clinically — radiology_report, lab_result …"""

    __tablename__ = "document_kinds"

    loinc_code: Mapped[str | None] = mapped_column(String(32))
    fhir_resource: Mapped[str | None] = mapped_column(String(64))


class DocumentProvenance(_CatalogRow, Base):
    """How the document reached us — digital_native_pdf, scanned_paper …"""

    __tablename__ = "document_provenances"

    is_digital: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_imaging: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class DocumentAuthority(_CatalogRow, Base):
    """Trust ladder — original / derived / canonical_synthesis / stale.

    ``trust_score`` orders the picker ("most trustworthy first") but
    does not gate any backend logic; authorisation reads ``id``.
    """

    __tablename__ = "document_authorities"

    trust_score: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50"))


__all__ = ["DocumentAuthority", "DocumentKind", "DocumentProvenance"]
