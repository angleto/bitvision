"""Admin LLM prompt overrides.

Surface for editing the Q&A system prompts (one per locale) from a
dedicated admin UI. The storage is the generic ``app_settings`` table
under keys ``qna.system_prompt.it`` and ``qna.system_prompt.en``;
those keys are what ``services.qna_prompts.build_system_prompt`` reads
at request time. The frozen-in-code defaults in
``services.qna_prompts.DEFAULT_PROMPTS`` are the source of truth for
"factory reset" and the diff view.

Three operations:

* ``GET /api/admin/llm-prompts`` — return a bundle for every supported
  locale with both the current effective text and the frozen default,
  so the UI can render a diff side-by-side and offer a one-click
  restore.
* ``PUT /api/admin/llm-prompts/{locale}`` — upsert the override.
* ``DELETE /api/admin/llm-prompts/{locale}`` — drop the override so
  the frozen default is used again.

All endpoints are admin-only and audited. The list of supported
locales is derived from ``DEFAULT_PROMPTS`` so adding a new locale
upstream automatically exposes a row here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_admin
from bvphoenix.db.models import AppSetting, User
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.qna_prompts import DEFAULT_PROMPTS

router = APIRouter(tags=["admin-llm-prompts"], prefix="/admin/llm-prompts")

_KEY_PREFIX = "qna.system_prompt."


class LlmPromptOut(BaseModel):
    locale: str
    default_text: str
    current_text: str
    is_override: bool
    updated_at: str | None
    updated_by_subject_id: str | None


class LlmPromptUpdateIn(BaseModel):
    value: str = Field(min_length=1, max_length=32_000)


def _key_for(locale: str) -> str:
    return f"{_KEY_PREFIX}{locale}"


def _extract_text(value: object) -> str | None:
    """Return the stored prompt text or ``None`` if the row exists but
    holds an empty/unsupported shape.

    The JSONB column accepts strings directly (``"..."``) and also a
    ``{"value": "..."}`` wrapper that some legacy admin UIs emit; we
    tolerate both, same as ``build_system_prompt``.
    """
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, dict):
        inner = value.get("value")
        if isinstance(inner, str) and inner.strip():
            return inner
    return None


def _row_to_out(locale: str, row: AppSetting | None) -> LlmPromptOut:
    default_text = DEFAULT_PROMPTS[locale]
    override = _extract_text(row.value) if row is not None else None
    return LlmPromptOut(
        locale=locale,
        default_text=default_text,
        current_text=override if override is not None else default_text,
        is_override=override is not None,
        updated_at=row.updated_at.isoformat() if (row and override is not None) else None,
        updated_by_subject_id=(
            str(row.updated_by_subject_id)
            if (row and override is not None and row.updated_by_subject_id)
            else None
        ),
    )


@router.get("", response_model=list[LlmPromptOut])
async def list_prompts(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[LlmPromptOut]:
    locales = sorted(DEFAULT_PROMPTS.keys())
    keys = [_key_for(loc) for loc in locales]
    rows = (await db.execute(select(AppSetting).where(AppSetting.key.in_(keys)))).scalars().all()
    by_key = {r.key: r for r in rows}
    return [_row_to_out(loc, by_key.get(_key_for(loc))) for loc in locales]


@router.put("/{locale}", response_model=LlmPromptOut)
async def upsert_prompt(
    locale: str,
    body: LlmPromptUpdateIn,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> LlmPromptOut:
    if locale not in DEFAULT_PROMPTS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_locale",
                "supported": sorted(DEFAULT_PROMPTS.keys()),
            },
        )
    text = body.value.strip()
    if not text:
        raise HTTPException(status_code=400, detail={"code": "empty"})
    key = _key_for(locale)
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None:
        row = AppSetting(
            key=key,
            value=text,
            scope="admin",
            description=f"Q&A system prompt override ({locale})",
            updated_at=now,
            updated_by_subject_id=admin.subject_id,
        )
        db.add(row)
    else:
        row.value = text
        row.scope = "admin"
        row.updated_at = now
        row.updated_by_subject_id = admin.subject_id
    await db.commit()
    await db.refresh(row)
    await audit.log(
        action="llm_prompt.upsert",
        actor_subject_id=admin.subject_id,
        resource_kind="app_setting",
        resource_id=None,
        metadata={"key": key, "len": len(text)},
    )
    return _row_to_out(locale, row)


@router.delete("/{locale}", response_model=LlmPromptOut)
async def reset_prompt(
    locale: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> LlmPromptOut:
    """Remove the override so the frozen default is used again."""
    if locale not in DEFAULT_PROMPTS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_locale",
                "supported": sorted(DEFAULT_PROMPTS.keys()),
            },
        )
    key = _key_for(locale)
    await db.execute(delete(AppSetting).where(AppSetting.key == key))
    await db.commit()
    await audit.log(
        action="llm_prompt.reset",
        actor_subject_id=admin.subject_id,
        resource_kind="app_setting",
        resource_id=None,
        metadata={"key": key},
    )
    return _row_to_out(locale, None)
