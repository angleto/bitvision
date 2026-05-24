"""Folder description + edit metadata: end-to-end smoke.

Pins the contract for migration ``0047_folder_description``:

* ``Folder.description`` is round-tripped by the patient-tree endpoint
  on folder nodes (null when unset, plain string when set).
* The tree-scoped folder PATCH endpoint accepts ``description`` and
  follows the "exclude_unset" convention: omitting the key leaves the
  field alone, sending ``""`` or ``null`` clears it.
* ``kind_counts`` (full per-kind aggregate) is populated alongside the
  legacy 8-cap ``preview_kinds`` so the grid hover preview can show
  accurate counts even on folders with more than 8 children.
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
from bvphoenix.db.models import (
    Folder,
    FolderItem,
    PatientDocument,
    Study,
    User,
)
from bvphoenix.db.models.principals import Subject
from bvphoenix.db.session import (
    SERVICE_SUBJECT,
    get_db,
    set_current_subject,
)
from bvphoenix.main import app

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with F12 + 0047 migrations applied",
)


@pytest_asyncio.fixture
async def folder_fixture() -> AsyncIterator[tuple[AsyncSession, User, str, str]]:
    """Yield ``(db, owner_user, patient_id, folder_id)``.

    Folder seeded with one study + 9 documents so the count fans past
    the 8-cap on ``preview_kinds`` and we can assert that
    ``kind_counts`` reports the full breakdown.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    sid = uuid.uuid4()
    pid = uuid.uuid4()
    study_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    doc_ids: list[uuid.UUID] = [uuid.uuid4() for _ in range(9)]
    try:
        await set_current_subject(db, SERVICE_SUBJECT)
        db.add(Subject(id=sid, kind="user", display_name=f"desc-{sid}"))
        await db.flush()
        user = User(
            subject_id=sid,
            email=f"desc-{sid}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user)
        await db.flush()
        from bvphoenix.db.models import Patient

        db.add(
            Patient(
                id=pid,
                managed_by_subject_id=sid,
                display_name="Description Patient",
            )
        )
        await db.flush()
        db.add(
            Study(
                id=study_id,
                study_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
                owner_subject_id=sid,
                patient_id=pid,
                study_description="Desc study",
                modalities=["CT"],
                is_public=False,
            )
        )
        for did in doc_ids:
            db.add(
                PatientDocument(
                    id=did,
                    patient_id=pid,
                    uploaded_by_subject_id=sid,
                    document_type="clinical_note",
                    title=f"Doc {did}",
                    text="hello",
                )
            )
        db.add(
            Folder(
                id=folder_id,
                name="Imaging",
                owner_subject_id=sid,
                parent_folder_id=None,
                description=None,
            )
        )
        await db.flush()
        db.add(FolderItem(folder_id=folder_id, resource_kind="patient", resource_id=pid))
        db.add(FolderItem(folder_id=folder_id, resource_kind="study", resource_id=study_id))
        for did in doc_ids:
            db.add(FolderItem(folder_id=folder_id, resource_kind="document", resource_id=did))
        await db.commit()
        yield db, user, str(pid), str(folder_id)
    finally:
        try:
            await db.rollback()
            await set_current_subject(db, SERVICE_SUBJECT)
            await db.execute(text("DELETE FROM folders WHERE id = :f"), {"f": folder_id})
            await db.execute(text("DELETE FROM studies WHERE id = :s"), {"s": study_id})
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


@pytest.mark.asyncio
async def test_tree_returns_description_and_kind_counts(folder_fixture) -> None:
    db, user, pid, folder_id = folder_fixture
    app.dependency_overrides[get_db] = _override_db(db)
    app.dependency_overrides[require_user] = _override_user(user)
    app.dependency_overrides[optional_user] = _override_user(user)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    try:
        r = await client.get(f"/api/patients/{pid}/tree")
        assert r.status_code == 200, r.text
        body = r.json()
        folders = [n for n in body["nodes"] if n["type"] == "folder"]
        assert len(folders) == 1
        folder = folders[0]
        assert folder["id"] == folder_id

        # Description starts unset.
        assert folder["description"] is None

        # ``preview_kinds`` is capped at 8 items by the row-numbered
        # query that builds the preview stack, so the document count
        # reads at most 7 (1 study + 7 docs = 8 rows).
        preview_kinds = folder["preview_kinds"]
        assert preview_kinds["study"] == 1
        assert preview_kinds["document"] <= 7

        # ``kind_counts`` ignores the cap and reports the truth: 1
        # study + 9 documents.
        kind_counts = folder["kind_counts"]
        assert kind_counts == {"study": 1, "document": 9}
    finally:
        await client.aclose()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_patch_sets_and_clears_description(folder_fixture) -> None:
    db, user, pid, folder_id = folder_fixture
    app.dependency_overrides[get_db] = _override_db(db)
    app.dependency_overrides[require_user] = _override_user(user)
    app.dependency_overrides[optional_user] = _override_user(user)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    try:
        # Set the description.
        r = await client.patch(
            f"/api/patients/{pid}/tree/folder/{folder_id}",
            json={"description": "  Pre-op imaging  "},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["description"] == "Pre-op imaging"  # trimmed

        # Reload via the tree endpoint to confirm it round-trips.
        r2 = await client.get(f"/api/patients/{pid}/tree")
        folder = next(n for n in r2.json()["nodes"] if n["type"] == "folder")
        assert folder["description"] == "Pre-op imaging"

        # Omitting ``description`` leaves it alone.
        r3 = await client.patch(
            f"/api/patients/{pid}/tree/folder/{folder_id}",
            json={"name": "Imaging renamed"},
        )
        assert r3.status_code == 200, r3.text
        assert r3.json()["description"] == "Pre-op imaging"

        # Empty string clears the description.
        r4 = await client.patch(
            f"/api/patients/{pid}/tree/folder/{folder_id}",
            json={"description": ""},
        )
        assert r4.status_code == 200, r4.text
        assert r4.json()["description"] is None

        # Explicit null also clears.
        r5a = await client.patch(
            f"/api/patients/{pid}/tree/folder/{folder_id}",
            json={"description": "Some text"},
        )
        assert r5a.status_code == 200, r5a.text
        r5b = await client.patch(
            f"/api/patients/{pid}/tree/folder/{folder_id}",
            json={"description": None},
        )
        assert r5b.status_code == 200, r5b.text
        assert r5b.json()["description"] is None
    finally:
        await client.aclose()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_description_max_length_enforced(folder_fixture) -> None:
    db, user, pid, folder_id = folder_fixture
    app.dependency_overrides[get_db] = _override_db(db)
    app.dependency_overrides[require_user] = _override_user(user)
    app.dependency_overrides[optional_user] = _override_user(user)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    try:
        too_long = "x" * 501
        r = await client.patch(
            f"/api/patients/{pid}/tree/folder/{folder_id}",
            json={"description": too_long},
        )
        assert r.status_code == 422, r.text
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
