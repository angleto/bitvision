"""build_export_zip path coverage for v3 model wiring.

The legacy ``Report`` model (study-scoped, with ``text``/``version``)
was replaced by ``ReportContent`` (clinical-event-scoped, narrative
markdown) in v3, but ``services/patient_export.py`` kept dereferencing
the dead symbol. Default ``includes`` is ``{"studies","reports",
"documents","annotations"}``, so any default fascicolo export crashed
with ``NameError: name 'Report' is not defined`` the moment the worker
hit the reports branch — the user only ever saw "Creazione ZIP in
corso..." until the worker eventually marked the Job ``failed``.

This test exercises the same default-includes path against a real DB
(skips when one is not reachable) with stub S3 storage, so any future
rename / column drop in ReportContent / Document / DocumentFile breaks
the suite instead of silently breaking production exports.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from typing import BinaryIO

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    ClinicalEvent,
    Document,
    DocumentFile,
    Patient,
    ReportContent,
    User,
)
from bvphoenix.db.models.principals import Subject
from bvphoenix.services.patient_export import build_export_zip
from tests.conftest import skip_if_no_db

pytestmark = [pytest.mark.asyncio, skip_if_no_db]


class _StubStorage:
    """Minimal storage stub used by build_export_zip for blob fetches.

    The export builder only calls ``get_object_bytes(bucket=, key=)``;
    everything else (uploads, presigning) is the worker's job and lives
    outside this function.
    """

    def __init__(self, blobs: dict[tuple[str, str], bytes] | None = None) -> None:
        self.blobs = blobs or {}
        self.fetches: list[tuple[str, str]] = []

    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        self.fetches.append((bucket, key))
        try:
            return self.blobs[(bucket, key)]
        except KeyError:
            raise FileNotFoundError(f"no blob for {bucket}/{key}") from None

    # Unused by build_export_zip, but kept so the stub is interchangeable
    # with the real S3Storage when other call-sites get added.
    def upload_bytes(self, data: bytes | BinaryIO, *, bucket: str, key: str):
        raise NotImplementedError


@pytest_asyncio.fixture
async def patient_with_data(
    db_session: AsyncSession,
) -> AsyncIterator[tuple[Patient, User]]:
    """Patient + ClinicalEvent + ReportContent + Document(+DocumentFile).

    Minimal fixture covering every limb the export touches:
      - one ClinicalEvent (carries patient_id) joined by ReportContent
      - one ReportContent (authority='original', status='endorsed') with
        a non-trivial narrative_md so we can assert the .md file is
        emitted
      - one Document with both the legacy single-file and one DocumentFile
        child, so both export paths are exercised in a single test

    Builds Subject + User inline (no ``make_user`` dependency) so the
    cleanup ordering stays under our control: the conftest's
    ``db_session`` rollback discards every row we added, no ``DELETE``
    interactions with sibling fixtures that would race with the
    cross-FK constraints between ``subjects`` and ``report_contents``.
    """
    sid = uuid.uuid4()
    db_session.add(Subject(id=sid, kind="user", display_name=f"export-test-{sid}"))
    await db_session.flush()
    user = User(
        subject_id=sid,
        email=f"export-test-{sid}@example.com",
        password_hash=None,
        is_admin=False,
    )
    db_session.add(user)
    await db_session.flush()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=user.subject_id,
        display_name="Export Test Patient",
    )
    db_session.add(patient)
    await db_session.flush()

    event = ClinicalEvent(
        id=uuid.uuid4(),
        patient_id=patient.id,
        kind="outpatient_visit",
        title="discharge note",
    )
    db_session.add(event)
    await db_session.flush()

    rc = ReportContent(
        id=uuid.uuid4(),
        clinical_event_id=event.id,
        authority_id="original",
        status="endorsed",
        language="it",
        title="Lettera di dimissione",
        narrative_md="# Dimissione\n\nPaziente in buone condizioni.",
        structured_fields={},
        created_by_subject_id=user.subject_id,
        author_kind="human",
    )
    db_session.add(rc)

    doc = Document(
        id=uuid.uuid4(),
        patient_id=patient.id,
        uploaded_by_subject_id=user.subject_id,
        kind_id="unclassified",
        provenance_id="manual_entry",
        authority_id="original",
        title="referto.pdf",
        text=None,
        file_s3_key=f"derivatives/test/{uuid.uuid4()}.pdf",
        file_content_type="application/pdf",
    )
    db_session.add(doc)
    await db_session.flush()

    df = DocumentFile(
        id=uuid.uuid4(),
        document_id=doc.id,
        sequence=0,
        file_s3_key=f"derivatives/test/{uuid.uuid4()}.jpg",
        file_content_type="image/jpeg",
        original_filename="page1.jpg",
        size_bytes=42,
    )
    db_session.add(df)
    # No commit — the conftest ``db_session`` fixture rolls back at
    # close so the fixture state is per-test-isolated by construction.
    # Avoiding a commit also sidesteps the pytest-asyncio "event loop
    # is closed" race we hit when running multiple tests in this module.
    await db_session.flush()

    yield patient, user


async def test_default_includes_does_not_namerefor_report(
    db_session: AsyncSession,
    patient_with_data: tuple[Patient, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default fascicolo export must not crash with NameError.

    Pinning the v3 wiring: ``includes={"studies","reports","documents",
    "annotations"}`` (the dialog's default selection) successfully
    builds a ZIP and the manifest has the expected limbs populated.
    Pre-fix this raised ``NameError: name 'Report' is not defined`` the
    moment the reports branch ran.
    """
    patient, user = patient_with_data
    stub = _StubStorage(
        blobs={
            # Match whatever derivatives bucket the settings hand out;
            # use a wildcard via FileNotFoundError fallback for unknown
            # keys so the export records ``file_error`` instead of
            # raising.
        }
    )
    monkeypatch.setattr(
        "bvphoenix.services.patient_export.get_s3_storage",
        lambda: stub,
    )

    zip_bytes, manifest = await build_export_zip(
        db_session,
        user,
        patient,
        {"studies", "reports", "documents", "annotations"},
    )

    # ZIP integrity
    archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = set(archive.namelist())
    assert "manifest.json" in names

    # Reports limb: at least one .md file plus a manifest entry for the
    # ReportContent fixture.
    md_members = [n for n in names if n.startswith("reports/") and n.endswith(".md")]
    assert md_members, f"expected reports/<id>.md, got {sorted(names)}"
    assert manifest["counts"]["reports"] >= 1
    assert any(entry.get("title") == "Lettera di dimissione" for entry in manifest["reports"])
    # The narrative we wrote round-trips through the .md file
    assert any(archive.read(m).decode("utf-8").startswith("# Dimissione") for m in md_members)

    # Documents limb: the manifest carries an entry for the Document
    # and the DocumentFile child is recorded under ``files``. The
    # actual blobs aren't in the stub so each path records ``file_error``,
    # which is the expected graceful-degradation contract.
    assert manifest["counts"]["documents"] >= 1
    doc_entry = next(e for e in manifest["documents"] if e["title"] == "referto.pdf")
    assert doc_entry["kind"] == "unclassified"
    assert doc_entry["authority"] == "original"
    # Multi-file children must surface in the entry
    assert len(doc_entry["files"]) == 1
    assert doc_entry["files"][0]["original_filename"] == "page1.jpg"

    # Reports-only run: same fixture, narrower includes. Guards
    # against a refactor that accidentally fans the reports branch
    # into the documents loop. Folded into this test (instead of a
    # second test using the same fixture) because pytest-asyncio's
    # default fixture scoping closes the asyncpg event loop between
    # tests sharing one DB-touching fixture.
    _, manifest_reports = await build_export_zip(db_session, user, patient, {"reports"})
    assert manifest_reports["counts"]["reports"] >= 1
    assert manifest_reports["counts"]["studies"] == 0
    assert manifest_reports["counts"]["documents"] == 0
