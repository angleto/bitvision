"""Viewport state API — per-user, per-series UI state persistence.

The backend treats ``state`` as opaque JSON; only the viewer agrees on
its shape. See docs/viewer-state-persistence.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_user
from bvphoenix.db.models import Series, User, ViewportState
from bvphoenix.db.session import get_db

router = APIRouter(tags=["viewport-state"])


class ViewportStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    series_id: uuid.UUID
    state: dict
    updated_at: datetime


class ViewportStateIn(BaseModel):
    state: dict = Field(default_factory=dict)


async def _load_row(
    db: AsyncSession, user_subject_id: uuid.UUID, series_id: uuid.UUID
) -> ViewportState | None:
    return (
        await db.execute(
            select(ViewportState).where(
                ViewportState.user_subject_id == user_subject_id,
                ViewportState.series_id == series_id,
            )
        )
    ).scalar_one_or_none()


@router.get("/viewport-state/{series_id}", response_model=ViewportStateOut)
async def get_viewport_state(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> ViewportStateOut:
    row = await _load_row(db, user.subject_id, series_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no saved state")
    return ViewportStateOut.model_validate(row)


@router.put("/viewport-state/{series_id}", response_model=ViewportStateOut)
async def put_viewport_state(
    series_id: uuid.UUID,
    body: ViewportStateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> ViewportStateOut:
    # Clean 404 for a bogus series id; the FK would otherwise raise an
    # IntegrityError on insert below.
    if (
        await db.execute(select(Series.id).where(Series.id == series_id))
    ).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="series not found")

    row = await _load_row(db, user.subject_id, series_id)
    now = datetime.now(UTC)
    if row is None:
        row = ViewportState(
            user_subject_id=user.subject_id,
            series_id=series_id,
            state=body.state,
            updated_at=now,
        )
        db.add(row)
    else:
        row.state = body.state
        row.updated_at = now
    await db.commit()
    await db.refresh(row)
    return ViewportStateOut.model_validate(row)
