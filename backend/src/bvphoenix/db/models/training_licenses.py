"""Training-license ORM models (F10.1).

Three tables covering the aggregate-assembly workflow:

* :class:`TrainingLicense` — commercial deal + lifecycle status.
* :class:`LicensedDataset` — assembled dataset tied to a licence.
* :class:`DatasetStudy` — membership join: which study went into
  which dataset, with the anonymised S3 location and the
  contributor at assembly time (for the F10.4 revenue share).

F10.2 (k-anonymity ≥ 5 enforcement) is the guard that populates
``LicensedDataset.k_anon`` at assembly time. F10.3 wires the DUC
workflow into ``TrainingLicense.duc_request_id``. F10.4 reads
``DatasetStudy.contributor_subject_id`` and ``LicensedDataset.license
→ price_usd_cents`` to compute the 50/50 payout split.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base
from bvphoenix.db.models._common import uuid_pk

TRAINING_LICENSE_STATUSES: tuple[str, ...] = (
    "draft",
    "pending_duc",
    "approved",
    "signed",
    "revoked",
)


class TrainingLicense(Base):
    __tablename__ = "training_licenses"

    id: Mapped[uuid.UUID] = uuid_pk()
    licensee_name: Mapped[str] = mapped_column(String(255), nullable=False)
    licensee_email: Mapped[str] = mapped_column(String(320), nullable=False)
    price_usd_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    term_months: Mapped[int] = mapped_column(Integer, nullable=False, server_default="12")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    duc_request_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','pending_duc','approved','signed','revoked')",
            name="ck_training_licenses_status",
        ),
        CheckConstraint("price_usd_cents >= 0", name="ck_training_licenses_price_nonneg"),
        CheckConstraint("term_months > 0", name="ck_training_licenses_term_positive"),
        Index("ix_training_licenses_status", "status"),
    )


class LicensedDataset(Base):
    __tablename__ = "licensed_datasets"

    id: Mapped[uuid.UUID] = uuid_pk()
    license_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("training_licenses.id", ondelete="CASCADE"),
        nullable=False,
    )
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    study_count: Mapped[int] = mapped_column(Integer, nullable=False)
    contributor_count: Mapped[int] = mapped_column(Integer, nullable=False)
    k_anon: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_s3_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("study_count >= 0", name="ck_licensed_datasets_count"),
        CheckConstraint(
            "contributor_count >= 0",
            name="ck_licensed_datasets_contributor_count",
        ),
        CheckConstraint("k_anon >= 1", name="ck_licensed_datasets_k_anon"),
        Index("ix_licensed_datasets_license", "license_id"),
    )


class DatasetStudy(Base):
    __tablename__ = "dataset_studies"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("licensed_datasets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    study_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("imaging_studies.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    contributor_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )
    anonymized_s3_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    anonymized_s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_dataset_studies_contributor", "contributor_subject_id"),)
