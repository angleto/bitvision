"""DB-level enforcement of the folder ``patient_id`` inheritance invariant
(migration ``0037_folders_patient_inheritance``).

A folder nested under a parent MUST carry the parent's ``patient_id``. A
``BEFORE INSERT OR UPDATE`` trigger inherits it when NULL and rejects a
value that disagrees, so an *orphan* patient folder (``patient_id`` NULL
under a patient-scoped parent) — the bug behind the silently broken
fascicolo tree export (the contained study/document landed at the archive
root instead of under its folder) — is inexpressible regardless of the
write path: the tree API ``create_patient_folder``, the bulk-ingest
worker ``_ensure_subfolder``, or any future one. Service-layer hardening
in ``api/folders.py`` was bypassed by the newer tree endpoint, which is
exactly why the guarantee belongs in the database.

Runs against the dev/test Postgres with migrations through 0037 applied;
skipped when no DB URL is configured.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Folder, Patient
from bvphoenix.db.models.principals import Subject

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with migrations through 0037 applied",
)


@pytest_asyncio.fixture
async def owner_two_patients(db_session: AsyncSession):
    """An owner Subject + two patients (A, B), flushed but never committed.

    Everything is discarded by the ``db_session`` rollback at teardown,
    so the trigger is exercised inside one transaction with no cleanup
    bookkeeping. The trigger fires on ``flush`` (BEFORE INSERT), so a
    commit is unnecessary.
    """
    sid = uuid.uuid4()
    db_session.add(Subject(id=sid, kind="user", display_name=f"trig-{sid}"))
    await db_session.flush()
    pa = Patient(id=uuid.uuid4(), managed_by_subject_id=sid, display_name="Patient A")
    pb = Patient(id=uuid.uuid4(), managed_by_subject_id=sid, display_name="Patient B")
    db_session.add_all([pa, pb])
    await db_session.flush()
    return sid, pa, pb


async def _add_folder(
    db: AsyncSession,
    *,
    owner: uuid.UUID,
    name: str,
    parent_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
) -> Folder:
    """Insert a non-root folder and re-read it so the row reflects any
    trigger-side mutation of ``patient_id`` (the ORM object keeps the
    value it sent until refreshed)."""
    f = Folder(
        id=uuid.uuid4(),
        name=name,
        owner_subject_id=owner,
        parent_folder_id=parent_id,
        patient_id=patient_id,
    )
    db.add(f)
    await db.flush()
    await db.refresh(f)
    return f


@pytest.mark.asyncio
async def test_child_inherits_patient_id_when_null(db_session, owner_two_patients) -> None:
    """The orphan case: a child under a patient folder with NULL
    patient_id (what create_patient_folder / the worker produced) is
    healed to the parent's patient_id by the trigger."""
    sid, pa, _ = owner_two_patients
    parent = await _add_folder(db_session, owner=sid, name="2025", patient_id=pa.id)
    child = await _add_folder(
        db_session, owner=sid, name="2025-02-03 TC", parent_id=parent.id, patient_id=None
    )
    assert child.patient_id == pa.id


@pytest.mark.asyncio
async def test_deep_null_chain_inherits(db_session, owner_two_patients) -> None:
    """Inheritance composes top-down: a NULL grandchild under a
    (now-healed) NULL child still ends up patient-scoped."""
    sid, pa, _ = owner_two_patients
    root = await _add_folder(db_session, owner=sid, name="root", patient_id=pa.id)
    mid = await _add_folder(db_session, owner=sid, name="mid", parent_id=root.id)
    assert mid.patient_id == pa.id
    leaf = await _add_folder(db_session, owner=sid, name="leaf", parent_id=mid.id)
    assert leaf.patient_id == pa.id


@pytest.mark.asyncio
async def test_cross_patient_nesting_rejected(db_session, owner_two_patients) -> None:
    """A child that explicitly claims a different patient than its
    parent is rejected (cross-patient nesting is inexpressible)."""
    sid, pa, pb = owner_two_patients
    parent = await _add_folder(db_session, owner=sid, name="A", patient_id=pa.id)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Folder(
                    id=uuid.uuid4(),
                    name="rogue",
                    owner_subject_id=sid,
                    parent_folder_id=parent.id,
                    patient_id=pb.id,
                )
            )
            await db_session.flush()


@pytest.mark.asyncio
async def test_personal_workspace_nesting_stays_null(db_session, owner_two_patients) -> None:
    """Personal-workspace nesting (NULL under NULL) is untouched: the
    trigger neither inherits nor rejects."""
    sid, _, _ = owner_two_patients
    parent = await _add_folder(db_session, owner=sid, name="workspace", patient_id=None)
    child = await _add_folder(db_session, owner=sid, name="ws-sub", parent_id=parent.id)
    assert child.patient_id is None


@pytest.mark.asyncio
async def test_toplevel_patient_folder_requires_explicit_patient_id(
    db_session, owner_two_patients
) -> None:
    """The trigger can only inherit from a parent. A top-level patient
    folder (parent_folder_id=None) with patient_id=None stays NULL — the
    gap the ``create_patient_folder`` fix closes by setting the column
    explicitly. With it set, the row persists patient-scoped."""
    sid, pa, _ = owner_two_patients
    orphan = await _add_folder(db_session, owner=sid, name="top-null", patient_id=None)
    assert orphan.patient_id is None  # trigger cannot reach the no-parent case
    good = await _add_folder(db_session, owner=sid, name="top-set", patient_id=pa.id)
    assert good.patient_id == pa.id


@pytest.mark.asyncio
async def test_reparent_across_patients_rejected(db_session, owner_two_patients) -> None:
    """Re-parenting a patient-A folder under a patient-B folder without
    updating patient_id is rejected on UPDATE (the trigger covers
    UPDATE, not just INSERT)."""
    sid, pa, pb = owner_two_patients
    a_root = await _add_folder(db_session, owner=sid, name="A", patient_id=pa.id)
    b_root = await _add_folder(db_session, owner=sid, name="B", patient_id=pb.id)
    child = await _add_folder(db_session, owner=sid, name="c", parent_id=a_root.id)
    assert child.patient_id == pa.id
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            child.parent_folder_id = b_root.id
            await db_session.flush()
