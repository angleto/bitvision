"""FastAPI auth dependencies.

- ``optional_user`` — returns the user if a valid token is present, else None.
  Use this for routes that gracefully degrade for anonymous visitors
  (public study browsing, anonymous similarity search). Agent tokens are
  resolved here too: the owning human user is returned and
  ``request.state.agent_token`` is populated with the concrete row so
  downstream scope checks can run.
- ``require_user`` — 401 if not authenticated (user *or* agent).
- ``require_admin`` — 403 if not an admin. Agent tokens never satisfy
  this; admin actions are strictly human-gated.
- ``require_agent_scope(scope)`` — 403 unless the caller is an agent
  token whose ``scope`` claim contains ``scope``.
- ``enforce_agent_patient_scope(request, patient_id)`` — 403 if the
  request is agent-authenticated and the token's ``patient_id`` does
  not match. No-op for human callers and for agent tokens that were
  minted without a patient restriction (operator / service tokens).

Anonymous-first traffic is a platform pillar (DESIGN.md §2), so default
to ``optional_user`` and only require auth where it's strictly needed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.tokens import decode_token, hash_agent_token
from bvphoenix.db.models import (
    AgentAssistant,
    AgentAssistantPatient,
    AgentToken,
    Grant,
    RevokedToken,
    User,
)
from bvphoenix.db.session import get_db, set_current_subject

bearer_scheme = HTTPBearer(auto_error=False)

# Mirror of ``bvphoenix.api.auth.SESSION_COOKIE_NAME``. Duplicated as a
# string literal to avoid a circular import (auth.py imports
# ``require_user`` from this module).
_SESSION_COOKIE_NAME = "bvp_session"


def _creds_from_request(
    request: Request,
    creds: HTTPAuthorizationCredentials | None,
) -> HTTPAuthorizationCredentials | None:
    """Resolve the bearer token from the most-specific source available.

    Priority:
      1. ``Authorization: Bearer`` header — preserved for explicit API
         clients (curl, the import CLI, MCP transports);
      2. ``bvp_session`` HttpOnly cookie — set by the SPA login flow
         (see ``bvphoenix.api.auth._set_session_cookie``).

    Returning a synthesised ``HTTPAuthorizationCredentials`` instead of
    a raw string keeps the downstream chain unchanged: every existing
    resolver still consumes ``creds.credentials``.
    """
    if creds is not None:
        return creds
    cookie_token = request.cookies.get(_SESSION_COOKIE_NAME)
    if cookie_token:
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=cookie_token)
    return None


async def _resolve_user_from_creds(
    request: Request,
    creds: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
) -> User | None:
    """Shared resolution: turn (creds | None) into (User | None).

    Returns None when no creds, malformed token, expired/revoked agent
    token, or user not found. The caller decides how to react: strict
    (raise 401) or permissive (return None for anonymous browsing).
    """
    effective = _creds_from_request(request, creds)
    if effective is None:
        return None
    return await _resolve_credential(request, effective, db)


async def optional_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """Dep used by endpoints that *should* require login when
    ``require_auth_globally`` is on (F12.2d default).

    Anonymous callers receive 401. Authenticated callers see the
    User. Endpoints that genuinely need anonymous access (a2a
    JSON-RPC, transparency stats, shared-link) use :func:`public_user`
    instead.
    """
    user = await _resolve_user_from_creds(request, creds, db)
    if user is None:
        from bvphoenix.config import get_settings as _get_settings

        if _get_settings().require_auth_globally:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return user


async def public_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """Dep used by endpoints that intentionally accept anonymous callers.

    Anonymous OK; authenticated returns the User. Use sparingly for
    public-by-design surfaces (a2a, transparency, shared-link, healthz).
    """
    return await _resolve_user_from_creds(request, creds, db)


async def _resolve_assistant_secret(
    request: Request,
    db: AsyncSession,
    plaintext_bearer: str,
) -> User | None:
    """Try to authenticate a request as an AI assistant using the
    plaintext per-assistant secret.

    Computes ``sha256(bearer)`` and looks up an active
    ``AgentAssistant`` whose ``client_secret_hash`` matches. On a
    hit, sets the agent context (``request.state.is_agent`` etc.)
    and returns the assistant's owner User; otherwise returns
    ``None`` so the caller can fall through to the next auth path.
    """
    import hashlib as _hashlib

    candidate = plaintext_bearer.strip()
    if not candidate or len(candidate) < 16:
        return None
    secret_hash = _hashlib.sha256(candidate.encode("utf-8")).hexdigest()

    row = (
        await db.execute(
            select(AgentAssistant).where(
                AgentAssistant.client_secret_hash == secret_hash,
                AgentAssistant.is_active.is_(True),
                # Explicit revoked_at gate — operator-fired security
                # event. A row carrying ``revoked_at IS NOT NULL`` is
                # never honoured even if ``is_active`` was flipped
                # back to True by mistake.
                AgentAssistant.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    owner = (
        await db.execute(select(User).where(User.subject_id == row.owner_subject_id))
    ).scalar_one_or_none()
    if owner is None:
        return None

    allowed_patient_rows = (
        await db.execute(
            select(AgentAssistantPatient.patient_id).where(
                AgentAssistantPatient.assistant_id == row.id
            )
        )
    ).all()
    request.state.agent_token = None
    request.state.agent_assistant_id = row.id
    request.state.agent_patient_ids = {pid for (pid,) in allowed_patient_rows}
    request.state.agent_scope = list(row.permissions or [])
    request.state.is_agent = True
    await set_current_subject(db, str(owner.subject_id))
    return owner


async def _resolve_credential(
    request: Request,
    creds: HTTPAuthorizationCredentials,
    db: AsyncSession,
) -> User | None:
    """Internal: parse a bearer credential into a User row.

    Three paths share this entry point:

    1. **Standard phoenix JWT** — ``decode_token`` succeeds against
       the local signing secret; the row is loaded from
       ``users.subject_id``.
    2. **Per-assistant client_secret** — the bearer is the plaintext
       secret minted by ``POST /api/ai-assistants``. We sha256 it
       and look the assistant up by ``client_secret_hash``; on a
       hit (with ``is_active = true``) the request is promoted to
       an agent context with the assistant's owner / scope / patient
       set.
    3. **Anonymous** — neither path matches; we return ``None`` and
       let the upstream dep decide whether 401 or "ok, public" is
       the right answer.
    """
    payload = decode_token(creds.credentials)

    # If the bearer is not a phoenix JWT, try matching it against the
    # per-assistant secrets table before giving up. This is the
    # machine-to-machine path used by Claude.ai and any other MCP
    # client.
    if payload is None:
        promoted = await _resolve_assistant_secret(request, db, creds.credentials)
        return promoted

    # ADR 0016: every JWT minted by the backend carries ``jti``. If the
    # caller presents a token whose ``jti`` is in ``revoked_tokens``,
    # treat it as anonymous (the higher-level dep will 401). Tokens
    # without ``jti`` (legacy / pre-migration) bypass this gate.
    if payload.jti is not None:
        revoked = (
            await db.execute(select(RevokedToken.jti).where(RevokedToken.jti == payload.jti))
        ).first()
        if revoked is not None:
            return None

    if payload.typ == "agent":
        # Legacy local-token agents (pre v2.1.11). Kept around so
        # already-issued JWTs do not break overnight; new assistants
        # always go through the bearer-hash path above.
        if payload.owner_subject_id is None:
            return None
        owner = (
            await db.execute(select(User).where(User.subject_id == payload.owner_subject_id))
        ).scalar_one_or_none()
        if owner is None:
            return None

        token_hash = hash_agent_token(creds.credentials)
        local_row = (
            await db.execute(select(AgentToken).where(AgentToken.token_hash == token_hash))
        ).scalar_one_or_none()
        if local_row is None:
            return None
        if local_row.revoked_at is not None or local_row.expires_at <= datetime.now(UTC):
            return None
        assistant_id = local_row.assistant_id
        request.state.agent_token = local_row

        allowed_patient_rows = (
            await db.execute(
                select(AgentAssistantPatient.patient_id).where(
                    AgentAssistantPatient.assistant_id == assistant_id
                )
            )
        ).all()
        request.state.agent_patient_ids = {pid for (pid,) in allowed_patient_rows}
        request.state.agent_scope = list(payload.scope)
        request.state.is_agent = True
        await set_current_subject(db, str(owner.subject_id))
        return owner

    # Standard user branch.
    user = (
        await db.execute(select(User).where(User.subject_id == payload.subject_id))
    ).scalar_one_or_none()
    if user is not None:
        # Bind the RLS principal so migration 0009's policies see the caller.
        await set_current_subject(db, str(user.subject_id))
        return user

    # Shared-link branch: tokens minted by ``/api/shared/{token}/verify``
    # carry ``sub = PUBLIC_SUBJECT_ID`` and a ``grant_id`` claim. Public
    # has a row in ``subjects`` but never in ``users`` (it's a synthetic
    # principal), so the SELECT above returns None. We synthesize a
    # transient User the rest of the auth chain can read — the
    # permission layer will resolve real access via the grant matching
    # ``grantee_subject_id = public`` later on.
    if payload.grant_id is not None:
        grant = (
            await db.execute(select(Grant).where(Grant.id == payload.grant_id))
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if grant is None or grant.revoked_at is not None:
            return None
        if grant.valid_from > now:
            return None
        if grant.valid_until is not None and grant.valid_until < now:
            return None
        synthetic = User(
            subject_id=payload.subject_id,
            email=payload.email or "shared-link",
            is_admin=False,
            # SQLAlchemy server_default='true' on the column only kicks
            # in when a row is INSERTed and reloaded; an unpersisted
            # instance keeps ``is_active=None``, which ``require_user``
            # reads as ``not None == True`` and rejects the session
            # with ``403 account_blocked``. Pin it so share-link
            # sessions reach endpoints that go through ``require_user``
            # (care-timeline, provenance, …), not just the
            # ``optional_user`` ones.
            is_active=True,
        )
        # Pin the resolved grant onto the User instance so the
        # downstream visibility filters / permission helpers can scope
        # to the SINGLE grant that minted this JWT, instead of treating
        # every active grant against PUBLIC_SUBJECT_ID as in scope.
        # Without this every share-link session would see every other
        # patient currently shared via link — a cross-patient leak.
        # ``_share_grant`` is a transient attribute on the unmapped
        # synthetic instance; mapped User columns are unaffected.
        synthetic._share_grant = grant  # type: ignore[attr-defined]
        request.state.share_grant = grant
        # Anonymous-link writes are attributed in the versioning DAG
        # via ``ActorContext.kind='link'`` and ``share_link_id``. The
        # JWT carries the originating ``share_links.id``; we pin it to
        # the request state so ``versioning_hooks.resolve_actor`` can
        # find it without a DB round-trip.
        if payload.share_link_id is not None:
            request.state.share_link_id = payload.share_link_id
        await set_current_subject(db, str(payload.subject_id))
        return synthetic

    return None


async def require_user(
    user: Annotated[User | None, Depends(optional_user)],
) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Soft account lock — an admin can flip ``is_active=false`` from
    # the admin dashboard. Existing JWTs are still cryptographically
    # valid but the request is refused so the user effectively
    # disappears from the platform until unblocked. Admins are still
    # allowed through so they can't lock themselves out.
    if not getattr(user, "is_active", True) and not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "account_blocked",
                "reason": getattr(user, "blocked_reason", None),
            },
        )
    return user


async def require_admin(
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> User:
    # Agent tokens can never satisfy admin gates, even if the owner is
    # an admin. This prevents a leaked agent credential from being
    # used to perform destructive account-wide operations.
    if getattr(request.state, "is_agent", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin actions require a user session"
        )
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return user


def require_agent_scope(scope: str):
    """Build a dependency that enforces ``scope`` on the calling agent
    token.

    The returned dep resolves only when the caller presents a valid
    agent token *and* its ``scope`` claim contains ``scope``. Use on
    endpoints designed to be invoked by an AI agent on a patient's
    behalf, e.g. ``Depends(require_agent_scope("consultation:write"))``.
    """

    async def _dep(
        request: Request,
        user: Annotated[User, Depends(require_user)],
    ) -> User:
        if not getattr(request.state, "is_agent", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="agent token required",
            )
        # Delegated check goes through ``enforce_agent_scope`` so the
        # legacy/canonical alias map applies here too.
        enforce_agent_scope(request, scope)
        return user

    return _dep


def require_scope_if_agent(scope: str):
    """Build a no-arg FastAPI dependency that enforces ``scope`` on
    agent-token requests and is a no-op for human users.

    Use as ``_: None = Depends(require_scope_if_agent("patient:images"))``
    to gate an endpoint without changing its signature for human
    callers. Internally a one-line wrapper around
    :func:`enforce_agent_scope`.
    """

    def _dep(request: Request) -> None:
        enforce_agent_scope(request, scope)

    return _dep


# Bridge between the legacy singular scope catalog (carried by
# already-issued ``agent_assistants.permissions`` rows and still
# referenced by older backend call sites) and the canonical plural
# catalog defined in ``mcp/src/bvmcp/scopes.py``. A scope check
# succeeds when EITHER name is held — both directions.
#
# This mirrors ``mcp/src/bvmcp/auth.py::_SCOPE_ALIASES`` and must stay
# in sync with it; the comment in ``api/ai_assistants.py`` calls out
# the dual-catalog drift the operator-facing UI inherited until
# 2026-05-03. Without normalisation here, a token granted only the
# canonical names ("patients:read") gets 403 on every backend gate
# that still asks for the singular legacy name ("patient:read"), and
# vice versa — exactly the symptom that surfaced on the prod MCP
# session 2026-05-03.
_LEGACY_SCOPE_ALIASES: dict[str, frozenset[str]] = {
    "patient:read": frozenset({"patients:read"}),
    "patient:write": frozenset({"patients:write"}),
    "patient:images": frozenset({"imaging:read"}),
    "patient:identify": frozenset({"patients:identify"}),
    "consultation:read": frozenset({"events:read", "reports:read"}),
    "consultation:write": frozenset({"events:write", "reports:write"}),
    "documents:download": frozenset({"documents:read"}),
}

# Reverse map for required-side normalisation: when a legacy required
# scope has a single canonical replacement we can substitute it; the
# multi-target legacy aliases (``consultation:*``) are kept as-is and
# resolved owner-side via expansion.
_LEGACY_TO_CANONICAL: dict[str, str] = {
    legacy: next(iter(canonical))
    for legacy, canonical in _LEGACY_SCOPE_ALIASES.items()
    if len(canonical) == 1
}


def _expand_owned_scopes(scopes: set[str]) -> set[str]:
    """Add the canonical implications of every legacy scope held by
    the agent. ``patient:read`` adds ``patients:read``,
    ``consultation:write`` adds ``events:write`` and ``reports:write``,
    etc. The original names are preserved so logging stays truthful."""
    expanded = set(scopes)
    for s in scopes:
        if s in _LEGACY_SCOPE_ALIASES:
            expanded.update(_LEGACY_SCOPE_ALIASES[s])
    return expanded


def enforce_agent_scope(request: Request, *required_scopes: str) -> None:
    """Refuse the request when an agent token does not carry at least
    one of ``required_scopes``.

    No-op when the caller is a human user. Raises ``HTTPException(403)``
    otherwise.

    Use as a function-call gate at the top of an endpoint when you want
    fine-grained capability control on top of
    ``enforce_agent_patient_scope`` (which only handles the patient
    invariant). Multiple ``required_scopes`` are OR-ed: pass them when
    an endpoint accepts more than one capability flavour
    (e.g. ``enforce_agent_scope(request, "studies:write_metadata",
    "patient:full")``).

    Both legacy singular names ("patient:read") and the canonical
    plural names ("patients:read") are accepted on either side via
    :data:`_LEGACY_SCOPE_ALIASES`. New call sites should pass the
    canonical names.

    Counterpart to ``require_agent_scope``: the latter MANDATES an
    agent token, this one is a no-op for humans which is what you
    want for endpoints that serve both audiences.
    """
    if not getattr(request.state, "is_agent", False):
        return
    owned = _expand_owned_scopes(set(getattr(request.state, "agent_scope", []) or []))
    for required in required_scopes:
        if required in owned:
            return
        canonical = _LEGACY_TO_CANONICAL.get(required)
        if canonical is not None and canonical in owned:
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=("agent token missing required scope: " + " or ".join(sorted(required_scopes))),
    )


def enforce_agent_patient_scope(
    request: Request,
    patient_id: uuid.UUID | None,
    *,
    scope: str | None = None,
) -> None:
    """Refuse the request when an agent token's assistant has not been
    explicitly granted access to ``patient_id``.

    No-op when:

    * the caller is a human user (``request.state.is_agent`` is False
      or unset);
    * the request does not resolve to a specific patient
      (``patient_id is None``).

    Raises ``HTTPException(403)`` otherwise. Call this at the top of
    any patient-scoped endpoint right after the target patient has
    been loaded from the DB, so a leaked token cannot traverse across
    fascicoli even if the owner's broader human permissions would
    allow it.

    Optional ``scope``: if provided, also enforce that the agent
    token's ``scope`` claim contains it (via :func:`enforce_agent_scope`).
    Use this to combine the per-patient invariant with a capability
    check in one call: ``enforce_agent_patient_scope(req, pid,
    scope="patient:read")``.

    The allowed patient set is loaded once per request in
    ``_resolve_user`` (the agent branch) and cached on
    ``request.state.agent_patient_ids``. Membership check is O(1).
    """
    if not getattr(request.state, "is_agent", False):
        return
    if patient_id is not None:
        allowed: set[uuid.UUID] | None = getattr(request.state, "agent_patient_ids", None)
        if allowed is None or patient_id not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="agent token is not authorised for this patient",
            )
    if scope is not None:
        enforce_agent_scope(request, scope)


async def active_share_grant(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Grant | None:
    """Return the share-link ``Grant`` encoded in the caller's token, if any.

    Returns ``None`` when the request isn't share-link-authenticated (the
    token has no ``grant_id`` claim, or there's no token at all). Routes
    use this to decide whether grant-scoped transforms like DICOM
    de-identification apply.
    """
    if creds is None:
        return None
    payload = decode_token(creds.credentials)
    if payload is None or payload.grant_id is None:
        return None
    return (
        await db.execute(select(Grant).where(Grant.id == payload.grant_id))
    ).scalar_one_or_none()
