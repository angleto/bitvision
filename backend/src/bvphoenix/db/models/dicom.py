"""DICOM resource models — ImagingStudy, Series, Instance, Derivative.

In v3 the previous ``Study`` is the imaging projection of a
``ClinicalEvent`` (see ``clinical_events.py``). The DICOM-specific
fields (study_instance_uid, modalities, marketplace tier flags) live
here; the patient-timeline-level concept (kind, event_date, narrative)
lives on the parent ``ClinicalEvent``. The link is 1:1 via
``ImagingStudy.clinical_event_id`` (UNIQUE), so a query "give me
the imaging behind this event" is a single FK hop.

The series remains the logical unit of sharing, annotation,
embedding and LLM work throughout the platform (DESIGN.md §2, §5).
Instances are the raw ``.dcm`` blobs in S3 and are rarely addressed
individually from the UI. Derivatives live in a separate S3 bucket
so cache invalidation never touches originals.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import (
    CONTRIBUTION_TIER_ENUM_NAME,
    CONTRIBUTION_TIER_VALUES,
    TimestampMixin,
    UpdatedAtMixin,
    uuid_pk,
)


class ImagingStudy(TimestampMixin, UpdatedAtMixin, Base):
    __tablename__ = "imaging_studies"

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="SET NULL"),
        index=True,
    )
    # 1:1 back to the parent clinical event. Nullable on creation so
    # the ingestion pipeline can populate study + event in any order;
    # the application invariant is that every imaging study has an
    # event by the time ``ingestion_complete=true``.
    clinical_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clinical_events.id", ondelete="CASCADE"),
    )
    # DICOM UIDs are *supposed* to be globally unique, but in practice
    # vendors and anonymisers reuse them across sites and DVDs. We
    # scope uniqueness to the owning subject so two users uploading
    # studies with colliding StudyInstanceUIDs (typically from a
    # template scanner protocol or a redacted dataset) don't graft
    # data onto each other's records. The composite UNIQUE lives in
    # ``__table_args__``.
    study_instance_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    owner_org_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
        index=True,
    )
    contribution_tier: Mapped[str] = mapped_column(
        Enum(
            *CONTRIBUTION_TIER_VALUES,
            name=CONTRIBUTION_TIER_ENUM_NAME,
            create_type=False,
        ),
        nullable=False,
        server_default="t1",
    )
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_listed_for_sale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    ingestion_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    study_description: Mapped[str | None] = mapped_column(Text)
    study_date: Mapped[date | None] = mapped_column(Date)
    modalities: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, server_default="{}"
    )
    # Provenance & license — populated by the public-dataset importer
    # (``services.public_dataset``) for T4 / is_public=True rows; NULL
    # on every user-uploaded private study. Idempotency anchor for
    # re-imports lives on (source_collection, source_subject_id,
    # study_instance_uid); enforced by partial UNIQUE in migration 0004.
    source_collection: Mapped[str | None] = mapped_column(Text)
    source_subject_id: Mapped[str | None] = mapped_column(Text)
    license_spdx: Mapped[str | None] = mapped_column(Text)
    license_url: Mapped[str | None] = mapped_column(Text)
    citation_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    citation_text: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "owner_subject_id",
            "study_instance_uid",
            name="uq_imaging_studies_owner_uid",
        ),
        Index("ix_imaging_studies_uid", "study_instance_uid"),
        Index("ix_imaging_studies_public", "is_public"),
        Index("ix_imaging_studies_tier", "contribution_tier"),
        # Partial UNIQUE on clinical_event_id (1:1 to clinical_events)
        # is created by migration 0073 as a partial index — Alembic
        # is the source of truth; the ORM does not need to mirror it
        # since SQLAlchemy never autogenerates against this model.
    )


class Series(TimestampMixin, Base):
    __tablename__ = "series"

    id: Mapped[uuid.UUID] = uuid_pk()
    study_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("imaging_studies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SeriesInstanceUID is unique *within the parent study* — two
    # different studies (each owned by their own user) may carry the
    # same SeriesInstanceUID without conflict, so the constraint is
    # composite. Bare global ``unique=True`` would fail on real CDs.
    series_instance_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    series_number: Mapped[int | None] = mapped_column(Integer)
    modality: Mapped[str | None] = mapped_column(String(16))
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    series_description: Mapped[str | None] = mapped_column(Text)
    expected_instance_count: Mapped[int | None] = mapped_column(Integer)
    received_instance_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    ingestion_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    __table_args__ = (
        UniqueConstraint(
            "study_id",
            "series_instance_uid",
            name="uq_series_study_uid",
        ),
        Index("ix_series_uid", "series_instance_uid"),
    )


class Instance(TimestampMixin, Base):
    __tablename__ = "instances"

    id: Mapped[uuid.UUID] = uuid_pk()
    series_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SOPInstanceUID is unique within the parent series. Same as the
    # series-level reasoning: different DVDs reuse SOP UIDs, so the
    # global UNIQUE that used to live on this column has been replaced
    # by ``UNIQUE(series_id, sop_instance_uid)`` below.
    sop_instance_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    sop_class_uid: Mapped[str | None] = mapped_column(String(128))
    instance_number: Mapped[int | None] = mapped_column(Integer)
    s3_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_sha256: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "sop_instance_uid",
            name="uq_instances_series_uid",
        ),
        Index("ix_instances_uid", "sop_instance_uid"),
    )


class Derivative(TimestampMixin, Base):
    """Generated artefact for a series — thumbnail, MPR cache, packed NIfTI
    volume, tile pyramid. ``kind`` is a free-form string (not an enum) so
    new derivative types can be added without a schema migration.
    """

    __tablename__ = "derivatives"

    id: Mapped[uuid.UUID] = uuid_pk()
    series_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    s3_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    generator_version: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("series_id", "kind", "format", name="uq_derivatives_series_kind_format"),
        CheckConstraint("kind <> ''", name="ck_derivatives_kind_nonempty"),
    )
