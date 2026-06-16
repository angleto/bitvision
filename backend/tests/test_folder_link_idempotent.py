"""Regression: filing a study/document into a folder must be idempotent.

A DICOM exam re-uploaded — or split across several manifest groups, each
committed by its own ``process_bulk_ingest`` call (the worker iterates
``grouped.items()``) — re-touches the SAME deterministic study UUID
(derived from its StudyInstanceUID). The study re-appears in
``IngestSummary.studies_created`` although it is already filed, and the
old unconditional ``db.add(FolderItem(...))`` aborted the WHOLE ingest
transaction with::

    asyncpg.exceptions.UniqueViolationError: duplicate key value violates
    unique constraint "folder_items_pkey"
    Key (folder_id, resource_kind, resource_id)=(..., study, ...) already exists

so a multi-group exam failed on the very first upload (and every retry),
leaving only the first group's series persisted.

``services.folders.link_resource_to_folder`` makes the link a no-op in
that case (``INSERT ... ON CONFLICT DO NOTHING`` on ``folder_items_pkey``).
These tests pin the contract end to end: the service helper and the
``add_item_to_folder`` API endpoint that shares it.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import Folder, FolderItem, Patient, User
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from bvphoenix.services.folders import get_or_create_root_folder, link_resource_to_folder
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


async def _folder_item_count(
    db: AsyncSession, folder_id: uuid.UUID, resource_kind: str, resource_id: uuid.UUID
) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(FolderItem)
            .where(
                FolderItem.folder_id == folder_id,
                FolderItem.resource_kind == resource_kind,
                FolderItem.resource_id == resource_id,
            )
        )
    ).scalar_one()


def _client_as(session: AsyncSession, user: User | None) -> AsyncClient:
    async def _db():
        yield session

    async def _usr():
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _usr
    app.dependency_overrides[optional_user] = _usr
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_link_resource_to_folder_is_idempotent(db_session: AsyncSession, make_user) -> None:
    """Calling the helper twice for the same (folder, kind, resource)
    inserts exactly one row: first call reports ``True`` (inserted),
    second reports ``False`` (already present), and crucially the second
    call does NOT raise a UniqueViolationError / poison the session."""
    owner = await make_user()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="Ferlita Francesca",
    )
    db_session.add(patient)
    await db_session.flush()
    root = await get_or_create_root_folder(db_session, patient)
    study_id = uuid.uuid4()

    first = await link_resource_to_folder(
        db_session, folder_id=root.id, resource_kind="study", resource_id=study_id
    )
    second = await link_resource_to_folder(
        db_session, folder_id=root.id, resource_kind="study", resource_id=study_id
    )
    await db_session.commit()

    assert first is True
    assert second is False
    assert await _folder_item_count(db_session, root.id, "study", study_id) == 1


async def test_reingest_after_committed_link_is_noop(db_session: AsyncSession, make_user) -> None:
    """Reproduces the worker's multi-group scenario: 'group 1' commits a
    FolderItem for the study; 'group 2' re-touches the same study and
    re-links it. With the old ``db.add`` this raised on commit; the
    helper makes it a no-op so group 2's transaction survives."""
    owner = await make_user()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="Ferlita Francesca",
    )
    db_session.add(patient)
    await db_session.flush()
    root = await get_or_create_root_folder(db_session, patient)
    study_id = uuid.uuid4()

    # ---- group 1: study filed and committed ----
    db_session.add(FolderItem(folder_id=root.id, resource_kind="study", resource_id=study_id))
    await db_session.commit()

    # ---- group 2: same study re-touched -> re-link must be a no-op ----
    inserted = await link_resource_to_folder(
        db_session, folder_id=root.id, resource_kind="study", resource_id=study_id
    )
    # The transaction must still be usable afterwards (no poisoned session).
    await db_session.commit()

    assert inserted is False
    assert await _folder_item_count(db_session, root.id, "study", study_id) == 1


async def test_add_item_to_folder_endpoint_idempotent(db_session: AsyncSession, make_user) -> None:
    """The ``POST /folders/{id}/items`` endpoint must not 500 on a repeat
    add (double-click / retry); it returns ``already_present`` instead."""
    owner = await make_user()
    folder = Folder(
        id=uuid.uuid4(),
        name="Esami TAC",
        owner_subject_id=owner.subject_id,
        patient_id=None,
    )
    db_session.add(folder)
    await db_session.commit()
    study_id = uuid.uuid4()
    body = {"resource_kind": "study", "resource_id": str(study_id)}

    try:
        async with _client_as(db_session, owner) as client:
            r1 = await client.post(f"/api/folders/{folder.id}/items", json=body)
            r2 = await client.post(f"/api/folders/{folder.id}/items", json=body)
    finally:
        app.dependency_overrides.clear()

    assert r1.status_code == 201, r1.text
    assert r1.json()["status"] == "added"
    assert r2.status_code == 201, r2.text
    assert r2.json()["status"] == "already_present"
    assert await _folder_item_count(db_session, folder.id, "study", study_id) == 1
