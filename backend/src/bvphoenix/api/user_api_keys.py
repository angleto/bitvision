"""User API-key management (F7.1 BYOK).

Surface:

* ``GET    /api/settings/api-keys`` — list active keys (metadata only,
  never plaintext).
* ``PUT    /api/settings/api-keys/{provider}`` — create or rotate.
* ``DELETE /api/settings/api-keys/{provider}`` — revoke.

All endpoints require an authenticated user. The decrypted plaintext
never leaves :mod:`bvphoenix.services.byok`; the REST layer only ever
sees redacted tails for display purposes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_user
from bvphoenix.db.models import User
from bvphoenix.db.models.user_api_keys import USER_API_KEY_PROVIDERS
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.byok import (
    BYOKConfigError,
    list_user_api_keys,
    revoke_user_api_key,
    save_user_api_key,
)

router = APIRouter(prefix="/settings/api-keys", tags=["settings"])


class APIKeyIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=4096)


class APIKeyOut(BaseModel):
    provider: str
    granted_at: str
    last_used_at: str | None
    key_tail: str = Field(
        description=(
            "Last 4 characters of the plaintext key are echoed back on "
            "create so the user can recognise which key they just "
            "installed without the backend persisting the plaintext."
        ),
    )


class APIKeyListOut(BaseModel):
    provider: str
    granted_at: str
    last_used_at: str | None


@router.get("", response_model=list[APIKeyListOut])
async def list_keys(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[APIKeyListOut]:
    rows = await list_user_api_keys(db, user_subject_id=user.subject_id)
    return [
        APIKeyListOut(
            provider=r.provider,
            granted_at=r.granted_at.isoformat(),
            last_used_at=r.last_used_at.isoformat() if r.last_used_at else None,
        )
        for r in rows
    ]


@router.put("/{provider}", response_model=APIKeyOut)
async def save_key(
    provider: str,
    body: APIKeyIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> APIKeyOut:
    if provider not in USER_API_KEY_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown provider; must be one of {list(USER_API_KEY_PROVIDERS)}",
        )
    try:
        row = await save_user_api_key(
            db,
            user_subject_id=user.subject_id,
            provider=provider,
            api_key=body.api_key,
        )
    except BYOKConfigError as exc:
        # 503: the server *could* accept the key, but is misconfigured.
        # Makes the surface unambiguous for an operator looking at logs.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await db.commit()
    await audit.log(
        action="byok.key_rotated",
        actor_subject_id=user.subject_id,
        resource_kind="user_api_key",
        resource_id=row.id,
        metadata={"provider": provider},
    )
    return APIKeyOut(
        provider=provider,
        granted_at=row.granted_at.isoformat(),
        last_used_at=None,
        key_tail=body.api_key[-4:],
    )


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    provider: str,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> None:
    if provider not in USER_API_KEY_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown provider; must be one of {list(USER_API_KEY_PROVIDERS)}",
        )
    changed = await revoke_user_api_key(db, user_subject_id=user.subject_id, provider=provider)
    await db.commit()
    if changed:
        await audit.log(
            action="byok.key_revoked",
            actor_subject_id=user.subject_id,
            resource_kind="user_api_key",
            resource_id=None,
            metadata={"provider": provider},
        )
    return None
