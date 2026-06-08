"""Shared upload helpers used by BOTH the legacy ``/api/upload/bulk`` endpoint
and the resumable ``/api/upload/sessions`` endpoints.

Lifted out of ``api/bulk_upload.py`` so the durable-session path resolves the
upload target (owner subject / patient / folder) and enforces the SAME WRITE
permission gate as the legacy path — one implementation, no drift (the owner's
"modular, no duplication" rule). ``enforce_agent_patient_scope`` keeps agent
tokens inside their bound patient.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import enforce_agent_patient_scope
from bvphoenix.db.models import Folder, Patient, Subject, User
from bvphoenix.services.permissions import (
    DELETE,
    READ_METADATA,
    WRITE_REPORT,
    can_access_folder,
    can_patient,
    effective_permissions_on_patient,
)


async def resolve_owner_subject(db: AsyncSession, user: User) -> Subject:
    row = (
        await db.execute(select(Subject).where(Subject.id == user.subject_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=500, detail="owner subject missing for authenticated user")
    return row


async def resolve_patient(
    db: AsyncSession,
    user: User,
    patient_id: uuid.UUID | None,
    request: Request,
) -> Patient | None:
    if patient_id is None:
        return None
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)
    if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if WRITE_REPORT not in perms and DELETE not in perms:
        raise HTTPException(status_code=403, detail="cannot write to this patient")
    return patient


async def resolve_folder(
    db: AsyncSession, user: User, folder_id: uuid.UUID | None
) -> Folder | None:
    if folder_id is None:
        return None
    folder = (await db.execute(select(Folder).where(Folder.id == folder_id))).scalar_one_or_none()
    # Use the shared folder-access cascade (admin / folder owner / direct
    # folder grant / patient-level grant on a patient-scoped folder)
    # rather than an owner-only check. The old owner-only gate diverged
    # from ``can_access_folder`` and rejected a collaborator who could
    # already write to the patient (``resolve_patient`` enforces
    # WRITE_REPORT) the moment they targeted one of that patient's
    # folders — a 404 on a folder they legitimately reach everywhere else.
    if folder is None or not await can_access_folder(db, user=user, folder_id=folder.id):
        raise HTTPException(status_code=404, detail="folder not found")
    return folder
