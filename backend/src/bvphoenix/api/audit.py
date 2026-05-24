"""Audit log read API — admin-only.

The write path lives in :mod:`bvphoenix.services.audit`; this module
only exposes a paginated, filterable view over the ``audit_log`` table
so security reviewers and admins can answer "who did what, when".

Non-admin access is refused with 403. We deliberately skip any
per-resource ACL — the audit log is a security-team artifact, not a
user-facing trail.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_admin, require_user
from bvphoenix.db.models import AuditLog, User
from bvphoenix.db.session import get_db
from bvphoenix.services import audit as audit_service

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditEntryOut(BaseModel):
    id: str
    actor_subject_id: str | None
    action: str
    resource_kind: str | None
    resource_id: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)
    ip_address: str | None
    user_agent: str | None
    created_at: str


class PaginatedAudit(BaseModel):
    items: list[AuditEntryOut]
    total: int
    limit: int
    offset: int


def _to_out(row: AuditLog) -> AuditEntryOut:
    return AuditEntryOut(
        id=str(row.id),
        actor_subject_id=str(row.actor_subject_id) if row.actor_subject_id else None,
        action=row.action,
        resource_kind=row.resource_kind,
        resource_id=str(row.resource_id) if row.resource_id else None,
        metadata=row.metadata_ or {},
        ip_address=str(row.ip_address) if row.ip_address else None,
        user_agent=row.user_agent,
        created_at=row.created_at.isoformat(),
    )


@router.get("", response_model=PaginatedAudit)
async def list_audit_entries(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
    actor: uuid.UUID | None = Query(None, description="Filter by actor subject_id"),
    action: str | None = Query(None, max_length=64),
    resource_kind: str | None = Query(None, max_length=32),
    resource_id: uuid.UUID | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> PaginatedAudit:
    """Paginated audit log query. Admin-only."""
    base = select(AuditLog)
    if actor is not None:
        base = base.where(AuditLog.actor_subject_id == actor)
    if action:
        base = base.where(AuditLog.action == action)
    if resource_kind:
        base = base.where(AuditLog.resource_kind == resource_kind)
    if resource_id is not None:
        base = base.where(AuditLog.resource_id == resource_id)
    if from_ is not None:
        base = base.where(AuditLog.created_at >= from_)
    if to is not None:
        base = base.where(AuditLog.created_at <= to)

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    rows = (
        (await db.execute(base.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )

    return PaginatedAudit(
        items=[_to_out(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


# ----- MCP HTTP audit hop (ADR 0018) ---------------------------------------


class MCPAuditIn(BaseModel):
    """Body for ``POST /api/audit/mcp``.

    Posted fire-and-forget by the remote MCP HTTP server after every
    request. Carries the OAuth subject claim, the active scope set,
    the originating IP, and the response status. We trust the bearer
    JWT for the actor identity; the rest is verbatim.
    """

    subject_id: str | None = None
    scopes: list[str] = Field(default_factory=list)
    ip: str | None = None
    status_code: int | None = None
    tool: str | None = Field(default=None, max_length=128)


@router.post("/mcp", status_code=201, response_model=dict)
async def record_mcp_http_request(
    body: MCPAuditIn,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> dict[str, str]:
    """Append a row to ``audit_log`` for an inbound MCP HTTP call.

    Authenticated by the same JWT the MCP forwarded; the user's
    subject_id is what we record (we never trust ``body.subject_id``
    when the bearer was valid). Status code goes into the metadata
    JSONB so reporters can split success from failure without having
    to re-issue a query.
    """
    metadata: dict[str, Any] = {
        "scopes": body.scopes,
        "status_code": body.status_code,
        "ip": body.ip,
        "kind": "mcp_http",
    }
    if body.tool:
        metadata["tool"] = body.tool

    await audit_service.log_action(
        actor_subject_id=user.subject_id,
        action="mcp_http_request",
        resource_kind="mcp",
        resource_id=None,
        request=request,
        metadata=metadata,
    )
    return {"status": "accepted"}
