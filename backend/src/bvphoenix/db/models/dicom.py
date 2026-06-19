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
from datetime import date, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
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

# Contrast / acquisition phase of a CT (or dynamic-MR) series, relative to
# IV contrast injection. A phase is a property OF a series: in multiphase
# contrast CT each phase is acquired as its own SeriesInstanceUID, so it
# lives on the series row, NOT on the care-timeline ``CarePhase`` (an
# unrelated clinical concept that already owns the bare word "phase" in
# this codebase — keep the names disjoint, hence ``acquisition_phase``).
# NULL = not yet classified. Order matters: it is the canonical temporal
# ordering of phases. See ``services.contrast_phase``.
ACQUISITION_PHASES: tuple[str, ...] = (
    "unenhanced",  # pre-contrast / basal (no IV agent yet)
    "arterial",  # late-arterial / hepatic-arterial (~20-40s)
    "portal_venous",  # portal-venous / hepatic-venous (~60-80s)
    "delayed",  # delayed / equilibrium (~3-5 min+)
    "hepatobiliary",  # hepatobiliary / HBP (Gd-EOB, ~20 min)
    "corticomedullary",  # renal corticomedullary (~30-40s)
    "nephrographic",  # renal nephrographic (~90-120s)
    "excretory",  # renal excretory / urographic (~3-10 min)
    "dynamic",  # generic dynamic / perfusion / 4D frame
    "other",  # classified but not one of the above
)

# Who set ``acquisition_phase``: the automatic classifier (low-confidence
# values render as a "confirm" candidate until a human approves) or a
# human override (which pins the value and clears ``phase_confidence``).
PHASE_SOURCES: tuple[str, ...] = ("auto", "human")


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
    # Dual-config full-text vector: the Italian-stemmed lexemes (so
    # "polmoni" matches "polmone") OR'd with the raw 'simple' tokens
    # (so DICOM code-strings / acronyms like "T2 FLAIR" still match
    # exactly). Generated + stored, mirroring text_chunks.text_tsv.
    study_description_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('italian'::regconfig, coalesce(study_description, '')) "
            "|| to_tsvector('simple'::regconfig, coalesce(study_description, ''))",
            persisted=True,
        ),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_subject_id",
            "study_instance_uid",
            name="uq_imaging_studies_owner_uid",
        ),
        Index("ix_imaging_studies_uid", "study_instance_uid"),
        Index("ix_imaging_studies_public", "is_public"),
        Index("ix_imaging_studies_tier", "contribution_tier"),
        Index("ix_studies_description_tsv", "study_description_tsv", postgresql_using="gin"),
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
    # ---- Acquisition timing & contrast phase ----------------------------
    # Representative time-of-day of the series acquisition (DICOM
    # AcquisitionTime 0008,0032, falling back to SeriesTime / ContentTime),
    # persisted at ingest from the already-parsed header. ``study_date`` is
    # DATE-only, so this is the only intra-study temporal ordering key for
    # the contrast-phase classifier. NULL when the source tag is absent.
    acquisition_time_of_day: Mapped[time | None] = mapped_column(Time)
    # Contrast agent (0018,0010) if any was administered; NULL/empty is the
    # unenhanced signal. ``contrast_bolus_start_time`` (0018,1042) is the
    # injection start; AcquisitionTime - start = seconds-post-injection,
    # the strongest phase discriminator when the vendor records it.
    contrast_bolus_agent: Mapped[str | None] = mapped_column(Text)
    contrast_bolus_start_time: Mapped[time | None] = mapped_column(Time)
    # Classified contrast phase (see ACQUISITION_PHASES). Derived by
    # ``services.contrast_phase`` from sibling-series timing + description,
    # or set by a human (``phase_source='human'``). NULL = not classified.
    acquisition_phase: Mapped[str | None] = mapped_column(String(24))
    # 0..1 classifier confidence; low values render as a "confirm" badge
    # and block auto-finalised washout. NULL for human-set or unclassified.
    phase_confidence: Mapped[float | None] = mapped_column(Float)
    phase_source: Mapped[str | None] = mapped_column(String(8))
    expected_instance_count: Mapped[int | None] = mapped_column(Integer)
    received_instance_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    ingestion_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # Dual-config FTS (Italian-stemmed || simple); see ImagingStudy above.
    series_description_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('italian'::regconfig, coalesce(series_description, '')) "
            "|| to_tsvector('simple'::regconfig, coalesce(series_description, ''))",
            persisted=True,
        ),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "study_id",
            "series_instance_uid",
            name="uq_series_study_uid",
        ),
        CheckConstraint(
            "acquisition_phase IS NULL OR acquisition_phase IN ("
            + ",".join(f"'{p}'" for p in ACQUISITION_PHASES)
            + ")",
            name="ck_series_acquisition_phase",
        ),
        CheckConstraint(
            "phase_source IS NULL OR phase_source IN ("
            + ",".join(f"'{s}'" for s in PHASE_SOURCES)
            + ")",
            name="ck_series_phase_source",
        ),
        CheckConstraint(
            "phase_confidence IS NULL OR (phase_confidence >= 0 AND phase_confidence <= 1)",
            name="ck_series_phase_confidence_range",
        ),
        Index("ix_series_uid", "series_instance_uid"),
        Index("ix_series_description_tsv", "series_description_tsv", postgresql_using="gin"),
        # Phase-faceted study queries ("show me the portal-venous series")
        # and the classifier's per-study sweep both filter on phase.
        Index("ix_series_acquisition_phase", "acquisition_phase"),
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
    # Which co-located sub-stack of the series this derivative is, for
    # volumes packed from a multi-stack DICOM series (Philips mDIXON
    # Water/Fat/In-phase/Out-of-phase, multi-echo, DWI b-values; see
    # ``services.volumes.partition_substacks``). 0 = the primary/default
    # stack (and the only stack for the overwhelmingly common single-stack
    # series), 1.. = the extra contrasts. Part of the uniqueness key so a
    # series can hold one derivative per (kind, format, stack).
    stack_index: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    s3_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    generator_version: Mapped[str | None] = mapped_column(String(64))
    # Real patient-space geometry of a packed volume_f32 derivative,
    # computed at pack time from the sorted DICOM datasets (see
    # ``services.volumes.compute_volume_geometry``). Served back to the
    # viewer as X-Volume-* response headers so it builds the Cornerstone
    # volume in true LPS space instead of a fabricated identity frame.
    # NULL for non-volume derivatives and for legacy packs predating this
    # column (the viewer falls back to the identity frame). Shape:
    # ``{"origin": [3]|None, "direction": [9]|None, "frame_of_reference_uid": str|None}``.
    geometry: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "kind",
            "format",
            "stack_index",
            name="uq_derivatives_series_kind_format_stack",
        ),
        CheckConstraint("kind <> ''", name="ck_derivatives_kind_nonempty"),
    )
