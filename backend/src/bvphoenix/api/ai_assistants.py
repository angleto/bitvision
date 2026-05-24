"""AI assistant management API.

A bitvision user configures one or more *AI assistants* and shares
specific patients with each. The same patient can be shared with
multiple assistants — that's the benchmark / second-opinion workflow.

Each assistant carries its own machine-to-machine credentials
(``client_id`` + ``client_secret``). The MCP HTTP transport
authenticates the inbound request by sha256-matching the Bearer
token against ``client_secret_hash``; when a row matches and
``is_active`` is true, the request is promoted to an agent context
with the assistant's scope set + patient list.

Endpoints
---------
``POST   /api/ai-assistants``                  create + reveal client_secret once
``GET    /api/ai-assistants``                  list (no secret reveal)
``GET    /api/ai-assistants/{id}``             single
``PATCH  /api/ai-assistants/{id}``             edit metadata / permissions / is_active
``DELETE /api/ai-assistants/{id}``             cascade-delete
``POST   /api/ai-assistants/{id}/rotate``      generate new client_secret, invalidate old
``GET    /api/ai-assistants/{id}/patients``    list shared patients
``POST   /api/ai-assistants/{id}/patients``    share a patient
``DELETE /api/ai-assistants/{id}/patients/{patient_id}``  un-share
``GET    /api/ai-assistants/scope-catalog``    OAuth scope catalog
``GET    /api/ai-assistants/connector-info``   MCP base URL + onboarding hint

Authorization vocabulary is kept narrow and enforced server-side
(see ``backend/src/bvphoenix/auth/deps.py:enforce_agent_scope``).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated

from bvmcp.scopes import SCOPE_CATALOG, ScopeDef
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_user
from bvphoenix.db.models import (
    AgentAssistant,
    AgentAssistantPatient,
    Patient,
    User,
)
from bvphoenix.db.session import get_db
from bvphoenix.services.permissions import READ_METADATA, can_patient

router = APIRouter(prefix="/ai-assistants", tags=["ai-assistants"])


# ---------- credential helpers ----------


def _generate_client_id() -> str:
    """``bvp_agt_<32-hex>`` — printable, unique, easy to grep in audit
    logs."""
    return f"bvp_agt_{uuid.uuid4().hex}"


def _generate_client_secret() -> str:
    """48-byte URL-safe random. ~64 chars of base64url, ~256 bits of
    entropy. The plaintext leaves the server exactly once: in the
    create / rotate response."""
    return secrets.token_urlsafe(48)


def _hash_client_secret(plaintext: str) -> str:
    """Lowercase hex sha256. The MCP gate computes the same hash on
    the inbound bearer and looks the assistant up by the resulting
    string — keeping plaintext out of the DB without giving up O(1)
    auth lookups."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _client_secret_prefix(plaintext: str) -> str:
    """First 8 chars of the secret. Surfaced in the UI ("…Z9_kxR3p")
    so the operator can tell two assistants apart at a glance without
    storing the full credential anywhere outside the K8s Secret-style
    one-shot reveal."""
    return plaintext[:8]


# Single source of truth: the canonical scope catalog lives in
# ``mcp/src/bvmcp/scopes.py`` and is enforced by the MCP HTTP gate
# (``has_scope`` in mcp/src/bvmcp/auth.py). Until 2026-05-03 the backend
# kept its own narrower list of grantable scopes (10 legacy keys) and
# the UI never exposed the full surface — "abilita tutti" produced
# tokens missing ``documents:write`` / ``documents:ingest`` / etc., so
# every write tool returned 403 even with all checkboxes ticked.
# Deriving both ``_ALLOWED_PERMISSIONS`` and ``_SCOPE_CATALOG`` from
# the canonical catalog removes the drift.

# Scope concedibili a un agent token: tutto il catalogo MCP eccetto
# i scope strutturalmente human-only (``synthesis:sign`` — il backend
# rifiuta comunque la chiamata se arriva da un agent token, vedi
# scopes.py:84-89).
_GRANTABLE_SCOPES: frozenset[str] = frozenset(s.id for s in SCOPE_CATALOG if not s.human_only)

# Scope legacy (pre-Sprint 6) ancora salvati nelle rows
# ``agent_assistants.permissions`` di assistant esistenti. Restano
# accettati dal validatore PATCH affinché un GET-poi-PATCH dello stesso
# set non rompa nulla. Il MCP gate li espande a runtime via
# ``bvmcp.auth._SCOPE_ALIASES``, quindi gli assistant legacy
# continuano a funzionare senza una migrazione DB obbligatoria.
_LEGACY_SCOPES: frozenset[str] = frozenset(
    {
        "patient:read",
        "patient:write",
        "patient:images",
        "consultation:read",
        "consultation:write",
        "studies:write_metadata",
        "series:write_metadata",
        "consultations:finalize",
    }
)

_ALLOWED_PERMISSIONS: frozenset[str] = _GRANTABLE_SCOPES | _LEGACY_SCOPES


# Presentation metadata for the UI checkbox renderer. The technical
# description lives on ``ScopeDef`` itself; here we add the user-facing
# label and the category bucket the FE groups by.
_SCOPE_LABELS: dict[str, str] = {
    "patients:read": "Read patient profiles + demographics",
    "patients:write": "Modify patient profiles + demographics",
    "patients:identify": "Add or remove external patient identifiers",
    "events:read": "Read clinical events (visits, procedures, labs)",
    "events:write": "Create non-imaging clinical events",
    "documents:read": "Read document metadata + OCR text",
    "documents:write": "Modify document metadata + content",
    "documents:ingest": "Upload new documents into a fascicolo",
    "documents:download": "Download original document binaries",
    "documents:delete": "Soft-delete + restore documents",
    "documents:merge": "Merge or split document aliases",
    "reports:read": "Read report contents + citations",
    "reports:write": "Extract or update report contents",
    "reports:endorse": "Endorse extracted report contents",
    "synthesis:write": "Draft, supersede, or reject canonical syntheses",
    "provenance:read": "Read artefact lineage (audit explanations)",
    "lookup:external": "Cross-patient lookup by external identifier",
    "annotations:read": "Read in-viewer markers + annotations",
    "annotations:write": "Create, update, or delete annotations",
    "imaging:read": "Read DICOM series, slices, thumbnails",
    "imaging:compute": "Trigger expensive imaging compute (segmentation, registration, embeddings)",
    "tags:read": "Read tags",
    "tags:write": "Modify tags on study, series, or patient",
    "folders:read": "List folders + read folder contents",
    "folders:write": "Create, rename, move, delete folders, add or remove items",
    "search:read": "Full-text + semantic search",
}


def _scope_category(s: ScopeDef) -> str:
    """Bucket a scope into the UI tabs the FE renders.

    ``danger`` → flagged scopes the UI must require explicit opt-in for
    (``patients:identify``, ``documents:download``, ``reports:endorse``,
    ``lookup:external`` — everything ``ScopeDef.sensitive``).
    ``read``   → ``*:read`` and ``provenance:read``-style scopes.
    ``write``  → everything else (creates, mutates, computes).
    """
    if s.sensitive:
        return "danger"
    if s.id.endswith(":read"):
        return "read"
    return "write"


# Catalog returned by ``GET /api/ai-assistants/scope-catalog``. Derived
# from ``SCOPE_CATALOG`` so a new MCP scope automatically surfaces in
# the UI as soon as it lands in ``bvmcp.scopes``. ``synthesis:sign`` is
# filtered out: it is structurally ungrantable to agents (the
# ``/synthesis/sign`` endpoint refuses any agent_token-backed request),
# so showing a checkbox would be misleading.
_SCOPE_CATALOG: list[dict[str, object]] = [
    {
        "key": s.id,
        "category": _scope_category(s),
        "label": _SCOPE_LABELS.get(s.id, s.id),
        "description": s.description,
        "dangerous": s.sensitive,
        "enforced": True,
    }
    for s in SCOPE_CATALOG
    if not s.human_only
]

# Hard ceiling on token lifetime — 30 days. Short-lived enough that a
# leaked credential ages out before most incident-response cycles
# conclude, long enough that interactive agent sessions don't demand
# constant re-issuance.
_MAX_TTL_SECONDS = 30 * 24 * 3600
_DEFAULT_TTL_SECONDS = 7 * 24 * 3600


# ---------- schemas ----------


class AssistantOut(BaseModel):
    id: str
    label: str
    provider: str | None
    model_id: str | None
    notes: str | None
    permissions: list[str]
    deidentify_on_use: bool
    patient_count: int
    # The stable, public identifier of the assistant's OAuth client.
    # Format: ``bvp_agt_<32hex>``. Safe to share — by itself it is not
    # a credential.
    client_id: str
    # First ~8 chars of the latest secret, for human-eyeball
    # identification ("…Z9_kxR3p"). ``None`` if no secret has been
    # minted yet.
    client_secret_prefix: str | None
    # ``True`` once the operator has minted at least one secret. The
    # plaintext is never exposed by GET endpoints.
    has_secret: bool
    # Soft-revocation flag.
    is_active: bool
    # Timestamp of the most recent ``POST /revoke`` (security event).
    # ``None`` for assistants that have never had their secret
    # explicitly revoked. The UI shows "Revocato il …" when set.
    revoked_at: str | None
    created_at: str
    updated_at: str


class AssistantCreatedOut(AssistantOut):
    """Returned exclusively at ``POST /api/ai-assistants`` and
    ``POST /api/ai-assistants/{id}/rotate``. ``client_secret`` is the
    plaintext bearer token; the operator MUST copy it immediately
    because the server only stores its sha256 hash and will never
    reveal it again."""

    client_secret: str


class AssistantCreateIn(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    provider: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    # The cap was 16 when the legacy catalog only had 10 grantable
    # entries; once the unified catalog landed (24 canonical scopes +
    # the 2 folders ones = 26) "Seleziona tutti" sent 26 permissions
    # and the PATCH returned ``Request body failed validation``. Lift
    # the cap to 64 — comfortably above the catalog size today plus
    # legacy aliases the validator still accepts. _validate_permissions
    # remains the real authority: an unknown key is rejected regardless
    # of length.
    permissions: list[str] = Field(min_length=1, max_length=64)
    deidentify_on_use: bool = Field(default=True)


class AssistantUpdateIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    permissions: list[str] | None = Field(default=None, min_length=1, max_length=64)
    deidentify_on_use: bool | None = None
    # Soft revocation toggle. False = bearer-hash matches still come
    # in but the gate refuses to promote the call to typ=agent.
    is_active: bool | None = None


class PatientShareIn(BaseModel):
    patient_id: uuid.UUID


class SharedPatientOut(BaseModel):
    patient_id: str
    display_name: str
    granted_at: str


# ---------- helpers ----------


def _mask_tail(tail: str) -> str:
    return f"xxx...{tail}"


def _validate_permissions(perms: list[str]) -> None:
    unknown = set(perms) - _ALLOWED_PERMISSIONS
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid permissions: {sorted(unknown)}",
        )


async def _owned_assistant(db: AsyncSession, assistant_id: uuid.UUID, user: User) -> AgentAssistant:
    row = (
        await db.execute(select(AgentAssistant).where(AgentAssistant.id == assistant_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="assistant not found")
    if row.owner_subject_id != user.subject_id and not user.is_admin:
        raise HTTPException(status_code=404, detail="assistant not found")
    return row


async def _patient_count(db: AsyncSession, assistant_id: uuid.UUID) -> int:
    rows = (
        await db.execute(
            select(AgentAssistantPatient.patient_id).where(
                AgentAssistantPatient.assistant_id == assistant_id
            )
        )
    ).all()
    return len(rows)


async def _to_out(db: AsyncSession, row: AgentAssistant) -> AssistantOut:
    return AssistantOut(
        id=str(row.id),
        label=row.label,
        provider=row.provider,
        model_id=row.model_id,
        notes=row.notes,
        permissions=list(row.permissions),
        deidentify_on_use=row.deidentify_on_use,
        patient_count=await _patient_count(db, row.id),
        client_id=row.client_id,
        client_secret_prefix=row.client_secret_prefix,
        has_secret=row.client_secret_hash is not None,
        is_active=row.is_active,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


async def _to_created_out(
    db: AsyncSession, row: AgentAssistant, plaintext_secret: str
) -> AssistantCreatedOut:
    base = await _to_out(db, row)
    return AssistantCreatedOut(client_secret=plaintext_secret, **base.model_dump())


# ---------- endpoints ----------


class ScopeCatalogEntry(BaseModel):
    key: str
    category: str
    label: str
    description: str
    dangerous: bool
    enforced: bool


class ConnectorInfoOut(BaseModel):
    """Surfaced on the UI so the operator knows what to paste into
    Claude.ai's custom-connector form."""

    mcp_url: str
    instructions_md: str


@router.get(
    "/connector-info",
    response_model=ConnectorInfoOut,
    summary="MCP connector URL + onboarding hint",
)
async def get_connector_info() -> ConnectorInfoOut:
    """The remote MCP HTTP transport endpoint to register on Claude.ai.

    The frontend pulls this on the assistants page and shows a
    one-click-copy box. The instruction string is markdown-light so
    the UI can render bullet points without ferrying yet another
    translation file.
    """
    from bvphoenix.config import get_settings as _gs

    s = _gs()
    return ConnectorInfoOut(
        mcp_url=s.mcp_public_url.rstrip("/"),
        instructions_md=(
            "1. Open https://claude.ai → Settings → Connectors → Add custom connector.\n"
            f"2. Server URL: `{s.mcp_public_url.rstrip('/')}`\n"
            "3. When asked, log in with your bitvision email and password (this site).\n"
            "4. Approve the requested scopes; the connector is then live."
        ),
    )


@router.get(
    "/scope-catalog",
    response_model=list[ScopeCatalogEntry],
    summary="OAuth scope catalog for AI assistants",
)
async def get_scope_catalog() -> list[ScopeCatalogEntry]:
    """Return the human-readable description of every scope an AI
    assistant can be granted.

    Categories:
    * ``read`` — purely informational scopes; safe defaults.
    * ``write`` — scopes that let the agent mutate stored state. The
      UI should show a confirmation before enabling these by default
      on a new assistant.
    * ``danger`` — privileged actions (e.g. finalising a consultation).
      The UI must require an explicit opt-in step before flipping
      these on.
    """
    return [ScopeCatalogEntry(**entry) for entry in _SCOPE_CATALOG]  # type: ignore[arg-type]


@router.post("", response_model=AssistantCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_assistant(
    body: AssistantCreateIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AssistantCreatedOut:
    """Create an assistant + mint its first machine-to-machine secret.

    The plaintext ``client_secret`` is returned in the response and
    only persisted as ``sha256(plaintext)``. The operator is expected
    to copy + paste it into the AI client (Claude.ai custom connector,
    Cursor, …) immediately. A future call to ``POST /rotate`` issues a
    fresh secret and invalidates the old one.
    """
    _validate_permissions(body.permissions)

    plaintext_secret = _generate_client_secret()
    assistant = AgentAssistant(
        owner_subject_id=user.subject_id,
        label=body.label,
        provider=body.provider,
        model_id=body.model_id,
        notes=body.notes,
        permissions=list(body.permissions),
        deidentify_on_use=body.deidentify_on_use,
        client_id=_generate_client_id(),
        client_secret_hash=_hash_client_secret(plaintext_secret),
        client_secret_prefix=_client_secret_prefix(plaintext_secret),
        is_active=True,
    )
    db.add(assistant)
    await db.commit()
    await db.refresh(assistant)
    return await _to_created_out(db, assistant, plaintext_secret)


@router.get("", response_model=list[AssistantOut])
async def list_assistants(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AssistantOut]:
    rows = (
        (
            await db.execute(
                select(AgentAssistant)
                .where(AgentAssistant.owner_subject_id == user.subject_id)
                .order_by(AgentAssistant.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await _to_out(db, r) for r in rows]


@router.get("/{assistant_id}", response_model=AssistantOut)
async def get_assistant(
    assistant_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AssistantOut:
    row = await _owned_assistant(db, assistant_id, user)
    return await _to_out(db, row)


@router.patch("/{assistant_id}", response_model=AssistantOut)
async def update_assistant(
    assistant_id: uuid.UUID,
    body: AssistantUpdateIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AssistantOut:
    row = await _owned_assistant(db, assistant_id, user)
    if body.permissions is not None:
        _validate_permissions(body.permissions)
        row.permissions = list(body.permissions)
    if body.label is not None:
        row.label = body.label
    if body.provider is not None:
        row.provider = body.provider or None
    if body.model_id is not None:
        row.model_id = body.model_id or None
    if body.notes is not None:
        row.notes = body.notes or None
    if body.deidentify_on_use is not None:
        row.deidentify_on_use = body.deidentify_on_use
    if body.is_active is not None:
        row.is_active = body.is_active
    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return await _to_out(db, row)


@router.delete("/{assistant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assistant(
    assistant_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    row = await _owned_assistant(db, assistant_id, user)
    await db.delete(row)
    await db.commit()


@router.post(
    "/{assistant_id}/rotate",
    response_model=AssistantCreatedOut,
)
async def rotate_secret(
    assistant_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AssistantCreatedOut:
    """Mint a fresh ``client_secret`` and invalidate the old one.

    The new plaintext secret is returned exactly once; the old secret
    is no longer accepted by the MCP gate as soon as the row is
    committed. ``client_id`` is intentionally preserved so AI clients
    only need to update the bearer string in their config, not the
    stable identifier.

    Rotate also clears ``revoked_at`` and re-enables the row: if the
    operator previously hit ``/revoke`` and then issues a fresh
    secret, the assistant comes back online with the new credential.
    """
    row = await _owned_assistant(db, assistant_id, user)
    plaintext_secret = _generate_client_secret()
    row.client_secret_hash = _hash_client_secret(plaintext_secret)
    row.client_secret_prefix = _client_secret_prefix(plaintext_secret)
    row.revoked_at = None
    row.is_active = True
    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return await _to_created_out(db, row, plaintext_secret)


@router.post(
    "/{assistant_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_assistant(
    assistant_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Mark the assistant's secret as compromised.

    Distinct from ``PATCH {is_active: false}`` (which is a soft-pause
    flow used during legitimate maintenance windows): ``/revoke`` is
    a security event that

      * sets ``revoked_at = now()`` for audit / forensics,
      * clears ``client_secret_hash`` and ``client_secret_prefix`` so
        the leaked plaintext can never be re-promoted, even if the
        operator later flips ``is_active`` back to True without
        rotating,
      * sets ``is_active = false`` for backwards compatibility with
        callers that only inspect the legacy flag.

    Idempotent: revoking an already-revoked row is a no-op (200/204
    either way; we re-stamp ``revoked_at`` to keep the most-recent
    incident timestamp).
    """
    row = await _owned_assistant(db, assistant_id, user)
    now = datetime.now(UTC)
    row.client_secret_hash = None
    row.client_secret_prefix = None
    row.is_active = False
    row.revoked_at = now
    row.updated_at = now
    await db.commit()


# ---------- patient sharing ----------


@router.get("/{assistant_id}/patients", response_model=list[SharedPatientOut])
async def list_shared_patients(
    assistant_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SharedPatientOut]:
    await _owned_assistant(db, assistant_id, user)
    rows = (
        await db.execute(
            select(AgentAssistantPatient, Patient)
            .join(Patient, Patient.id == AgentAssistantPatient.patient_id)
            .where(AgentAssistantPatient.assistant_id == assistant_id)
            .order_by(AgentAssistantPatient.granted_at.desc())
        )
    ).all()
    return [
        SharedPatientOut(
            patient_id=str(p.id),
            display_name=p.display_name,
            granted_at=link.granted_at.isoformat(),
        )
        for link, p in rows
    ]


@router.post("/{assistant_id}/patients", response_model=SharedPatientOut)
async def share_patient(
    assistant_id: uuid.UUID,
    body: PatientShareIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SharedPatientOut:
    assistant = await _owned_assistant(db, assistant_id, user)
    patient = (
        await db.execute(select(Patient).where(Patient.id == body.patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    # Caller must have at least read access on the patient — minting an
    # assistant share for someone else's record would be a privilege
    # leak.
    if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
        raise HTTPException(status_code=403, detail="cannot share this patient")

    existing = (
        await db.execute(
            select(AgentAssistantPatient).where(
                AgentAssistantPatient.assistant_id == assistant.id,
                AgentAssistantPatient.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = AgentAssistantPatient(
            assistant_id=assistant.id,
            patient_id=patient.id,
            granted_by_subject_id=user.subject_id,
        )
        db.add(existing)
        await db.commit()
        await db.refresh(existing)
    return SharedPatientOut(
        patient_id=str(patient.id),
        display_name=patient.display_name,
        granted_at=existing.granted_at.isoformat(),
    )


@router.delete(
    "/{assistant_id}/patients/{patient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unshare_patient(
    assistant_id: uuid.UUID,
    patient_id: uuid.UUID,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _owned_assistant(db, assistant_id, user)
    row = (
        await db.execute(
            select(AgentAssistantPatient).where(
                AgentAssistantPatient.assistant_id == assistant_id,
                AgentAssistantPatient.patient_id == patient_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    await db.delete(row)
    await db.commit()
    return None
