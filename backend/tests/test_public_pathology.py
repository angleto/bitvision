"""Tests for the OpenData public-pathology importer.

Exercises ``import_public_pathology_slide`` end-to-end with a fake S3
storage and a fake OpenSlide, so the test is hermetic (no real gigapixel
WSI, no ``tifffile`` / ``libtiff`` fixture). The fake reproduces exactly
the OpenSlide surface the importer touches: ``properties``,
``dimensions``, ``level_count``, ``associated_images``, ``get_thumbnail``,
``close``.

DB-touching tests require a live Postgres (same dev DB as the rest of the
suite) and are skipped otherwise. Pure tests (adapter listing, licensing)
run unconditionally.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import ClinicalEvent, PathologySlide, Patient
from bvphoenix.services import pathology_import
from bvphoenix.services.licensing import license_allows_commercial_use
from bvphoenix.services.permissions import platform_owner_subject_id
from bvphoenix.services.public_pathology import (
    PublicPathologySource,
    completed_slide_keys_for_source,
    import_public_pathology_slide,
)
from tests.conftest import skip_if_no_db

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class _FakeUploadResult:
    bucket: str
    key: str
    size_bytes: int


class _FakeS3Storage:
    """Records uploads without touching network / disk."""

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


class _FakeOpenSlide:
    """Minimal OpenSlide stand-in covering only what the importer reads."""

    def __init__(self, _path: str) -> None:
        self.properties = {
            "openslide.objective-power": "40",
            "openslide.mpp-x": "0.25",
            "openslide.mpp-y": "0.25",
            "openslide.vendor": "fake",
        }
        self.dimensions = (1024, 768)
        self.level_count = 3
        self.associated_images: dict[str, Image.Image] = {}

    def get_thumbnail(self, _size) -> Image.Image:
        return Image.new("RGB", (64, 48), (128, 64, 64))

    def close(self) -> None:
        return None


@pytest.fixture
def fake_openslide(monkeypatch) -> None:
    monkeypatch.setattr(pathology_import.openslide, "OpenSlide", _FakeOpenSlide)


@pytest.fixture
def sync_session() -> Session:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    return Session(engine)


def _write_fake_slide(path: Path, *, payload: bytes = b"FAKE-WSI-BYTES") -> None:
    path.write_bytes(payload)


def _cleanup_collection(session: Session, collection: str) -> None:
    slides = (
        session.execute(
            select(PathologySlide).where(PathologySlide.source_collection == collection)
        )
        .scalars()
        .all()
    )
    patient_ids = {s.patient_id for s in slides if s.patient_id is not None}
    session.execute(delete(PathologySlide).where(PathologySlide.source_collection == collection))
    if patient_ids:
        session.execute(delete(ClinicalEvent).where(ClinicalEvent.patient_id.in_(patient_ids)))
        session.execute(delete(Patient).where(Patient.id.in_(patient_ids)))
    session.commit()


# --------------------------------------------------------------------------
# Pure tests (no DB)
# --------------------------------------------------------------------------


def test_license_allows_commercial_use() -> None:
    assert license_allows_commercial_use("CC-BY-4.0") is True
    assert license_allows_commercial_use("CC-BY-3.0") is True
    assert license_allows_commercial_use("CC0-1.0") is True
    assert license_allows_commercial_use("CC-BY-NC-4.0") is False
    assert license_allows_commercial_use("CC-BY-NC-SA-4.0") is False
    assert license_allows_commercial_use(None) is True
    assert license_allows_commercial_use("") is True


def test_http_adapter_lists_and_validates_suffix() -> None:
    from bvphoenix.cli.public_import_pathology import (
        ManifestPathologySource,
        _list_items_http,
    )

    src = ManifestPathologySource(
        collection="OpenSlide/test-data",
        adapter="http",
        license_spdx="CC0-1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        citation_text="x",
        citation_required=False,
        stain="H&E",
        slides=[
            {"subject_id": "CMU-1", "url": "https://example.org/a/CMU-1.svs"},
        ],
    )
    items = _list_items_http(src)
    assert len(items) == 1
    assert items[0].subject_id == "CMU-1"
    assert items[0].upstream_file_id == "CMU-1.svs"
    assert items[0].ext == ".svs"
    assert items[0].stain == "H&E"

    bad = ManifestPathologySource(
        collection="X",
        adapter="http",
        license_spdx="CC0-1.0",
        license_url="u",
        citation_text="x",
        citation_required=False,
        slides=[{"subject_id": "S", "url": "https://example.org/S.mrxs"}],
    )
    import click

    with pytest.raises(click.ClickException):
        _list_items_http(bad)


def test_gdc_adapter_is_deferred() -> None:
    import click

    from bvphoenix.cli.public_import_pathology import ManifestPathologySource, _list_items

    src = ManifestPathologySource(
        collection="GDC/TCGA-BRCA",
        adapter="gdc",
        license_spdx="UNLICENSED",
        license_url="u",
        citation_text="x",
        citation_required=True,
    )
    with pytest.raises(click.ClickException, match="deferred"):
        _list_items(src)


# --------------------------------------------------------------------------
# DB tests
# --------------------------------------------------------------------------


@skip_if_no_db
def test_import_creates_platform_owned_public_slide(
    tmp_path: Path, sync_session: Session, fake_openslide
) -> None:
    collection = f"TEST/PATH-{uuid.uuid4().hex[:8]}"
    subject_id = "SLIDE-001"
    slide_file = tmp_path / "slide.svs"
    _write_fake_slide(slide_file)

    storage = _FakeS3Storage()
    source = PublicPathologySource(
        collection=collection,
        subject_id=subject_id,
        license_spdx="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        citation_text="Test citation.",
        citation_required=True,
        stain="H&E",
        upstream_file_id="slide.svs",
    )
    try:
        result = import_public_pathology_slide(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="test-bucket",
            path=slide_file,
            source=source,
        )
        sync_session.commit()

        patient = sync_session.execute(
            select(Patient).where(Patient.id == result.patient_id)
        ).scalar_one()
        assert patient.managed_by_subject_id == platform_owner_subject_id()
        assert patient.external_identifiers == [
            {
                "system": f"urn:opendata:pathology:{collection}",
                "type": "opendata-subject",
                "value": subject_id,
            }
        ]
        assert result.patient_created is True

        slide = sync_session.execute(
            select(PathologySlide).where(PathologySlide.id == result.slide_result.slide_id)
        ).scalar_one()
        assert slide.is_public is True
        assert slide.contribution_tier == "t4"
        assert slide.source_collection == collection
        assert slide.source_subject_id == subject_id
        assert slide.license_spdx == "CC-BY-4.0"
        assert slide.slide_label == "slide.svs"  # upstream file id
        assert slide.owner_subject_id == platform_owner_subject_id()
        assert slide.label_redacted is True
        assert slide.s3_label_key is None
        assert result.slide_result.created is True
        # source + thumbnail uploaded (no macro on the fake).
        assert len(storage.uploads) == 2
    finally:
        _cleanup_collection(sync_session, collection)
        sync_session.close()


@skip_if_no_db
def test_reimport_same_slide_is_idempotent(
    tmp_path: Path, sync_session: Session, fake_openslide
) -> None:
    collection = f"TEST/PATH-{uuid.uuid4().hex[:8]}"
    subject_id = "SLIDE-IDEM"
    slide_file = tmp_path / "slide.svs"
    _write_fake_slide(slide_file)

    storage = _FakeS3Storage()
    source = PublicPathologySource(
        collection=collection,
        subject_id=subject_id,
        license_spdx="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        citation_text="Test citation.",
        upstream_file_id="slide.svs",
    )
    try:
        first = import_public_pathology_slide(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="test-bucket",
            path=slide_file,
            source=source,
        )
        sync_session.commit()
        assert first.patient_created is True
        assert first.slide_result.created is True

        second = import_public_pathology_slide(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="test-bucket",
            path=slide_file,
            source=source,
        )
        sync_session.commit()
        assert second.patient_created is False
        assert second.patient_id == first.patient_id
        assert second.slide_result.created is False
        assert second.slide_result.slide_id == first.slide_result.slide_id
        assert second.slide_result.bytes_uploaded == 0
    finally:
        _cleanup_collection(sync_session, collection)
        sync_session.close()


@skip_if_no_db
def test_completed_slide_keys_reports_ingested(
    tmp_path: Path, sync_session: Session, fake_openslide
) -> None:
    collection = f"TEST/PATH-{uuid.uuid4().hex[:8]}"
    subject_id = "SLIDE-SKIP"
    slide_file = tmp_path / "slide.svs"
    _write_fake_slide(slide_file)

    storage = _FakeS3Storage()
    source = PublicPathologySource(
        collection=collection,
        subject_id=subject_id,
        license_spdx="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        citation_text="x",
        upstream_file_id="the-file-id.svs",
    )
    try:
        result = import_public_pathology_slide(
            session=sync_session,
            storage=storage,  # type: ignore[arg-type]
            bucket="test-bucket",
            path=slide_file,
            source=source,
        )
        sync_session.commit()

        done = completed_slide_keys_for_source(
            sync_session, collection=collection, subject_id=subject_id
        )
        # Both the upstream file id (slide_label) and the slide UID report done.
        assert "the-file-id.svs" in done
        slide = sync_session.execute(
            select(PathologySlide).where(PathologySlide.id == result.slide_result.slide_id)
        ).scalar_one()
        assert slide.slide_instance_uid in done

        assert (
            completed_slide_keys_for_source(
                sync_session, collection=collection, subject_id="SLIDE-DOES-NOT-EXIST"
            )
            == set()
        )
    finally:
        _cleanup_collection(sync_session, collection)
        sync_session.close()
