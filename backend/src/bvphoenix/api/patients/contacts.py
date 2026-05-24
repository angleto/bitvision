# ruff: noqa: F405
# Auto-split from api/patients.py on 2026-05-21.
# Section: ``contacts``. Decorators register against the
# local ``router`` below; the package __init__.py
# aggregates every child via include_router so main.py's
# wiring stays a single line.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.patients import _shared  # for runtime access
from bvphoenix.api.patients._shared import *  # noqa: F403

router = APIRouter()


@router.get(
    "/patients/{patient_id}/contacts",
    response_model=list[PatientContact],
)
async def list_patient_contacts(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> list[PatientContact]:
    """Return every contact attached to a patient.

    Read-only, ``patient:read`` scope (or human visibility on the
    fascicolo) is enough. Primary contact appears first.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request)
    return await _load_patient_contacts(db, patient.id)


@router.post(
    "/patients/{patient_id}/contacts",
    response_model=PatientContact,
    status_code=status.HTTP_201_CREATED,
)
async def create_patient_contact(
    request: Request,
    patient_id: uuid.UUID,
    body: ContactCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> PatientContact:
    """Append a single contact. Atomic ergonomic wrapper around the
    replace-all semantics on ``update_patient.contacts``."""
    enforce_agent_scope(request, "patient:write")
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if DELETE not in perms and READ_METADATA not in perms:
        raise HTTPException(status_code=403, detail="cannot edit this patient")
    from bvphoenix.services import patient_contacts as svc

    row = await svc.create_contact(
        db,
        patient_id=patient.id,
        label=body.label,
        relationship=body.relationship,
        email=body.email,
        phone=body.phone,
        notes=body.notes,
        is_primary=body.is_primary,
        consent_to_contact=body.consent_to_contact,
    )
    await db.commit()
    await db.refresh(row)
    await audit.log(
        action="patient_contact_create",
        actor_subject_id=user.subject_id,
        resource_kind="patient_contact",
        resource_id=row.id,
        metadata={"patient_id": str(patient.id)},
    )
    return PatientContact(**svc.to_pydantic_dict(row))


@router.patch(
    "/patients/{patient_id}/contacts/{contact_id}",
    response_model=PatientContact,
)
async def patch_patient_contact(
    request: Request,
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: ContactUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> PatientContact:
    """Edit one contact in place. ``is_primary=True`` demotes any
    other primary on the same patient atomically."""
    enforce_agent_scope(request, "patient:write")
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if DELETE not in perms and READ_METADATA not in perms:
        raise HTTPException(status_code=403, detail="cannot edit this patient")
    from bvphoenix.services import patient_contacts as svc

    fields = body.model_dump(exclude_unset=True)
    row = await svc.update_contact(db, patient_id=patient.id, contact_id=contact_id, fields=fields)
    if row is None:
        raise HTTPException(status_code=404, detail="contact not found")
    await db.commit()
    await db.refresh(row)
    await audit.log(
        action="patient_contact_update",
        actor_subject_id=user.subject_id,
        resource_kind="patient_contact",
        resource_id=row.id,
        metadata={
            "patient_id": str(patient.id),
            "fields_updated": sorted(fields.keys()),
        },
    )
    return PatientContact(**svc.to_pydantic_dict(row))


@router.delete(
    "/patients/{patient_id}/contacts/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_patient_contact(
    request: Request,
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> Response:
    """Remove one contact. Refuses contacts with an active delegation;
    the operator must revoke the delegation explicitly first
    (``DELETE /api/patients/{id}/contacts/{cid}/delegate``)."""
    enforce_agent_scope(request, "patient:write")
    patient = await _get_patient_or_404(db, patient_id, user, request)
    perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    if DELETE not in perms and READ_METADATA not in perms:
        raise HTTPException(status_code=403, detail="cannot edit this patient")
    from bvphoenix.services import patient_contacts as svc

    deleted, reason = await svc.delete_contact(db, patient_id=patient.id, contact_id=contact_id)
    if not deleted:
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="contact not found")
        raise HTTPException(status_code=409, detail=reason)
    await db.commit()
    await audit.log(
        action="patient_contact_delete",
        actor_subject_id=user.subject_id,
        resource_kind="patient_contact",
        resource_id=contact_id,
        metadata={"patient_id": str(patient.id)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/patients/{patient_id}/contacts/{contact_id}/delegate",
    response_model=ContactDelegateOut,
    status_code=status.HTTP_201_CREATED,
)
async def delegate_contact(
    request: Request,
    patient_id: uuid.UUID,
    contact_id: str,
    body: ContactDelegateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> ContactDelegateOut:
    """Promote a ``Patient.contacts`` entry to a fascicolo delegate.

    The contact must already exist on the patient — operators add
    contacts via the regular ``PATCH /patients/{id}`` endpoint. Once
    promoted, the contact's row carries ``delegation_*`` pointers and
    the delegate can claim a real account via the share link.
    """
    from bvphoenix.services import patient_delegation as svc

    patient = await _get_patient_or_404(db, patient_id, user, request, action=DELETE)

    if body.password and body.autogen_password:
        raise HTTPException(
            status_code=400,
            detail="password and autogen_password are mutually exclusive",
        )

    try:
        result = await svc.promote_contact_to_delegate(
            db,
            patient=patient,
            contact_id=contact_id,
            user=user,
            access_level=body.access_level,
            expires_in_hours=body.expires_in_hours,
            autogen_password=body.autogen_password,
            explicit_password=body.password,
        )
    except svc.ContactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except svc.AlreadyDelegatedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except svc.InvalidLevelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base = str(request.base_url).rstrip("/")
    share_url = f"{base}/share/{result.share_link_token}"

    await audit.log(
        action="patient_contact_delegate",
        actor_subject_id=user.subject_id,
        resource_kind="patient",
        resource_id=patient.id,
        metadata={
            "contact_id": result.contact_id,
            "share_link_id": result.share_link_id,
            "access_level": result.delegation_level,
        },
    )

    return ContactDelegateOut(
        contact_id=result.contact_id,
        delegation_subject_id=result.subject_id,
        delegation_share_link_id=result.share_link_id,
        delegation_share_link_token=result.share_link_token,
        delegation_level=result.delegation_level,
        expires_at=result.expires_at.isoformat() if result.expires_at else None,
        generated_password=result.generated_password,
        share_url=share_url,
    )


@router.delete(
    "/patients/{patient_id}/contacts/{contact_id}/delegate",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_contact_delegate(
    request: Request,
    patient_id: uuid.UUID,
    contact_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> None:
    """Revoke an active delegation. The contact stays on the row as
    informational; only the ``delegation_*`` pointers + the underlying
    Grant disappear. The delegate's existing claimed account (if any)
    is untouched — they just lose access to this fascicolo.
    """
    from bvphoenix.services import patient_delegation as svc

    patient = await _get_patient_or_404(db, patient_id, user, request, action=DELETE)
    try:
        await svc.revoke_contact_delegation(db, patient=patient, contact_id=contact_id)
    except svc.ContactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await audit.log(
        action="patient_contact_delegate_revoke",
        actor_subject_id=user.subject_id,
        resource_kind="patient",
        resource_id=patient.id,
        metadata={"contact_id": contact_id},
    )
