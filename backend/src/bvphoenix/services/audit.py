"""Append-only audit logging service.

Healthcare platforms need answers to "who looked at this patient's data,
when, from where, under which grant" — this service is the single write
path for :class:`bvphoenix.db.models.AuditLog` and the session-view
aggregation in :class:`bvphoenix.db.models.AuditSessionView` (ADR 0005).

Design notes
------------
- Fire-and-log: a failure to record the audit row MUST NOT break the
  business flow. Logs are emitted on failure so ops can chase it up.
- Own transaction: we use ``SessionFactory`` under the hood so audit
  rows land even if the caller's outer transaction rolls back. The
  audit trail is append-only evidence; rolling it back with the
  operation defeats the point.
- PHI scrubbing: metadata often carries request bodies; we best-effort
  redact obvious PHI (passwords, codice fiscale, full name) before the
  row reaches Postgres. It's not a DLP pipeline — it's a safety net.
- Read events use :func:`record_session_view`: a 15 minute idle window
  groups GETs from the same actor on the same patient. Write events
  keep per-action granularity via :func:`log_action`.
"""

from __future__ import annotations

import functools
import inspect
import logging
import re
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import select, update

from bvphoenix.config import get_settings
from bvphoenix.db.models import AuditLog, AuditSessionView
from bvphoenix.db.session import SessionFactory

_log = logging.getLogger("bvphoenix.audit")

# Keys that should never make it into the audit metadata. The spec
# calls out passwords, codice fiscale, and full names — we keep the
# list tight on purpose so investigative metadata (attempted email,
# user agent strings) still survives. Matched case-insensitively at
# any nesting depth.
_REDACT_KEYS: frozenset[str] = frozenset(
    {
        # Credentials & tokens
        "password",
        "new_password",
        "old_password",
        "current_password",
        "passwd",
        "pwd",
        "secret",
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
        # Fiscal / national identifiers
        "codice_fiscale",
        "tax_id",
        "cf",
        "ssn",
        "fiscal_code",
        # Full-name variants
        "display_name",
        "full_name",
        "first_name",
        "last_name",
        "given_name",
        "family_name",
    }
)

# Italian codice fiscale: 16 alphanumeric characters in a specific layout.
# We don't aim for 100% precision — just blot out obvious occurrences.
_CF_PATTERN = re.compile(
    r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b",
    re.IGNORECASE,
)


def _scrub(value: Any) -> Any:
    """Recursively redact obvious PHI from ``value``.

    Dict keys listed in ``_REDACT_KEYS`` get a ``"[redacted]"`` sentinel;
    lists/tuples recurse; strings are passed through a codice-fiscale
    blotter. Numbers / bools / None pass through unchanged.
    """
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if k.lower() in _REDACT_KEYS else _scrub(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return _CF_PATTERN.sub("[redacted-cf]", value)
    return value


def _client_ip(request: Request | None) -> str | None:
    """Best-effort client IP extraction, honoring X-Forwarded-For if
    set by a trusted proxy (nginx/traefik in the infra manifest).

    When behind a reverse proxy, the first hop in X-Forwarded-For is
    the originating client — that's what's forensically interesting.
    """
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    candidate: str | None = None
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            candidate = first
    if not candidate:
        real = request.headers.get("x-real-ip")
        if real:
            candidate = real.strip()
    if not candidate and request.client and request.client.host:
        candidate = request.client.host
    # audit_log.ip_address is INET — drop pseudo-hosts (e.g. the Starlette
    # TestClient sends "testclient") so the insert doesn't fail.
    if candidate and not _looks_like_ip(candidate):
        return None
    return candidate


def _looks_like_ip(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    ua = request.headers.get("user-agent")
    return ua[:1024] if ua else None


def _agent_provenance_from_request(
    request: Request | None,
) -> tuple[uuid.UUID | None, str | None, str | None]:
    """Pull ``(agent_token_id, model_version, conversation_id)`` from
    request state and headers.

    The auth deps put the resolved :class:`AgentToken` on
    ``request.state.agent_token`` when an agent JWT was presented.
    ``X-Conversation-Id`` and ``X-Model-Version`` are agent-supplied
    soft hints — we record them as-is, capped at 128 chars.
    """
    if request is None:
        return None, None, None
    agent_token = getattr(request.state, "agent_token", None)
    agent_token_id = getattr(agent_token, "id", None) if agent_token else None
    model_version = (request.headers.get("x-model-version") or "")[:128] or None
    conversation_id = (request.headers.get("x-conversation-id") or "")[:128] or None
    return agent_token_id, model_version, conversation_id


async def log_action(
    *,
    actor_subject_id: uuid.UUID | None,
    action: str,
    resource_kind: str | None = None,
    resource_id: uuid.UUID | None = None,
    request: Request | None = None,
    metadata: dict[str, Any] | None = None,
    agent_token_id: uuid.UUID | None = None,
    model_version: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Write an audit row. Swallows errors — never re-raise to the caller.

    ``action`` is a free-form verb (e.g. ``"login_success"``,
    ``"study_view"``) — see ``docs/security-audit-log.md`` for the
    taxonomy. The optional ``agent_token_id`` / ``model_version`` /
    ``conversation_id`` carry AI provenance (ADR 0005); when omitted
    they are auto-derived from ``request`` state if available.
    """
    safe_metadata = _scrub(metadata) if metadata else {}
    if agent_token_id is None and model_version is None and conversation_id is None:
        agent_token_id, model_version, conversation_id = _agent_provenance_from_request(request)
    try:
        session = SessionFactory()
        try:
            entry = AuditLog(
                actor_subject_id=actor_subject_id,
                action=action,
                resource_kind=resource_kind,
                resource_id=resource_id,
                metadata_=safe_metadata,
                ip_address=_client_ip(request),
                user_agent=_user_agent(request),
                agent_token_id=agent_token_id,
                model_version=model_version,
                conversation_id=conversation_id,
            )
            session.add(entry)
            await session.commit()
        finally:
            await session.close()
    except Exception as exc:
        _log.warning(
            "audit.log_action failed action=%s resource_kind=%s resource_id=%s: %s",
            action,
            resource_kind,
            resource_id,
            exc,
        )


def _session_window_seconds() -> int:
    """Idle window for session-view aggregation (default 15 minutes)."""
    settings = get_settings()
    return getattr(settings, "audit_session_window_seconds", 15 * 60)


async def record_session_view(
    *,
    actor_subject_id: uuid.UUID | None,
    patient_id: uuid.UUID | None,
    resource_kind: str,
    request: Request | None = None,
) -> None:
    """Aggregate a read into ``audit_session_view``.

    Look up the most recent session row for ``(actor, patient,
    agent_token, ip)``; if it is younger than the idle window, bump
    ``last_event_at`` / ``read_count`` and add the kind to the touched
    set. Otherwise insert a new row.

    Failures are swallowed: a missing session view never breaks the
    user-visible request.
    """
    agent_token_id, _model_version, conversation_id = _agent_provenance_from_request(request)
    ip = _client_ip(request)
    ua = _user_agent(request)
    window = timedelta(seconds=_session_window_seconds())
    now = datetime.now(UTC)

    try:
        session = SessionFactory()
        try:
            existing = (
                await session.execute(
                    select(AuditSessionView)
                    .where(
                        AuditSessionView.actor_subject_id == actor_subject_id,
                        AuditSessionView.patient_id == patient_id,
                        AuditSessionView.agent_token_id == agent_token_id,
                        AuditSessionView.last_event_at >= now - window,
                    )
                    .order_by(AuditSessionView.last_event_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if existing is None:
                row = AuditSessionView(
                    actor_subject_id=actor_subject_id,
                    patient_id=patient_id,
                    agent_token_id=agent_token_id,
                    conversation_id=conversation_id,
                    first_event_at=now,
                    last_event_at=now,
                    read_count=1,
                    resource_kinds_touched=[resource_kind],
                    ip_address=ip,
                    user_agent=ua,
                )
                session.add(row)
            else:
                kinds = list(existing.resource_kinds_touched or [])
                if resource_kind not in kinds:
                    kinds.append(resource_kind)
                await session.execute(
                    update(AuditSessionView)
                    .where(AuditSessionView.id == existing.id)
                    .values(
                        last_event_at=now,
                        read_count=AuditSessionView.read_count + 1,
                        resource_kinds_touched=kinds,
                    )
                )
            await session.commit()
        finally:
            await session.close()
    except Exception as exc:
        # phi-safe: UUIDs are non-clinical identifiers
        _log.warning(
            "audit.record_session_view failed actor=%s patient=%s kind=%s: %s",
            actor_subject_id,
            patient_id,
            resource_kind,
            exc,
        )


def audit_write(
    action: str,
    resource_kind: str,
    *,
    resource_id_kw: str | None = None,
) -> Callable[[Callable[..., Coroutine[Any, Any, Any]]], Callable[..., Coroutine[Any, Any, Any]]]:
    """Decorator: log a write action after the wrapped endpoint succeeds.

    Usage::

        @router.patch("/api/patients/{pid}/documents/{did}")
        @audit_write("document_update", "document", resource_id_kw="did")
        async def update_document(pid: UUID, did: UUID, ...): ...

    Behaviour:

    * The wrapped function MUST be ``async``.
    * The decorator extracts ``Request`` from kwargs (or the wrapped
      callable's signature) to attribute IP / UA / agent provenance.
    * ``resource_id_kw``: name of the keyword argument carrying the
      resource UUID. Optional — when omitted, ``resource_id`` is left
      ``None``.
    * Failure to write the audit row never propagates: the underlying
      ``log_action`` call swallows.
    * Audit is fired *after* the handler returns successfully. A
      raised exception aborts the audit (the operation didn't happen).
    """

    def decorator(
        fn: Callable[..., Coroutine[Any, Any, Any]],
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await fn(*args, **kwargs)

            try:
                bound = sig.bind_partial(*args, **kwargs)
                request: Request | None = None
                for value in bound.arguments.values():
                    if isinstance(value, Request):
                        request = value
                        break

                actor_subject_id: uuid.UUID | None = None
                if request is not None:
                    user = getattr(request.state, "user", None)
                    sid = getattr(user, "subject_id", None) if user else None
                    if isinstance(sid, uuid.UUID):
                        actor_subject_id = sid

                rid_value = bound.arguments.get(resource_id_kw) if resource_id_kw else None
                resource_id = rid_value if isinstance(rid_value, uuid.UUID) else None

                await log_action(
                    actor_subject_id=actor_subject_id,
                    action=action,
                    resource_kind=resource_kind,
                    resource_id=resource_id,
                    request=request,
                )
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning(
                    "audit_write decorator failed action=%s kind=%s: %s",
                    action,
                    resource_kind,
                    exc,
                )

            return result

        return wrapper

    return decorator
