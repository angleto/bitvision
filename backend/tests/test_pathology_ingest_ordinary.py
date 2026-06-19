"""Ingest of ordinary pathology images (gross photos / micrographs).

The WSI viewer covers more than scanned slides: gross specimen photos and
static micrographs arrive as plain RGB images (JPEG/PNG/TIFF) that
OpenSlide cannot open. ``import_pathology_slide`` routes those through a
Pillow reader and stamps ``slide_class``; the ``tile_wsi`` worker then
builds their pyramid so they share the deep-zoom viewer.

Also asserts the ``ck_pathology_slides_dzi_ready_complete`` CHECK rejects
a half-written ready row (defence in depth, like the label-redacted CHECK).

Hermetic: a fake S3 storage records uploads; the image is a real tiny PNG
written to a tmp path. DB-touching, so skipped without Postgres.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import ClinicalEvent, PathologySlide, Patient
from bvphoenix.db.models.principals import Subject
from bvphoenix.services.pathology_import import (
    PathologyImportSource,
    import_pathology_slide,
)
from tests.conftest import skip_if_no_db


@dataclass
class _FakeUploadResult:
    bucket: str
    key: str
    size_bytes: int


class _FakeS3Storage:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, int]] = []

    def ensure_bucket(self, _name: str) -> None:
        return None

    def upload_file(self, path: Path, *, bucket: str, key: str) -> _FakeUploadResult:
        size = path.stat().st_size
        self.uploads.append((bucket, key, size))
        return _FakeUploadResult(bucket=bucket, key=key, size_bytes=size)

    def upload_bytes(self, data: bytes, *, bucket: str, key: str) -> _FakeUploadResult:
        self.uploads.append((bucket, key, len(data)))
        return _FakeUploadResult(bucket=bucket, key=key, size_bytes=len(data))


@pytest.fixture
def sync_session() -> Session:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    return Session(engine)


def _make_owner_and_patient(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    owner = Subject(id=uuid.uuid4(), kind="user", display_name="path-owner")
    patient = Patient(id=uuid.uuid4(), managed_by_subject_id=None)
    session.add(owner)
    session.add(patient)
    session.flush()
    return owner.id, patient.id


def _cleanup(session: Session, *, patient_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    session.execute(delete(PathologySlide).where(PathologySlide.patient_id == patient_id))
    session.execute(delete(ClinicalEvent).where(ClinicalEvent.patient_id == patient_id))
    session.execute(delete(Patient).where(Patient.id == patient_id))
    session.execute(delete(Subject).where(Subject.id == owner_id))
    session.commit()


@skip_if_no_db
def test_ordinary_png_ingests_as_micrograph(sync_session: Session, tmp_path: Path) -> None:
    """A plain PNG is read via Pillow, classed 'micrograph', dims captured,
    pyramid_levels=1, mpp absent, dzi not yet built."""
    img_path = tmp_path / "micrograph.png"
    Image.new("RGB", (640, 480), (200, 100, 100)).save(img_path)

    storage = _FakeS3Storage()
    owner_id, patient_id = _make_owner_and_patient(sync_session)
    try:
        result = import_pathology_slide(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="bvphoenix-raw",
            source=PathologyImportSource(
                path=img_path, owner_subject_id=owner_id, patient_id=patient_id
            ),
        )
        sync_session.commit()
        assert result.created is True

        slide = sync_session.execute(
            select(PathologySlide).where(PathologySlide.id == result.slide_id)
        ).scalar_one()
        assert slide.slide_class == "micrograph"  # inferred from .png
        assert slide.source_format == "png"
        assert slide.base_width == 640
        assert slide.base_height == 480
        assert slide.pyramid_levels == 1
        assert slide.mpp_x is None
        assert slide.dzi_ready is False  # the worker builds the pyramid later
        assert slide.s3_macro_key is None  # ordinary images have no macro
        # source + thumbnail uploaded (no macro).
        keys = [k for (_b, k, _s) in storage.uploads]
        assert any(k.endswith("source.png") for k in keys)
        assert any(k.endswith("thumbnail.jpg") for k in keys)
    finally:
        _cleanup(sync_session, patient_id=patient_id, owner_id=owner_id)


@skip_if_no_db
def test_slide_class_gross_override(sync_session: Session, tmp_path: Path) -> None:
    """--slide-class gross forces the class for a specimen photo."""
    img_path = tmp_path / "specimen.jpg"
    Image.new("RGB", (300, 200), (50, 80, 50)).save(img_path, format="JPEG")

    storage = _FakeS3Storage()
    owner_id, patient_id = _make_owner_and_patient(sync_session)
    try:
        result = import_pathology_slide(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="bvphoenix-raw",
            source=PathologyImportSource(
                path=img_path,
                owner_subject_id=owner_id,
                patient_id=patient_id,
                slide_class="gross",
            ),
        )
        sync_session.commit()
        slide = sync_session.execute(
            select(PathologySlide).where(PathologySlide.id == result.slide_id)
        ).scalar_one()
        assert slide.slide_class == "gross"
        assert slide.source_format == "jpeg"
    finally:
        _cleanup(sync_session, patient_id=patient_id, owner_id=owner_id)


@skip_if_no_db
def test_dzi_ready_complete_check_rejects_partial(sync_session: Session) -> None:
    """A dzi_ready row missing the descriptor key violates the DB CHECK —
    the serving endpoints can therefore trust dzi_ready as a single gate."""
    owner_id, patient_id = _make_owner_and_patient(sync_session)
    try:
        bad = PathologySlide(
            id=uuid.uuid4(),
            patient_id=patient_id,
            owner_subject_id=owner_id,
            slide_instance_uid=str(uuid.uuid4()),
            source_format="png",
            slide_class="micrograph",
            s3_bucket="bvphoenix-raw",
            s3_source_key="x/source.png",
            size_bytes=10,
            content_sha256="0" * 64,
            dzi_ready=True,  # but s3_dzi_key / dzi_levels / ... are NULL
        )
        sync_session.add(bad)
        with pytest.raises(IntegrityError):
            sync_session.flush()
        sync_session.rollback()
    finally:
        _cleanup(sync_session, patient_id=patient_id, owner_id=owner_id)
