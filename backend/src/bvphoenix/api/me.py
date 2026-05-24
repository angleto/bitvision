"""``/api/me/...`` — caller introspection.

Tools and the GUI use these endpoints to gate UI affordances and to
let agents decide whether to attempt a write or downgrade to a read.
The handlers never expose internal state (DB ids that are not safe
to disclose, bucket names, etc.); they return only what the caller
already conceptually owns about themselves.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from bvphoenix.auth.deps import require_user
from bvphoenix.db.models import User

router = APIRouter(tags=["me"])


class MyScopesOut(BaseModel):
    """OAuth scopes the caller currently holds."""

    subject_id: uuid.UUID
    is_admin: bool
    scopes: list[str] = Field(default_factory=list)
    agent_token_id: uuid.UUID | None = None
    is_agent: bool = False


@router.get("/me/scopes", response_model=MyScopesOut)
async def get_my_scopes(
    user: Annotated[User, Depends(require_user)],
    request: Request,
) -> MyScopesOut:
    """Return the OAuth scopes attached to the current request.

    Agent tokens carry an explicit scope set on ``request.state``;
    human sessions are scope-unrestricted but still carry the admin
    flag for write-gated surfaces.
    """
    state: Any = request.state
    raw_scopes = getattr(state, "agent_scopes", None)
    if isinstance(raw_scopes, (set, list, tuple)):
        scopes = sorted({str(s) for s in raw_scopes})
    elif isinstance(raw_scopes, str):
        scopes = sorted({s for s in raw_scopes.split() if s})
    else:
        scopes = []

    agent_token_id = getattr(state, "agent_token_id", None)
    is_agent = bool(getattr(state, "is_agent", False) or agent_token_id)

    return MyScopesOut(
        subject_id=user.subject_id,
        is_admin=bool(user.is_admin),
        scopes=scopes,
        agent_token_id=agent_token_id,
        is_agent=is_agent,
    )


__all__ = ["router"]
