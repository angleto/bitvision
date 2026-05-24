"""Request-scoped audit context.

Endpoints declare ``audit: AuditContext = Depends(get_audit_context)``
and then call ``await audit.log(action=..., ...)`` at the point of
interest. The context pre-binds the incoming :class:`Request` so call
sites stay terse — one line per sensitive endpoint, matching
``docs/security-audit-log.md``.

Audit writes are fire-and-forget inside the service (see
:mod:`bvphoenix.services.audit`), so callers never need to handle
exceptions here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request

from bvphoenix.services import audit as audit_service


@dataclass(slots=True)
class AuditContext:
    """Bound audit writer for the current request."""

    request: Request

    async def log(
        self,
        *,
        action: str,
        actor_subject_id: uuid.UUID | None = None,
        resource_kind: str | None = None,
        resource_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await audit_service.log_action(
            actor_subject_id=actor_subject_id,
            action=action,
            resource_kind=resource_kind,
            resource_id=resource_id,
            request=self.request,
            metadata=metadata,
        )

    async def session_view(
        self,
        *,
        actor_subject_id: uuid.UUID | None,
        patient_id: uuid.UUID | None,
        resource_kind: str,
    ) -> None:
        """Aggregate a read into ``audit_session_view`` (ADR 0005).

        Use on GET endpoints that touch patient-scoped data. Writes go
        through :meth:`log` with a granular action; reads use this so
        the per-GET volume stays bounded.
        """
        await audit_service.record_session_view(
            actor_subject_id=actor_subject_id,
            patient_id=patient_id,
            resource_kind=resource_kind,
            request=self.request,
        )


async def get_audit_context(request: Request) -> AuditContext:
    """FastAPI dependency that hands back a request-scoped audit logger."""
    return AuditContext(request=request)


# Type alias for terse ``Annotated[AuditContext, Depends(...)]`` usage.
AuditDep = Annotated[AuditContext, Depends(get_audit_context)]
