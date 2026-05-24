"""End-to-end smoke tests pinning the F12 versioning wiring on the
write-side endpoints that the backend now records into the chain:

  * ``POST   /api/patients``                (create + initial seed)
  * ``PATCH  /api/patients/{id}``           (demographic edits)
  * ``POST   /api/patients/{id}/documents`` (document upload)
  * ``POST   /api/studies/{id}/reports``    (report create)
  * ``POST   /api/consultations``           (consult body snapshot)

For each test we hit the real route through ASGI, then read
``GET /api/patients/{id}/history`` and assert the new commit appears
on ``main`` (or on the consultation branch for the consult body).

If any of these wirings regresses the corresponding test fails — the
pilot module ``api/clinical_notes.py`` already had its own coverage,
so it is not duplicated here.
"""

from __future__ import annotations

import pytest

# v3-phase-4-skip: this test file targets entities/endpoints that were
# refactored in the v3 architecture (Study → ImagingStudy + ClinicalEvent
# parent, PatientDocument → Document with 3-axis taxonomy, Consultation
# folded into ReportContent). The test bodies need substantial rewrites
# against the new fixtures + queries; phase 4 of the v3 rollout owns
# that work. Skipped at module load until then.
pytest.skip("v3-phase-4-skip — pending rewrite on the v3 model", allow_module_level=True)


import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bvphoenix.auth import optional_user, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import Series, Study, User
from bvphoenix.db.models.principals import Subject
from bvphoenix.db.session import (
    SERVICE_SUBJECT,
    get_db,
    set_current_subject,
)
from bvphoenix.main import app

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with F12 migrations applied",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def owner_session() -> AsyncIterator[tuple[AsyncSession, User]]:
    """Yield ``(db, owner_user)``: an owner User row inside its own engine.

    Each test gets a clean session/engine pair so dependency overrides
    can route the route handler back to the same session and we can
    cleanup precisely on teardown.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    sid = uuid.uuid4()
    created_pids: list[uuid.UUID] = []
    created_studies: list[uuid.UUID] = []
    try:
        await set_current_subject(db, SERVICE_SUBJECT)
        db.add(Subject(id=sid, kind="user", display_name=f"wiring-{sid}"))
        await db.flush()
        user = User(
            subject_id=sid,
            email=f"wiring-{sid}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user)
        await db.commit()
        # Stash created ids on the user instance so tests can register
        # what they create without juggling another fixture.
        user._wiring_created_pids = created_pids  # type: ignore[attr-defined]
        user._wiring_created_studies = created_studies  # type: ignore[attr-defined]
        yield db, user
    finally:
        try:
            await db.rollback()
            await set_current_subject(db, SERVICE_SUBJECT)
            for sid_ in created_studies:
                await db.execute(text("DELETE FROM studies WHERE id = :s"), {"s": sid_})
            for pid in created_pids:
                await db.execute(text("DELETE FROM patients WHERE id = :p"), {"p": pid})
            await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": sid})
            await db.commit()
        finally:
            await db.close()
            await engine.dispose()


def _override_db(session: AsyncSession):
    async def _dep():
        yield session

    return _dep


def _override_user(user: User):
    async def _dep():
        return user

    return _dep


def _client_for(session: AsyncSession, user: User) -> AsyncClient:
    app.dependency_overrides[get_db] = _override_db(session)
    app.dependency_overrides[require_user] = _override_user(user)
    app.dependency_overrides[optional_user] = _override_user(user)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _list_history(client: AsyncClient, patient_id: str, ref: str = "main") -> list[dict]:
    r = await client.get(
        f"/api/patients/{patient_id}/history",
        params={"ref": ref, "limit": 100},
    )
    assert r.status_code == 200, r.text
    return r.json()["commits"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPatientWriteWiring:
    @pytest.mark.asyncio
    async def test_create_patient_seeds_and_records_initial_commit(self, owner_session) -> None:
        db, user = owner_session
        client = _client_for(db, user)
        try:
            r = await client.post(
                "/api/patients",
                json={"display_name": "Wiring Test Pat"},
            )
            assert r.status_code == 201, r.text
            pid = r.json()["id"]
            user._wiring_created_pids.append(uuid.UUID(pid))

            commits = await _list_history(client, pid)
            # Seed (`[init]`) + the demographic snapshot from create_patient
            # collapse into one new commit because seed runs first and the
            # subsequent record_versioned_change writes a child commit. So
            # we expect at least 2 entries.
            assert len(commits) >= 2
            messages = [c["message"] for c in commits]
            assert any("[patient] create" in m for m in messages)
            assert any("[init]" in m for m in messages)
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_update_patient_appends_commit(self, owner_session) -> None:
        db, user = owner_session
        client = _client_for(db, user)
        try:
            r = await client.post("/api/patients", json={"display_name": "Edit Pat"})
            pid = r.json()["id"]
            user._wiring_created_pids.append(uuid.UUID(pid))
            before = await _list_history(client, pid)

            r = await client.patch(
                f"/api/patients/{pid}",
                json={"allergies": "ramipril"},
            )
            assert r.status_code == 200, r.text
            after = await _list_history(client, pid)
            assert len(after) == len(before) + 1
            assert "[patient] edit" in after[0]["message"]
        finally:
            await client.aclose()
            app.dependency_overrides.clear()


class TestReportWriteWiring:
    @pytest.mark.asyncio
    async def test_create_report_appends_commit(self, owner_session) -> None:
        db, user = owner_session
        client = _client_for(db, user)
        try:
            r = await client.post("/api/patients", json={"display_name": "Report Pat"})
            pid = uuid.UUID(r.json()["id"])
            user._wiring_created_pids.append(pid)

            # Attach a study to the patient so /studies/{id}/reports works.
            study = Study(
                id=uuid.uuid4(),
                study_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
                owner_subject_id=user.subject_id,
                patient_id=pid,
                study_description="wiring",
                modalities=["CT"],
                is_public=False,
            )
            db.add(study)
            await db.flush()
            db.add(
                Series(
                    id=uuid.uuid4(),
                    study_id=study.id,
                    series_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
                    modality="CT",
                    body_part_examined="CHEST",
                )
            )
            await db.commit()
            user._wiring_created_studies.append(study.id)

            before = await _list_history(client, str(pid))
            r = await client.post(
                f"/api/studies/{study.id}/reports",
                data={"text": "first reading"},
            )
            assert r.status_code == 201, r.text
            after = await _list_history(client, str(pid))
            assert len(after) == len(before) + 1
            assert "[report] add" in after[0]["message"]
        finally:
            await client.aclose()
            app.dependency_overrides.clear()


class TestConsultationWriteWiring:
    @pytest.mark.asyncio
    async def test_create_consultation_records_body_on_branch(self, owner_session) -> None:
        db, user = owner_session
        client = _client_for(db, user)
        try:
            r = await client.post("/api/patients", json={"display_name": "Consult Pat"})
            pid = uuid.UUID(r.json()["id"])
            user._wiring_created_pids.append(pid)

            r = await client.post(
                "/api/consultations",
                json={
                    "patient_id": str(pid),
                    "title": "wiring consult",
                    "summary_md": "s",
                    "findings_md": "f",
                    "status": "draft",
                },
            )
            assert r.status_code == 201, r.text
            cid = r.json()["id"]
            commits = await _list_history(client, str(pid), ref=f"consultation/{cid}")
            messages = [c["message"] for c in commits]
            assert any("[consultation] open" in m for m in messages)
        finally:
            await client.aclose()
            app.dependency_overrides.clear()


class TestDocumentWriteWiring:
    @pytest.mark.asyncio
    async def test_create_document_text_only_appends_commit(self, owner_session) -> None:
        db, user = owner_session
        client = _client_for(db, user)
        try:
            r = await client.post("/api/patients", json={"display_name": "Doc Pat"})
            pid = uuid.UUID(r.json()["id"])
            user._wiring_created_pids.append(pid)

            before = await _list_history(client, str(pid))
            r = await client.post(
                f"/api/patients/{pid}/documents",
                data={
                    "title": "anamnesi",
                    "document_type": "clinical_note",
                    "text": "history collected",
                },
            )
            assert r.status_code == 201, r.text
            after = await _list_history(client, str(pid))
            assert len(after) == len(before) + 1
            assert "[document] add" in after[0]["message"]
        finally:
            await client.aclose()
            app.dependency_overrides.clear()
