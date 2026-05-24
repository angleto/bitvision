"""Patient + Document + DocumentFile (v3 refactor).

A patient is the subject of a clinical event but not necessarily a
platform user: a doctor or an organisation may manage a Health
Record on their behalf. When the patient is also a registered user
(self-owned data) the ``self_user_subject_id`` back-reference lets
the authorisation layer fold the patient's own principal set into
the access check.

v3 changes (vs. pre-v3):

* The single ``tax_id`` column is replaced by an
  ``external_identifiers`` JSONB array carrying every business
  identifier the patient has (codice fiscale, multiple MRNs, DICOM
  IssuerOfPatientID, lab IDs). UUID is the only key BitVision joins
  on; the array is descriptive metadata, never a join key. A
  Postgres GENERATED column ``cf_normalized`` materialises the CF
  for indexed lookup without forcing the application to keep a
  second column in sync.
* The ``external_id`` column (DICOM PatientID scoped per manager)
  is dropped; PatientID values now live as entries in
  ``external_identifiers`` with ``system='DICOM:Issuer:<aetitle>'``.
* The legacy ``contacts`` JSONB column is dropped: the relational
  ``patient_contacts`` table (introduced in 0071) is the sole
  source of truth.

``Document`` (formerly ``PatientDocument``) and ``DocumentFile``
(formerly ``PatientDocumentFile``) carry the FRBR Manifestation
layer. The single overloaded ``document_type`` enum is replaced by
three orthogonal FK lookups against the catalog tables (see
``document_catalog.py``):

* ``kind_id``       — what the document is clinically
* ``provenance_id`` — how it reached us
* ``authority_id``  — trust ladder (original/derived/...)

A new ``original_blob_hash`` column records the SHA-256 of the
*originating artefact* (distinct from ``content_sha256`` which
hashes the bytes we hold) so the similarity-based dedup pass can
collapse copies under their canonical original.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import TimestampMixin, UpdatedAtMixin, uuid_pk


class Patient(TimestampMixin, Base):
    __tablename__ = "patients"

    id: Mapped[uuid.UUID] = uuid_pk()
    managed_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
        index=True,
    )
    self_user_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.subject_id", ondelete="SET NULL"),
        unique=True,
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Demographics
    birth_date: Mapped[date | None] = mapped_column(Date)
    sex: Mapped[str | None] = mapped_column(String(1))  # M / F / O
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    blood_type: Mapped[str | None] = mapped_column(String(8))
    birth_place_city: Mapped[str | None] = mapped_column(String(128))
    birth_place_province: Mapped[str | None] = mapped_column(String(8))
    asl_code: Mapped[str | None] = mapped_column(String(16))
    asl_name: Mapped[str | None] = mapped_column(String(255))
    allergies: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # Provenance for the ``notes`` field. Set by the patient PATCH
    # handler only when ``notes`` is among the changed fields, so
    # the sticky ClinicalNotes panel can show "edited by X · Y ago"
    # without conflating it with demographics edits. NULL on legacy
    # rows (pre migration 0094).
    notes_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    notes_updated_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
        nullable=True,
    )

    # External identifiers — array of FHIR-shaped Identifier objects.
    # Never used as a join key; UUID ``id`` is the only key BitVision
    # FK-joins on. See migration 0076 for the v3 design rationale.
    external_identifiers: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'[]'::jsonb")
    )

    # Materialised by the DBMS from ``external_identifiers``; supports
    # the indexed CF lookup without re-introducing a unique constraint
    # (per-record duplicates are an informational signal in the UI,
    # not a DB error). The column is declared as ``GENERATED ALWAYS AS
    # ... STORED`` in migration 0076; we mirror that with ``Computed``
    # at the ORM level so SQLAlchemy excludes it from INSERT / UPDATE
    # statements (the DBMS owns the value). The expression here is
    # never emitted as DDL — alembic is the source of truth.
    cf_normalized: Mapped[str | None] = mapped_column(
        String(16),
        Computed(
            "UPPER(jsonb_path_query_first("
            "external_identifiers, "
            "'$[*] ? (@.type == \"fiscal-code\").value'"
            ") #>> '{}')",
            persisted=True,
        ),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(external_identifiers) = 'array'",
            name="ck_patients_external_identifiers_array",
        ),
        Index(
            "ix_patients_cf_normalized",
            "cf_normalized",
            postgresql_where=sql_text("cf_normalized IS NOT NULL"),
        ),
    )


class DocumentFile(Base):
    """One file attached to a document.

    A document can have multiple files (e.g. 5 jpeg scans of a single
    paper report, or a multi-page PDF rendered as N images). Used to be
    ``patient_document_files``; renamed in 0075.
    """

    __tablename__ = "document_files"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    file_s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    file_content_type: Mapped[str | None] = mapped_column(String(128))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Document(TimestampMixin, UpdatedAtMixin, Base):
    """Standalone document attached to a Health Record (Manifestation).

    Storage axis of the three-axis model (see
    ``docs/data-model.md §0``): the held artefact (PDF, scanned page,
    DVD label, inline note). Orthogonal to the temporal axis
    (``ClinicalEvent``, ``CarePhase``) and the organisational axis
    (``Folder``). Linked to events via ``ContentDocumentLink``.

    Document was previously ``patient_documents`` with a single
    overloaded ``document_type`` column. v3 splits the type into
    three FK lookups against the catalog tables:

    * ``kind_id``       — clinical class (radiology_report, lab_result, ...)
    * ``provenance_id`` — vector / form (digital_native_pdf, scanned_paper, ...)
    * ``authority_id``  — trust ladder (original, derived, ...)

    A document can hold:
      - a single inline ``text`` body (clinical note pasted by the doctor),
      - a single legacy file via ``file_s3_key`` (pre-multi-file uploads),
      - or multiple files via the ``document_files`` table.

    Soft-delete: ``deleted_at`` flips the row to a tombstone (excluded
    from default reads), ``purge_after`` is the wall-clock deadline
    after which the worker hard-deletes both metadata and underlying
    files, ``delete_reason`` is recorded for audit. Retention default
    is 30 days; the cron worker honours it.
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )

    # 3-axis taxonomy (v3) — all FK to the controlled-vocabulary catalog
    kind_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("document_kinds.id", ondelete="RESTRICT"),
        nullable=False,
        server_default=sql_text("'unclassified'"),
    )
    provenance_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("document_provenances.id", ondelete="RESTRICT"),
        nullable=False,
        server_default=sql_text("'manual_entry'"),
    )
    authority_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("document_authorities.id", ondelete="RESTRICT"),
        nullable=False,
        server_default=sql_text("'original'"),
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    file_s3_key: Mapped[str | None] = mapped_column(Text)
    file_content_type: Mapped[str | None] = mapped_column(String(128))
    document_date: Mapped[date | None] = mapped_column(Date)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    # Hash of the originating artefact even when our stored copy has
    # been re-encoded (OCR, derivative). Defaults to ``content_sha256``
    # at ingestion; the similarity-based dedup pass updates it for
    # manifestations confirmed to derive from a known original.
    original_blob_hash: Mapped[str | None] = mapped_column(String(64))

    # Soft-delete (Sprint 3, ADR 0006). Default state is "live": only
    # the DELETE endpoint populates these.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_reason: Mapped[str | None] = mapped_column(String(255))

    # Concurrency token; ETag header on read, If-Match on write.
    etag: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=sql_text("gen_random_uuid()"),
    )

    __table_args__ = (
        Index("ix_documents_kind", "patient_id", "kind_id"),
        Index("ix_documents_authority", "patient_id", "authority_id"),
        Index("ix_documents_provenance", "patient_id", "provenance_id"),
        Index(
            "ix_documents_patient_sha",
            "patient_id",
            "content_sha256",
        ),
        Index(
            "ix_documents_original_blob_hash",
            "patient_id",
            "original_blob_hash",
            postgresql_where=sql_text("original_blob_hash IS NOT NULL"),
        ),
        # Partial index over live rows so the default ``patient → docs``
        # query can skip tombstones cheaply.
        Index(
            "ix_documents_live",
            "patient_id",
            postgresql_where=sql_text("deleted_at IS NULL"),
        ),
        # Index over tombstones whose retention window has expired —
        # backs the purge worker.
        Index(
            "ix_documents_purge_due",
            "purge_after",
            postgresql_where=sql_text("deleted_at IS NOT NULL"),
        ),
    )


class DocumentStudyLink(Base):
    """N:M document ↔ study association.

    Re-introduced in migration 0083 after the v3 phase 3b retire of
    the legacy table. The v3 ``content_document_links`` flow remains
    the canonical surface for richer report-content provenance, but
    the lightweight ``document <-> study`` association is what the
    fascicolo navigation + the MCP ``link_document_to_study`` tool
    actually need to associate a freshly uploaded report with the
    imaging study it was authored for.
    """

    __tablename__ = "document_study_links"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    study_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("imaging_studies.id", ondelete="CASCADE"),
        nullable=False,
    )
    link_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "document_id", "study_id", "link_kind", name="uq_document_study_links_triple"
        ),
        # Updated in 0089: ``primary_report`` replaces the legacy
        # ``report_of`` participle (rename in-place); ``addendum`` and
        # ``second_opinion`` are new role-nouns that capture the
        # multi-referto case (a study can carry one primary report plus
        # any number of addenda or second opinions). The reference
        # roles ``extracted_from`` / ``cites`` / ``mentions`` are
        # unchanged. A partial unique index
        # ``uq_document_study_links_primary_per_study`` enforces
        # exactly one primary per study; multiple addenda are allowed.
        CheckConstraint(
            "link_kind IN ('primary_report','addendum','second_opinion',"
            "'extracted_from','cites','mentions')",
            name="ck_document_study_links_kind",
        ),
        Index("ix_document_study_links_document", "document_id"),
        Index("ix_document_study_links_study", "study_id"),
        Index(
            "uq_document_study_links_primary_per_study",
            "study_id",
            unique=True,
            postgresql_where="link_kind = 'primary_report'",
        ),
    )


__all__ = ["Document", "DocumentFile", "DocumentStudyLink", "Patient"]
