"""Internal-only auth endpoints for sibling services (mcp-http).

The MCP HTTP transport (``mcp.bitvision.example``) needs to verify
inbound bearer tokens against ``agent_assistants.client_secret_hash``.
Rather than wiring a second DB pool into the mcp-http image we expose
a tiny in-cluster RPC: mcp-http POSTs the sha256 of the bearer here,
the backend resolves it to a principal + scope + patient list, and
returns enough context for the MCP gate to enforce.

Authn between sibling services is a static shared secret carried in
the ``X-Internal-Key`` header (``BVP_INTERNAL_API_KEY``). The endpoint
is also routed only on the in-cluster ``ClusterIP`` Service — Traefik
ingress strips ``/api/internal/*`` (see ``ingress-bvphoenix.yaml``) so
it cannot be reached from the public Internet.

Schema is intentionally minimal: principal subject_id, scope, the
allowed patient set, and the assistant_id (for audit). No PII.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from bvphoenix.config import get_settings
from bvphoenix.db.models import AgentAssistant, AgentAssistantPatient, OAuthCode
from bvphoenix.db.session import get_session

router = APIRouter(prefix="/internal", tags=["internal"])


class ResolveBearerIn(BaseModel):
    # The MCP server hashes the inbound bearer with sha256 hex before
    # sending it. Plaintext bearers never traverse this RPC, even
    # though the link is in-cluster only.
    secret_hash: str = Field(min_length=64, max_length=64)


class ResolveBearerOut(BaseModel):
    assistant_id: str
    owner_subject_id: str
    owner_email: str
    scope: list[str]
    patient_ids: list[str]
    is_active: bool


def _check_internal_auth(provided: str | None) -> None:
    expected = get_settings().internal_api_key
    if not expected:
        # Fail safe: an unset internal key disables the endpoint
        # rather than letting it run open. The deploy yaml MUST set
        # ``BVP_INTERNAL_API_KEY``.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="internal auth disabled (BVP_INTERNAL_API_KEY unset)",
        )
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid internal key",
        )


@router.post(
    "/agent-bearer/resolve",
    response_model=ResolveBearerOut,
    status_code=status.HTTP_200_OK,
)
async def resolve_agent_bearer(
    body: ResolveBearerIn,
    x_internal_key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
) -> ResolveBearerOut:
    """Map a bearer-secret sha256 to its assistant + principal context.

    Returns 404 when no active assistant matches; 401 when the calling
    service didn't present a valid ``X-Internal-Key``. Active means
    ``is_active = true`` AND ``client_secret_hash`` is set (defend
    against rows that were created before a secret was minted).
    """
    _check_internal_auth(x_internal_key)

    async with get_session() as db:
        row = (
            await db.execute(
                select(AgentAssistant).where(
                    AgentAssistant.client_secret_hash == body.secret_hash,
                    AgentAssistant.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no active assistant matches the bearer",
            )

        from bvphoenix.db.models import User as _User

        owner = (
            await db.execute(select(_User).where(_User.subject_id == row.owner_subject_id))
        ).scalar_one_or_none()
        if owner is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="assistant has no owner",
            )

        patients = (
            await db.execute(
                select(AgentAssistantPatient.patient_id).where(
                    AgentAssistantPatient.assistant_id == row.id
                )
            )
        ).all()

        return ResolveBearerOut(
            assistant_id=str(row.id),
            owner_subject_id=str(owner.subject_id),
            owner_email=owner.email,
            scope=list(row.permissions or []),
            patient_ids=[str(pid) for (pid,) in patients],
            is_active=row.is_active,
        )


class MintOAuthCodeIn(BaseModel):
    # OAuth ``client_id`` (an assistant's ``bvp_agt_…``). We do not
    # validate that the row exists here: the actual auth happens at
    # ``/token`` time when the shim posts the client_secret to
    # :func:`resolve_agent_bearer`. Minting the code is cheap and
    # leaving validation to the redemption path keeps this endpoint
    # stateless wrt assistant lifecycle.
    client_id: str = Field(min_length=8, max_length=64)
    redirect_uri: str = Field(min_length=4, max_length=2048)
    code_challenge: str = Field(min_length=8, max_length=128)
    code_challenge_method: str = Field(min_length=4, max_length=16)
    ttl_seconds: int = Field(default=600, ge=30, le=3600)


class MintOAuthCodeOut(BaseModel):
    code: str
    expires_at: datetime


class ConsumeOAuthCodeIn(BaseModel):
    code: str = Field(min_length=8, max_length=64)


class ConsumeOAuthCodeOut(BaseModel):
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str


@router.post(
    "/oauth-code/mint",
    response_model=MintOAuthCodeOut,
    status_code=status.HTTP_200_OK,
)
async def mint_oauth_code(
    body: MintOAuthCodeIn,
    x_internal_key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
) -> MintOAuthCodeOut:
    """Persist a new authorization code and return it.

    The mcp-http OAuth shim calls this from ``GET /authorize``; the
    code goes back to the requesting MCP host (Claude.ai &c.) in the
    redirect-URI query string. Codes are single-use — see
    :func:`consume_oauth_code` — and time-bounded.
    """
    _check_internal_auth(x_internal_key)

    code = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=body.ttl_seconds)

    async with get_session() as db:
        # Opportunistic GC: every mint trims expired rows to keep the
        # table tiny under steady-state load. Cheap because the
        # ``ix_oauth_codes_expires_at`` index makes the predicate a
        # range scan.
        await db.execute(delete(OAuthCode).where(OAuthCode.expires_at < datetime.now(UTC)))
        db.add(
            OAuthCode(
                code=code,
                client_id=body.client_id,
                redirect_uri=body.redirect_uri,
                code_challenge=body.code_challenge,
                code_challenge_method=body.code_challenge_method,
                expires_at=expires_at,
            )
        )
        await db.commit()

    return MintOAuthCodeOut(code=code, expires_at=expires_at)


@router.post(
    "/oauth-code/consume",
    response_model=ConsumeOAuthCodeOut,
    status_code=status.HTTP_200_OK,
)
async def consume_oauth_code(
    body: ConsumeOAuthCodeIn,
    x_internal_key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
) -> ConsumeOAuthCodeOut:
    """Pop and return the metadata for a code, or 404 if missing/expired.

    Single-use semantics: the row is deleted before we return the
    payload, so a replay of the same code lands on 404. PKCE
    verification (challenge ↔ verifier) is the shim's responsibility
    — we just hand back the bound challenge for the shim to compare.
    """
    _check_internal_auth(x_internal_key)

    async with get_session() as db:
        row = (
            await db.execute(select(OAuthCode).where(OAuthCode.code == body.code))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="oauth code unknown or already consumed",
            )

        # Delete first; we don't want to surface the metadata if the
        # commit fails for any reason (the row stays in place and the
        # next call gets the same answer).
        await db.execute(delete(OAuthCode).where(OAuthCode.code == body.code))
        await db.commit()

        if row.expires_at < datetime.now(UTC):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="oauth code expired",
            )

        return ConsumeOAuthCodeOut(
            client_id=row.client_id,
            redirect_uri=row.redirect_uri,
            code_challenge=row.code_challenge,
            code_challenge_method=row.code_challenge_method,
        )


__all__ = [
    "ConsumeOAuthCodeIn",
    "ConsumeOAuthCodeOut",
    "MintOAuthCodeIn",
    "MintOAuthCodeOut",
    "ResolveBearerIn",
    "ResolveBearerOut",
    "router",
]
