"""App settings API.

Two surfaces:

* ``GET /api/settings/public`` — every setting with ``scope='public'``.
  Open to any authenticated user (and to anonymous when the global
  ``require_auth_globally`` toggle allows it). The frontend calls
  this on app boot and caches the result.

* ``GET /api/admin/settings`` and ``PATCH /api/admin/settings/{key}``
  — full read/write surface. Admin-only. Every PATCH is audited.

The model is intentionally generic (key/value/scope) so future
admin-tunable behaviour can land without a frontend round-trip:
ship a new key in the seed migration (or via PATCH) and consumers
read it. See ``db/models/app_settings.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_admin
from bvphoenix.db.models import AppSetting, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep

router = APIRouter(tags=["app-settings"])


class SettingOut(BaseModel):
    key: str
    value: Any
    scope: Literal["public", "admin"]
    description: str | None
    updated_at: str
    updated_by_subject_id: str | None


class SettingPatchIn(BaseModel):
    value: Any
    description: str | None = None
    scope: Literal["public", "admin"] | None = None


def _out(s: AppSetting) -> SettingOut:
    return SettingOut(
        key=s.key,
        value=s.value,
        scope=s.scope,  # type: ignore[arg-type]
        description=s.description,
        updated_at=s.updated_at.isoformat(),
        updated_by_subject_id=(str(s.updated_by_subject_id) if s.updated_by_subject_id else None),
    )


@router.get("/settings/public", response_model=list[SettingOut])
async def list_public_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User | None, Depends(optional_user)],
) -> list[SettingOut]:
    """Return every setting with scope='public'.

    Read-only. Any caller (including unauthenticated when the global
    auth gate allows it) can pull these — they drive client-side
    rendering only and must not contain secrets.
    """
    rows = (
        (
            await db.execute(
                select(AppSetting).where(AppSetting.scope == "public").order_by(AppSetting.key)
            )
        )
        .scalars()
        .all()
    )
    return [_out(s) for s in rows]


@router.get("/admin/settings", response_model=list[SettingOut])
async def list_all_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
) -> list[SettingOut]:
    rows = (await db.execute(select(AppSetting).order_by(AppSetting.key))).scalars().all()
    return [_out(s) for s in rows]


@router.patch("/admin/settings/{key}", response_model=SettingOut)
async def upsert_setting(
    key: str,
    body: SettingPatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    audit: AuditDep,
) -> SettingOut:
    if len(key) > 128 or not key:
        raise HTTPException(status_code=400, detail="invalid key")
    s = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    now = datetime.now(UTC)
    if s is None:
        s = AppSetting(
            key=key,
            value=body.value,
            scope=body.scope or "admin",
            description=body.description,
            updated_at=now,
            updated_by_subject_id=admin.subject_id,
        )
        db.add(s)
    else:
        s.value = body.value
        if body.scope is not None:
            s.scope = body.scope
        if body.description is not None:
            s.description = body.description
        s.updated_at = now
        s.updated_by_subject_id = admin.subject_id
    await db.commit()
    await db.refresh(s)
    await audit.log(
        action="setting_upsert",
        actor_subject_id=admin.subject_id,
        resource_kind="app_setting",
        resource_id=None,
        metadata={"key": key, "scope": s.scope},
    )
    return _out(s)
