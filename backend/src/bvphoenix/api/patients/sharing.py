# ruff: noqa: F405
# Auto-split from api/patients.py on 2026-05-21.
# Section: ``sharing``. Decorators register against the
# local ``router`` below; the package __init__.py
# aggregates every child via include_router so main.py's
# wiring stays a single line.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.patients import _shared  # for runtime access
from bvphoenix.api.patients._shared import *  # noqa: F403

router = APIRouter()


@router.post(
    "/patients/{patient_id}/share",
    response_model=ShareLinkOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_patient_share(
    request: Request,
    patient_id: uuid.UUID,
    body: ShareCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> ShareLinkOut:
    # Cross-patient guard for agent tokens: the bearer must hold this
    # patient in its scoped set, otherwise even a legitimately-owned
    # token cannot mint a share-link against a foreign patient.
    enforce_agent_patient_scope(request, patient_id)
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    if not (
        user.is_admin
        or patient.managed_by_subject_id == user.subject_id
        or patient.self_user_subject_id == user.subject_id
    ):
        raise HTTPException(status_code=403, detail="only the owner can share")

    valid_until = None
    if body.expires_in_hours is not None:
        valid_until = datetime.now(UTC) + timedelta(hours=body.expires_in_hours)

    perms = level_to_permissions(body.access_level, download=body.download)

    grantee_id = PUBLIC_SUBJECT_ID
    if body.target.kind == "email" and body.target.email:
        target_user = (
            await db.execute(select(User).where(User.email == body.target.email.lower()))
        ).scalar_one_or_none()
        if not target_user:
            raise HTTPException(status_code=404, detail=f"no user with email {body.target.email}")
        grantee_id = target_user.subject_id
    elif body.target.kind in ("org", "link_org") and body.target.org_subject_id:
        grantee_id = uuid.UUID(body.target.org_subject_id)

    grant = Grant(
        resource_kind="patient",
        resource_id=patient.id,
        grantor_subject_id=user.subject_id,
        grantee_subject_id=grantee_id,
        permissions=perms,
        conditions={"scope": body.target.kind},
        valid_until=valid_until,
        purpose=body.label or f"{body.access_level} access to Health Record",
    )
    db.add(grant)
    await db.flush()

    if body.target.kind in ("link_public", "link_org"):
        # Same hardening surface as /studies/{id}/share: validate
        # mutual exclusion + anonymous-mode preconditions BEFORE
        # touching the DB so a bad payload never leaves orphaned
        # rows.
        if body.autogen_password and body.password:
            raise HTTPException(
                status_code=400,
                detail="autogen_password and password are mutually exclusive",
            )
        if body.mode == "anonymous":
            if not body.recipient_name or not body.recipient_name.strip():
                raise HTTPException(
                    status_code=400,
                    detail="recipient_name is required for mode='anonymous'",
                )
            has_email = body.recipient_email and body.recipient_email.strip()
            has_phone = body.recipient_phone and body.recipient_phone.strip()
            if not (has_email or has_phone):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "at least one of recipient_email / recipient_phone "
                        "is required for mode='anonymous'"
                    ),
                )

        token = secrets.token_urlsafe(32)
        plaintext_password = body.password
        generated_password: str | None = None
        if body.autogen_password:
            generated_password = _autogen_share_password()
            plaintext_password = generated_password

        link = ShareLink(
            grant_id=grant.id,
            token=token,
            password_hash=hash_password(plaintext_password) if plaintext_password else None,
            label=body.label,
            max_uses=body.max_uses,
            mode=body.mode,
            recipient_name=(body.recipient_name or "").strip() or None,
            recipient_email=(body.recipient_email or "").strip().lower() or None,
            recipient_phone=(body.recipient_phone or "").strip() or None,
        )
        db.add(link)
        await db.commit()
        await db.refresh(link)
        await db.refresh(grant)
        return _link_out(link, grant, generated_password=generated_password)

    await db.commit()
    await db.refresh(grant)
    return ShareLinkOut(
        id=str(grant.id),
        token="",
        url="",
        label=body.label,
        permissions=list(grant.permissions),
        expires_at=grant.valid_until.isoformat() if grant.valid_until else None,
        revoked=False,
        use_count=0,
        max_uses=None,
        requires_password=False,
        created_at=grant.created_at.isoformat(),
    )
