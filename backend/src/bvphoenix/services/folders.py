"""Folder service helpers.

Currently provides ``get_or_create_root_folder``: idempotent creation
of the patient's materialised root folder (``folders.is_root = TRUE``).
The root folder is the FK-able representation of "the documents the
user has not filed under any specific folder", and the no-orphan DB
trigger relies on every live document having at least one folder
containment row pointing at *some* folder, of which the root is the
default fallback. See migration ``0088_patient_root_folder``.

The service is invoked from:
- the patient-creation flow, so each new patient starts with a root.
- ``ingest_document``, so a document uploaded without an explicit
  ``folder_id`` is attached to the patient's root in the same
  transaction.
- ``restore_document``, which reattaches a soft-deleted document to
  the root if its previous folder containment was lost.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models.folders import Folder
from bvphoenix.db.models.patients import Patient

# Sentinel name for the materialised root folder. The row exists only
# so the no-orphan trigger has a folder to point ``folder_items`` at —
# it is NEVER rendered as a folder card in the UI (the frontend
# substitutes the localised "Fascicolo" / "Health record" label as a
# synthetic breadcrumb segment instead). The leading underscores
# make the sentinel hard to mistake for a user-created folder if it
# leaks through a SQL query.
ROOT_FOLDER_NAME = "__root__"

# PLATFORM_OWNER subject seeded by migration 0036; used as fallback
# owner when a patient was created without a creator subject.
_PLATFORM_OWNER_SUBJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")


async def get_or_create_root_folder(
    db: AsyncSession,
    patient: Patient,
) -> Folder:
    """Return the patient's root folder, creating it if missing.

    Idempotent under the partial unique index
    ``uq_folders_root_per_patient``: a concurrent insert by another
    transaction is caught and translated to a fresh SELECT of the
    winning row.
    """
    existing = (
        await db.execute(
            select(Folder).where(Folder.patient_id == patient.id, Folder.is_root.is_(True))
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    owner_id = patient.managed_by_subject_id or _PLATFORM_OWNER_SUBJECT_ID
    stmt = (
        pg_insert(Folder.__table__)
        .values(
            id=uuid.uuid4(),
            name=ROOT_FOLDER_NAME,
            description=None,
            owner_subject_id=owner_id,
            parent_folder_id=None,
            patient_id=patient.id,
            is_root=True,
        )
        .on_conflict_do_nothing(index_elements=["patient_id"], index_where=text("is_root"))
        .returning(Folder.__table__.c.id)
    )
    try:
        row = (await db.execute(stmt)).one_or_none()
    except IntegrityError:
        row = None

    if row is None:
        # Either the ON CONFLICT skipped (concurrent winner) or the
        # IntegrityError fired; in both cases re-read to get the row
        # the other transaction won.
        return (
            await db.execute(
                select(Folder).where(Folder.patient_id == patient.id, Folder.is_root.is_(True))
            )
        ).scalar_one()

    return (await db.execute(select(Folder).where(Folder.id == row[0]))).scalar_one()
