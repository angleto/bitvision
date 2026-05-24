"""External patient identifiers — descriptive metadata, never a key.

The pre-v3 patient model treated ``tax_id`` (codice fiscale) as a
globally unique key and ``external_id`` (DICOM PatientID per manager)
as a per-manager unique. Both were business identifiers used as
technical keys with the well-known fragility (CF omocodi, MRN reuse
on RIS migrations, IssuerOfPatientID mistypes promoting distinct
real patients into the same row).

v3 enforces the FHIR-style separation: ``patients.id`` (UUID) is the
only key; every business identifier lives inside a JSONB array
``patients.external_identifiers`` shaped like FHIR ``Identifier``
(system / value / type / assigner). The ``cf_normalized`` GENERATED
column materialises the codice fiscale for indexed lookup without
forcing the application to keep a denormalised column in sync.

Endpoints (v3 phase 3a):
- ``GET /api/patients/{patient_id}/external-identifiers`` — list
- ``POST /api/patients/{patient_id}/external-identifiers`` — add (or update)
- ``DELETE /api/patients/{patient_id}/external-identifiers`` — remove by (system, value)
- ``GET /api/patients/lookup-external`` — cross-patient lookup, returns 0..N
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import enforce_agent_patient_scope, require_user
from bvphoenix.db.models import Patient, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.permissions import (
    READ_METADATA,
    WRITE_REPORT,
    can_patient,
    visible_patients_filter,
)
from bvphoenix.services.provenance_log import record_provenance

router = APIRouter(tags=["external-identifiers"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ExternalIdentifier(BaseModel):
    """FHIR ``Identifier`` minus the period / use / extension fields
    we do not yet need. ``type`` mirrors FHIR's ``identifier-use-codes``
    informally — common values: ``fiscal-code``, ``MR``
    (medical record number), ``PI`` (passport-style id),
    ``insurance``."""

    system: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=255)
    type: str = Field(..., min_length=1, max_length=64)
    assigner: str | None = Field(default=None, max_length=255)


class IdentifierLookupCandidate(BaseModel):
    """A patient that carries the queried (system, value) tuple in
    ``external_identifiers``. Lookup is intentionally non-deterministic:
    the UI always confirms with the human caregiver before binding."""

    patient_id: str
    display_name: str
    birth_date: str | None
    sex: str | None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/patients/{patient_id}/external-identifiers",
    response_model=list[ExternalIdentifier],
)
async def list_external_identifiers(
    patient_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> list[ExternalIdentifier]:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=READ_METADATA, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    raw: list[dict] = patient.external_identifiers or []
    out: list[ExternalIdentifier] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(ExternalIdentifier(**entry))
        except (TypeError, ValueError):
            # Defensive: a manually-inserted bad entry should not break
            # the whole list endpoint. Audit will surface it.
            continue
    return out


@router.post(
    "/patients/{patient_id}/external-identifiers",
    response_model=list[ExternalIdentifier],
    status_code=status.HTTP_201_CREATED,
)
async def add_external_identifier(
    patient_id: uuid.UUID,
    body: ExternalIdentifier,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> list[ExternalIdentifier]:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)

    existing: list[dict] = list(patient.external_identifiers or [])
    # Replace entry with same (system, value) if it exists; otherwise
    # append. Idempotent so an MCP retry produces the same final state.
    new_entry = body.model_dump()
    seen = False
    for i, entry in enumerate(existing):
        if (
            isinstance(entry, dict)
            and entry.get("system") == body.system
            and entry.get("value") == body.value
        ):
            existing[i] = new_entry
            seen = True
            break
    if not seen:
        existing.append(new_entry)
    patient.external_identifiers = existing

    record_provenance(
        db,
        target_kind="external_identifier",
        target_id=patient.id,
        activity="identify",
        user=user,
        request=request,
        diff={
            "action": "upsert",
            "system": body.system,
            "value": body.value,
            "type": body.type,
        },
    )
    await db.commit()
    await db.refresh(patient)
    return [
        ExternalIdentifier(**e) for e in (patient.external_identifiers or []) if isinstance(e, dict)
    ]


@router.delete(
    "/patients/{patient_id}/external-identifiers",
    response_model=list[ExternalIdentifier],
)
async def remove_external_identifier(
    patient_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    system: Annotated[str, Query()],
    value: Annotated[str, Query()],
) -> list[ExternalIdentifier]:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None or not await can_patient(
        db, user=user, action=WRITE_REPORT, patient=patient
    ):
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)
    existing: list[dict] = list(patient.external_identifiers or [])
    filtered = [
        e
        for e in existing
        if not (isinstance(e, dict) and e.get("system") == system and e.get("value") == value)
    ]
    if len(filtered) == len(existing):
        raise HTTPException(status_code=404, detail="identifier not found")
    patient.external_identifiers = filtered

    record_provenance(
        db,
        target_kind="external_identifier",
        target_id=patient.id,
        activity="identify",
        user=user,
        request=request,
        diff={"action": "remove", "system": system, "value": value},
    )
    await db.commit()
    await db.refresh(patient)
    return [
        ExternalIdentifier(**e) for e in (patient.external_identifiers or []) if isinstance(e, dict)
    ]


@router.get(
    "/patients/lookup-external",
    response_model=list[IdentifierLookupCandidate],
)
async def lookup_external_identifier(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    system: Annotated[str, Query(min_length=1, max_length=255)],
    value: Annotated[str, Query(min_length=1, max_length=255)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[IdentifierLookupCandidate]:
    """Cross-patient lookup. Intentionally non-deterministic: returns
    0, 1, or N candidates that carry ``(system, value)`` in their
    ``external_identifiers``. The caller (UI or MCP agent) confirms
    identity with the human; this endpoint never auto-binds.

    Restricted to patients the caller can already see — the lookup
    cannot enumerate identifiers across the global patient pool."""
    visible = await visible_patients_filter(db, user)
    # Match an entry where system AND value both equal the query.
    # The JSONB containment operator @> is index-friendly when a GIN
    # index exists on external_identifiers (added in a follow-up
    # migration; for now this is a sequential scan over the visible
    # subset, which is acceptable at typical fascicolo cardinality).
    needle = [{"system": system, "value": value}]
    stmt = (
        select(Patient.id, Patient.display_name, Patient.birth_date, Patient.sex)
        .where(
            Patient.id.in_(visible.select()),
            text("patients.external_identifiers @> :needle").bindparams(
                needle=func.cast(text(":needle_json"), text("jsonb"))
            ),
        )
        .limit(limit)
    )
    # The composed bindparams above are awkward; fall back to a raw
    # textual check for clarity. This is the supported async-friendly
    # form for JSONB containment with two parameter sites.
    stmt = (
        select(Patient.id, Patient.display_name, Patient.birth_date, Patient.sex)
        .where(Patient.id.in_(visible.select()))
        .where(text("external_identifiers @> CAST(:needle AS jsonb)"))
        .params(needle=__import__("json").dumps(needle))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        IdentifierLookupCandidate(
            patient_id=str(pid),
            display_name=name,
            birth_date=bd.isoformat() if bd else None,
            sex=sex,
        )
        for pid, name, bd, sex in rows
    ]


__all__ = ["ExternalIdentifier", "IdentifierLookupCandidate", "router"]
