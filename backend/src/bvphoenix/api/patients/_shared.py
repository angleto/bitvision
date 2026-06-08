"""Patients API — fascicolo elettronico del paziente.

CRUD on Health Records, structured fascicolo index (inspired by FSE 2.0),
unified timeline, standalone document upload, and patient-level sharing.

"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Text, desc, func, or_, select
from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._dry_run import dry_run_flag
from bvphoenix.api._http import (
    content_disposition as _content_disposition,
)
from bvphoenix.api._http import (
    proxy_s3_object,
)
from bvphoenix.api.sharing import _link_out
from bvphoenix.auth import (
    enforce_agent_patient_scope,
    enforce_agent_scope,
    hash_password,
    optional_user,
    require_user,
)
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    Document,
    DocumentFile,
    DocumentStudyLink,
    Folder,
    FolderItem,
    Grant,
    ImagingStudy,
    Instance,
    Marker,
    Patient,
    Series,
    ShareLink,
    Tag,
    User,
)
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.middleware.idempotency import IdempotencyContext, idempotent
from bvphoenix.middleware.problem_details import problem
from bvphoenix.services.access_levels import level_to_permissions
from bvphoenix.services.document_catalog_validation import (
    load_active_catalog_ids,
    translate_catalog_fk_violation,
    validate_kind_id,
)
from bvphoenix.services.document_mappers import (
    document_versioning_payload as _document_versioning_payload,
)
from bvphoenix.services.document_thumbnails import (
    UnsupportedThumbnailKindError,
    is_supported_thumbnail_mime,
    render_document_thumbnail,
)
from bvphoenix.services.documents.link_kind import (
    CANONICAL_KINDS as _DOCUMENT_STUDY_LINK_KINDS,
)
from bvphoenix.services.documents.link_kind import coerce_link_kind
from bvphoenix.services.etag import etag_for_branch, format_etag, parse_if_match, require_if_match
from bvphoenix.services.permissions import (
    DELETE,
    READ_METADATA,
    can_patient,
    effective_permissions_on_patient,
    platform_owner_subject_id,
    visible_patients_filter,
)
from bvphoenix.services.publish import publish_patient_to_opendata
from bvphoenix.services.upload_validation import validate_mime, validate_size
from bvphoenix.services.versioning import ActorContext
from bvphoenix.services.versioning_hooks import (
    record_versioned_change,
    seed_patient_main,
)
from bvphoenix.storage import get_s3_storage

_log = logging.getLogger(__name__)


router = APIRouter(tags=["patients"])


class PatientContact(BaseModel):
    """Additional contact for a patient (family member, caregiver, GP, ...).

    A contact is purely informational by default — it appears in the
    fascicolo header so the clinician can reach the right person. A
    contact can also be *promoted to a delegate* via
    ``POST /patients/{id}/contacts/{idx}/delegate``, which creates a
    Grant+ShareLink scoped to the patient resource and stores the
    resulting IDs on this row. From that moment on the contact has
    real access to the fascicolo (view/edit/manage depending on the
    chosen level) until ``DELETE /.../delegate`` revokes it.
    """

    # Stable opaque ID assigned on first write. Lets the UI address
    # a specific contact regardless of list reordering / additions.
    # Older rows persisted before this field was introduced have no
    # id — endpoints that need one assign one on the fly.
    id: str | None = Field(default=None, max_length=64)
    label: str = Field(min_length=1, max_length=120, description="Display name")
    relationship: str | None = Field(
        default=None,
        max_length=80,
        description=(
            "Free-text relation: 'figlio', 'moglie', 'MMG', 'caregiver'. "
            "The frontend suggests a localised vocabulary but accepts any string."
        ),
    )
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(
        default=None,
        description="Free-text per-contact note (e.g. 'best to call after 18:00').",
    )
    is_primary: bool = Field(
        default=False,
        description=(
            "Marks the default primary contact for the patient. The "
            "DB enforces at most one primary per patient via a partial "
            "unique index — promoting a different contact to primary "
            "demotes the previous one in the same transaction."
        ),
    )
    consent_to_contact: bool = Field(
        default=False,
        description=(
            "Explicit GDPR consent: the contact has agreed to be "
            "contacted by the clinic on the patient's behalf. Default "
            "False; toggle requires an explicit user action."
        ),
    )
    # ---- Delegation state (set by /delegate, cleared by DELETE /delegate) ----
    delegation_subject_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Subject UUID of the user that received the delegation. "
            "``None`` means this contact is purely informational."
        ),
    )
    delegation_share_link_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "ShareLink UUID that materialises the delegation. The link "
            "carries the magic-link token + password the contact uses "
            "to claim their account."
        ),
    )
    delegation_level: str | None = Field(
        default=None,
        max_length=16,
        description="Delegation level: 'viewer' | 'editor' | 'manager'.",
    )


class PatientOut(BaseModel):
    id: str
    display_name: str
    external_id: str | None
    birth_date: str | None
    sex: str | None
    tax_id: str | None
    phone: str | None
    email: str | None
    address: str | None
    blood_type: str | None
    birth_place_city: str | None = None
    birth_place_province: str | None = None
    asl_code: str | None = None
    asl_name: str | None = None
    allergies: str | None
    notes: str | None
    # Provenance pair for the ``notes`` field — set only when notes
    # change, so the FE can render "edited by X · Y ago" without
    # conflating it with a demographics-only PATCH. NULL on rows
    # whose notes haven't been edited since migration 0094 landed.
    notes_updated_at: str | None = None
    notes_updated_by_display_name: str | None = None
    contacts: list[PatientContact] = Field(default_factory=list)
    managed_by_subject_id: str | None
    self_user_subject_id: str | None
    # Computed origin from the caller's perspective. ``mine`` if the
    # caller manages or *is* the patient; ``public`` for open-data
    # patients owned by the platform-owner subject; ``shared`` for
    # everything else visible (i.e. surfaced via a Grant). ``None``
    # for unauthenticated reads (won't happen in practice since the
    # list endpoint requires auth, but kept conservative).
    origin: str | None = None
    created_at: str
    # Mirrors the ``ETag`` response header on PATCH. Hex of the latest
    # versioning commit on the patient's ``main`` branch (ADR 0001).
    # ``None`` for anonymous reads or rows predating the chain.
    etag: str | None = None


class PatientCreateIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    external_id: str | None = Field(default=None, max_length=128)
    birth_date: date | None = None
    sex: str | None = Field(default=None, max_length=1)
    tax_id: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = None
    blood_type: str | None = Field(default=None, max_length=8)
    birth_place_city: str | None = Field(default=None, max_length=128)
    birth_place_province: str | None = Field(default=None, max_length=8)
    asl_code: str | None = Field(default=None, max_length=16)
    asl_name: str | None = Field(default=None, max_length=255)
    allergies: str | None = None
    notes: str | None = None
    contacts: list[PatientContact] = Field(default_factory=list, max_length=20)


class PatientUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    external_id: str | None = None
    birth_date: date | None = None
    sex: str | None = Field(default=None, max_length=1)
    tax_id: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = None
    blood_type: str | None = Field(default=None, max_length=8)
    birth_place_city: str | None = Field(default=None, max_length=128)
    birth_place_province: str | None = Field(default=None, max_length=8)
    asl_code: str | None = Field(default=None, max_length=16)
    asl_name: str | None = Field(default=None, max_length=255)
    allergies: str | None = None
    notes: str | None = None
    contacts: list[PatientContact] | None = Field(default=None, max_length=20)


class PaginatedPatients(BaseModel):
    items: list[PatientOut]
    total: int
    limit: int
    offset: int


class PatientDocumentFileOut(BaseModel):
    id: str
    sequence: int
    file_content_type: str | None
    original_filename: str | None
    size_bytes: int | None
    created_at: str


class PatientDocumentOut(BaseModel):
    id: str
    patient_id: str
    uploaded_by_subject_id: str | None
    # v3: ``document_type`` is kept as an alias of ``kind_id`` for the
    # legacy frontend slice that still reads it; new clients should
    # prefer the explicit 3-axis fields below. They map to the catalog
    # tables (document_kinds / document_provenances /
    # document_authorities) via FK.
    document_type: str
    kind_id: str
    provenance_id: str
    authority_id: str
    content_sha256: str | None = None
    original_blob_hash: str | None = None
    title: str
    text: str | None
    file_s3_key: str | None
    file_content_type: str | None
    document_date: str | None
    created_at: str
    files: list[PatientDocumentFileOut] = []
    # ETag for optimistic concurrency on PATCH. v3: lives directly on
    # ``documents.etag`` (per-row UUID rotated on each update).
    etag: str | None = None
    # 0088+: number of folders containing this document (hardlink count).
    # Always ≥ 1 for live documents (no-orphan invariant). The FE
    # surfaces a chain-link badge on the card when the count is ≥ 2 so
    # the user understands the same document is reachable from multiple
    # folders (avoids "this looks duplicate" confusion). NOT part of
    # the document etag — it is derived from folder_items.
    folder_count: int = 1
    # True iff the only folder containment is the patient root. The FE
    # uses this to slightly demote sorting when offering "filable"
    # documents to the user.
    is_in_root_only: bool = False


class TimelineItem(BaseModel):
    type: str  # study | report | annotation | document
    date: str
    data: dict


class FascicoloSection(BaseModel):
    key: str
    label: str
    count: int
    last_date: str | None
    breakdown: dict[str, int] | None


class FascicoloIndex(BaseModel):
    patient: PatientOut
    sections: list[FascicoloSection]
    total_items: int


class ShareTarget(BaseModel):
    kind: str = Field(description="link_public | link_org | email | org")
    email: str | None = None
    org_subject_id: str | None = None


class ShareCreateIn(BaseModel):
    access_level: str = Field(default="viewer", description="viewer | editor | manager")
    download: bool = Field(default=False)
    target: ShareTarget = Field(default_factory=lambda: ShareTarget(kind="link_public"))
    expires_in_hours: int | None = Field(default=24 * 7)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    # Server-generated password (24 chars, ~118 bits). Mutually
    # exclusive with ``password``. Plaintext returned ONCE on POST in
    # ``generated_password``; mirrors the studies share endpoint.
    autogen_password: bool = Field(default=False)
    label: str | None = Field(default=None, max_length=255)
    max_uses: int | None = Field(default=None, ge=1)
    # Delivery mode for link shares. ``claim`` (default) is the
    # claimable-account flow; ``anonymous`` makes the link itself the
    # credential (treat as API key, every commit attributed via
    # ActorContext.kind='link'). Anonymous mode requires a recipient.
    mode: str = Field(default="claim", pattern="^(claim|anonymous)$")
    recipient_name: str | None = Field(default=None, max_length=255)
    recipient_email: str | None = Field(default=None, max_length=255)
    recipient_phone: str | None = Field(default=None, max_length=64)


class ShareLinkOut(BaseModel):
    id: str
    token: str
    url: str
    label: str | None
    permissions: list[str]
    expires_at: str | None
    revoked: bool
    use_count: int
    max_uses: int | None
    requires_password: bool
    created_at: str
    mode: str = "claim"
    recipient_name: str | None = None
    recipient_email: str | None = None
    recipient_phone: str | None = None
    # Plaintext autogen password — returned only on POST and never
    # again on any GET / PATCH path.
    generated_password: str | None = None


def _patient_origin(p: Patient, user: User | None) -> str | None:
    """Compute the caller's view of how this patient surfaced.

    ``mine`` — the caller manages the record or it represents the caller.
    ``public`` — owned by the platform-owner subject (open-data datasets
    like Fruits show up here). ``shared`` — everything else the caller
    can see (i.e. via an active Grant or principal-set membership).
    """
    if user is None:
        return None
    if p.managed_by_subject_id == user.subject_id or p.self_user_subject_id == user.subject_id:
        return "mine"
    if p.managed_by_subject_id == platform_owner_subject_id():
        return "public"
    return "shared"


async def _resolve_notes_editor_name(db: AsyncSession, subject_id: uuid.UUID | None) -> str | None:
    """Look up the display name for the subject that last edited
    ``patient.notes``. Returns ``None`` for legacy rows without a
    stamped editor (NULL ``notes_updated_by_subject_id``) and on
    misses (subject hard-deleted) so the FE can render the
    timestamp alone when the name is unknown.
    """
    if subject_id is None:
        return None
    from bvphoenix.db.models.principals import Subject

    row = (
        await db.execute(select(Subject.display_name).where(Subject.id == subject_id))
    ).scalar_one_or_none()
    return str(row) if row else None


def _patient_out(
    p: Patient,
    *,
    user: User | None = None,
    contacts: list[PatientContact] | None = None,
    notes_editor_display_name: str | None = None,
) -> PatientOut:
    """Project a Patient row to the API shape.

    v3: the legacy ``patients.contacts`` JSONB column was dropped in
    migration 0076; contacts now live exclusively in the relational
    ``patient_contacts`` table. The caller must preload the list (or
    pass an empty list); the function no longer falls back to a
    JSONB stash.

    v3: ``patients.tax_id`` and ``patients.external_id`` were also
    dropped (UUID is the only key, business identifiers live inside
    ``external_identifiers``). The legacy ``tax_id`` / ``external_id``
    response fields are derived from the JSONB array for back-compat
    with the existing frontend slice.
    """
    if contacts is None:
        contacts = []

    # Derive legacy tax_id + external_id from external_identifiers for
    # the back-compat fields on PatientOut. The frontend reads them as
    # plain strings; the v3 surface is :class:`ExternalIdentifier` via
    # the dedicated /external-identifiers endpoint.
    tax_id_legacy: str | None = None
    external_id_legacy: str | None = None
    for entry in p.external_identifiers or []:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        evalue = entry.get("value")
        if not isinstance(evalue, str):
            continue
        if etype == "fiscal-code" and tax_id_legacy is None:
            tax_id_legacy = evalue
        if etype == "MR" and external_id_legacy is None:
            external_id_legacy = evalue

    return PatientOut(
        id=str(p.id),
        display_name=p.display_name,
        external_id=external_id_legacy,
        birth_date=str(p.birth_date) if p.birth_date else None,
        sex=p.sex,
        tax_id=tax_id_legacy,
        phone=p.phone,
        email=p.email,
        address=p.address,
        blood_type=p.blood_type,
        birth_place_city=p.birth_place_city,
        birth_place_province=p.birth_place_province,
        asl_code=p.asl_code,
        asl_name=p.asl_name,
        allergies=p.allergies,
        notes=p.notes,
        notes_updated_at=p.notes_updated_at.isoformat() if p.notes_updated_at else None,
        notes_updated_by_display_name=notes_editor_display_name,
        contacts=contacts,
        managed_by_subject_id=str(p.managed_by_subject_id) if p.managed_by_subject_id else None,
        self_user_subject_id=str(p.self_user_subject_id) if p.self_user_subject_id else None,
        origin=_patient_origin(p, user),
        created_at=p.created_at.isoformat(),
    )


def _doc_out(
    d: Document,
    *,
    files: list[DocumentFile] | None = None,
    folder_count: int | None = None,
    is_in_root_only: bool | None = None,
) -> PatientDocumentOut:
    return PatientDocumentOut(
        id=str(d.id),
        patient_id=str(d.patient_id),
        uploaded_by_subject_id=str(d.uploaded_by_subject_id) if d.uploaded_by_subject_id else None,
        # v3: kind_id is the primary; document_type stays as an alias
        # so the existing frontend slice that reads ``document_type``
        # keeps working until phase 4 polish removes the alias.
        document_type=d.kind_id,
        kind_id=d.kind_id,
        provenance_id=d.provenance_id,
        authority_id=d.authority_id,
        content_sha256=d.content_sha256,
        original_blob_hash=d.original_blob_hash,
        title=d.title,
        text=d.text,
        file_s3_key=d.file_s3_key,
        file_content_type=d.file_content_type,
        document_date=str(d.document_date) if d.document_date else None,
        created_at=d.created_at.isoformat(),
        etag=str(d.etag),
        folder_count=folder_count if folder_count is not None else 1,
        is_in_root_only=is_in_root_only if is_in_root_only is not None else False,
        files=[
            PatientDocumentFileOut(
                id=str(f.id),
                sequence=f.sequence,
                file_content_type=f.file_content_type,
                original_filename=f.original_filename,
                size_bytes=f.size_bytes,
                created_at=f.created_at.isoformat(),
            )
            for f in (files or [])
        ],
    )


_PWD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789abcdefghjkmnpqrstuvwxyz"


def _autogen_share_password(length: int = 24) -> str:
    return "".join(secrets.choice(_PWD_ALPHABET) for _ in range(length))


async def _get_patient_or_404(
    db: AsyncSession,
    patient_id: uuid.UUID,
    user: User | None,
    request: Request,
    action: str = READ_METADATA,
) -> Patient:
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    # Refuse cross-patient access attempted via an agent token whose
    # scope is bound to a different patient. Runs before the human-
    # permission gate so a leaked token cannot enumerate reachable
    # fascicoli via error-code timing.
    enforce_agent_patient_scope(request, patient.id)
    if not await can_patient(db, user=user, action=action, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")
    return patient


_PATIENT_SCOPES = ("personal", "mine", "shared", "public", "all")


async def _load_patient_contacts(db: AsyncSession, patient_id: uuid.UUID) -> list[PatientContact]:
    """Pull the patient's contacts from the dedicated table and project
    each row to the public ``PatientContact`` Pydantic shape. Used by
    every endpoint that needs to surface contacts on a ``PatientOut``
    (both reads and post-write responses)."""
    from datetime import UTC, datetime

    from sqlalchemy import or_, select

    from bvphoenix.db.models import Grant
    from bvphoenix.services import patient_contacts as svc

    rows = await svc.list_contacts(db, patient_id)

    # A contact carries delegation pointers (``delegation_grant_id`` etc.)
    # even after its grant is revoked or expired — the pointers are only
    # cleared on an explicit revoke. Surfacing them verbatim makes the
    # contacts tab show a "Collaboratore" chip for access that ``/shares``
    # (active-only) correctly hides, and lets a dead delegation read as
    # live. So we project the delegation fields ONLY when the backing
    # grant is still active; otherwise the contact falls back to its
    # informational-only shape.
    grant_ids = [r.delegation_grant_id for r in rows if r.delegation_grant_id is not None]
    live_grants: set[uuid.UUID] = set()
    if grant_ids:
        now = datetime.now(UTC)
        live_grants = set(
            (
                await db.execute(
                    select(Grant.id).where(
                        Grant.id.in_(grant_ids),
                        Grant.revoked_at.is_(None),
                        Grant.valid_from <= now,
                        or_(Grant.valid_until.is_(None), Grant.valid_until >= now),
                    )
                )
            )
            .scalars()
            .all()
        )

    out: list[PatientContact] = []
    for row in rows:
        data = svc.to_pydantic_dict(row)
        if row.delegation_grant_id is None or row.delegation_grant_id not in live_grants:
            data["delegation_subject_id"] = None
            data["delegation_share_link_id"] = None
            data["delegation_level"] = None
        try:
            out.append(PatientContact(**data))
        except Exception:
            continue
    return out


def _seed_contact_ids(contacts: list[dict]) -> list[dict]:
    """Assign a stable UUID to any contact entry that lacks one.

    The delegation endpoints address contacts by ``id`` rather than
    list index so the UI can survive reordering / inserts without
    pointing the wrong delegation at the wrong person. Older rows
    persisted before this field existed have ``id == None``; we lazily
    fix them up on the next write so the fascicolo never carries an
    ambiguous contact.
    """
    from bvphoenix.services.patient_delegation import assign_missing_contact_ids

    contacts = [dict(c) for c in contacts]
    assign_missing_contact_ids(contacts)
    return contacts


def _patient_versioning_payload(p: Patient) -> dict:
    """Snapshot of the clinically relevant patient fields for F12.

    Server timestamps are excluded so an idempotent edit (same values
    re-saved) dedupes to the same ``object_hash`` and produces no
    pointless commit churn.
    """
    return {
        "id": str(p.id),
        "display_name": p.display_name,
        "birth_date": p.birth_date.isoformat() if p.birth_date else None,
        "sex": p.sex,
        "phone": p.phone,
        "email": p.email,
        "address": p.address,
        "blood_type": p.blood_type,
        "birth_place_city": p.birth_place_city,
        "birth_place_province": p.birth_place_province,
        "asl_code": p.asl_code,
        "asl_name": p.asl_name,
        "allergies": p.allergies,
        "notes": p.notes,
        # v3: tax_id / external_id / contacts JSONB columns dropped.
        # The full identity surface lives in ``external_identifiers``
        # JSONB; the relational ``patient_contacts`` table holds
        # the contacts. Both are versioned by their own commits.
        "external_identifiers": list(p.external_identifiers or []),
        "schema_version": 3,
    }


def _patient_diff(p: Patient, fields: dict) -> dict[str, dict[str, object | None]]:
    """``{field: {"before": x, "after": y}}`` for a requested update.

    Used by ``?dry_run=true`` to surface what *would* change without
    committing. Entries where before == after are dropped so a no-op
    edit returns an empty diff.
    """
    out: dict[str, dict[str, object | None]] = {}
    for k, v in fields.items():
        before = getattr(p, k, None)
        if k == "birth_date" and before is not None and hasattr(before, "isoformat"):
            before = before.isoformat()
        after = v
        if k == "birth_date" and after is not None and hasattr(after, "isoformat"):
            after = after.isoformat()
        if before == after:
            continue
        out[k] = {"before": before, "after": after}
    return out


class CFDecodeOut(BaseModel):
    """Read-only decode + consistency report for a codice fiscale."""

    cf: str
    valid_syntax: bool
    decoded: dict | None
    warnings: list[dict]


class ContactCreateIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    relationship: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    notes: str | None = None
    is_primary: bool = False
    consent_to_contact: bool = False


class ContactUpdateIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    relationship: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    notes: str | None = None
    is_primary: bool | None = None
    consent_to_contact: bool | None = None


class ContactDelegateIn(BaseModel):
    """Request body for ``POST /patients/{id}/contacts/{contact_id}/delegate``."""

    access_level: str = Field(
        default="viewer",
        pattern="^(viewer|editor|manager)$",
        description="Permission tier the contact will receive on the fascicolo.",
    )
    expires_in_hours: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional hard expiry. ``None`` (the default) makes the "
            "delegation permanent — typical for family members who "
            "manage records long-term."
        ),
    )
    autogen_password: bool = Field(
        default=True,
        description=(
            "When true the server mints a high-entropy one-time password "
            "and returns it on the response. The plaintext is never "
            "stored — capture it once and deliver it OOB to the contact."
        ),
    )
    password: str | None = Field(
        default=None,
        min_length=8,
        max_length=256,
        description=(
            "Optional explicit password. Mutually exclusive with "
            "``autogen_password``. Use only when the operator wants to "
            "deliver a memorable phrase to a low-tech recipient."
        ),
    )


class ContactDelegateOut(BaseModel):
    """Response body — surfaces everything the operator needs to deliver
    the magic link to the recipient out of band (email, SMS, in person).
    """

    contact_id: str
    delegation_subject_id: str
    delegation_share_link_id: str
    delegation_share_link_token: str
    delegation_level: str
    expires_at: str | None
    # Plaintext password — returned ONCE (autogen path) and never
    # again. The frontend must surface it with a "copy" affordance and
    # a clear "this won't be shown again" warning.
    generated_password: str | None
    # Convenience for the operator: the URL to send the recipient.
    # Built off the same origin as the request so it works in both
    # local dev and production without extra config.
    share_url: str


class PatientDocumentUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    # ``document_type`` is the legacy single-axis name kept here as an
    # alias of ``kind_id`` because the v2 frontend slice and several
    # MCP tools still use it. Either field is accepted on the wire;
    # callers SHOULD prefer ``kind_id`` (the actual catalog FK column
    # since migration 0075). When both are sent ``kind_id`` wins.
    document_type: str | None = Field(default=None, max_length=64)
    kind_id: str | None = Field(default=None, max_length=64)
    document_date: date | None = None
    text: str | None = None


def _document_diff(doc: Document, fields: dict) -> dict[str, dict[str, object | None]]:
    """Return ``{field: {"before": x, "after": y}}`` for the requested edit.

    Used by ``?dry_run=true`` to surface what *would* change without
    committing. Same payload shape as the audit metadata so an agent
    can confirm the change before apply.
    """
    out: dict[str, dict[str, object | None]] = {}
    for k, v in fields.items():
        if k == "text" and v == "":
            v = None
        if k == "document_date" and v is not None:
            v = v.isoformat() if hasattr(v, "isoformat") else str(v)
        before = getattr(doc, k, None)
        if k == "document_date" and before is not None:
            before = before.isoformat()
        if before == v:
            continue
        out[k] = {"before": before, "after": v}
    return out


_DEFAULT_PURGE_AFTER_DAYS = 30


class DeleteDocumentIn(BaseModel):
    reason: str | None = Field(default=None, max_length=255)
    # ``hard=True`` skips the soft-delete tombstone and purges the row
    # immediately. Restricted to admins so an overzealous agent can't
    # vaporise patient data.
    hard: bool = False


class DocumentMergeIn(BaseModel):
    duplicate_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    preserve_files_as_attachments: bool = True
    reason: str | None = Field(default=None, max_length=255)


class DocumentMergeOut(BaseModel):
    primary_id: str
    duplicate_ids: list[str]
    files_transferred: list[dict]
    files_orphaned: list[str]
    etag: str | None = None


class DocumentVersionOut(BaseModel):
    commit_hash: str
    parent_hashes: list[str]
    author_subject_id: str | None
    author_kind: str
    author_display_name: str | None
    model_id: str | None
    provider: str | None
    agent_token_id: str | None
    branch_at_creation: str | None
    message: str
    created_at: str
    is_delete: bool


class DocumentVersionsOut(BaseModel):
    document_id: str
    head_etag: str | None
    versions: list[DocumentVersionOut]


_BULK_UPDATE_HARD_CAP = 100


_BULK_UPDATE_ASYNC_THRESHOLD = 50


class BulkDocumentUpdateItem(BaseModel):
    document_id: uuid.UUID
    title: str | None = Field(default=None, min_length=1, max_length=255)
    # Legacy single-axis alias of ``kind_id``; accepted on the wire,
    # collapsed onto ``kind_id`` before apply (the column the field
    # actually points at since 0075). ``kind_id`` wins on collision.
    document_type: str | None = Field(default=None, max_length=64)
    kind_id: str | None = Field(default=None, max_length=64)
    document_date: date | None = None
    text: str | None = None
    etag: str | None = Field(
        default=None,
        description=(
            "Optional per-document ETag — the same UUID returned by "
            "``GET /patients/{pid}/documents/{did}`` and by the bundle. "
            "Aligned with ``PATCH /patients/{pid}/documents/{did}``: "
            "supply the current value to opt into optimistic concurrency "
            '(412 on stale), pass ``"*"`` to opt out. Pre-2026-05-03 '
            "this field was compared against the patient main-branch "
            "commit hash, which had different granularity than the "
            "single-PATCH path; the asymmetry is now fixed."
        ),
    )


class BulkDocumentUpdateIn(BaseModel):
    items: list[BulkDocumentUpdateItem] = Field(default_factory=list)
    atomic: bool = Field(
        default=False,
        description=(
            "When true, a single failure rolls back the whole manifest "
            "(ADR 0003). When false (default), each item is applied "
            "independently and a per-item outcome is reported."
        ),
    )


class BulkDocumentUpdateItemOut(BaseModel):
    document_id: str
    status: str
    diff: dict | None = None
    etag: str | None = None
    error: dict | None = None


class BulkDocumentUpdateOut(BaseModel):
    items: list[BulkDocumentUpdateItemOut]
    n_ok: int
    n_error: int
    n_dry_run: int
    head_etag: str | None = None
    job_id: str | None = None


class DocumentStudyLinkIn(BaseModel):
    study_id: uuid.UUID
    # Default to ``primary_report`` (canonical name post-0089). Legacy
    # callers sending ``report_of`` are auto-translated by
    # ``coerce_link_kind`` before validation.
    link_kind: str = Field(default="primary_report", max_length=32)


class DocumentStudyLinkOut(BaseModel):
    id: str
    document_id: str
    study_id: str
    link_kind: str
    created_at: str


class StudyDocumentLinkOut(BaseModel):
    """Forward-direction view: a document linked to *this* study.

    Carries the document fields the GUI needs to render the row
    without a second round-trip (title, kind, document_date), plus
    the link's own metadata (link_kind, created_at).
    """

    document_id: str
    document_title: str
    document_kind: str
    document_date: str | None
    document_text_preview: str | None
    has_attachment: bool
    link_kind: str
    created_at: str
    created_by_subject_id: str | None = None


class StudyRefOut(BaseModel):
    study_id: str
    link_kind: str
    created_at: str
    created_by_subject_id: str | None = None


class ContentRefOut(BaseModel):
    report_content_id: str
    role: str
    excerpt: str | None = None
    clinical_event_id: str | None = None


class CitationRefOut(BaseModel):
    report_content_id: str
    citation_id: str
    page: int | None = None
    excerpt: str | None = None


class FolderMembershipOut(BaseModel):
    folder_id: str
    name: str
    is_root: bool


class DocumentReferencesOut(BaseModel):
    """Reverse-direction inventory of everything that references a
    document. Powers the "Riferito da" panel on the document detail
    page and the FE conflict resolver when the user tries to delete a
    document with active references (the same payload is also embedded
    in the 409 ``blocking_references`` from the delete route, but this
    endpoint is the read-only entry point that surfaces *all*
    references including the non-blocking ``mentions`` ones)."""

    studies: list[StudyRefOut]
    report_contents: list[ContentRefOut]
    citations: list[CitationRefOut]
    folders: list[FolderMembershipOut]


_INLINE_TEXT_MAX_BYTES = 100 * 1024


class LabPointOut(BaseModel):
    document_id: str
    document_date: str | None
    text: str
    analyte: str
    value: float
    unit: str
    confidence: float


class LabTrendOut(BaseModel):
    direction: str  # 'up' | 'down' | 'stable' | 'unknown'
    delta: float | None
    rel_delta_pct: float | None
    earliest_iso: str | None
    latest_iso: str | None


class LabTimeseriesOut(BaseModel):
    patient_id: str
    analyte: str
    unit: str | None
    points: list[LabPointOut]
    trend: LabTrendOut


def _normalise_analyte(s: str) -> str:
    import re as _re

    return _re.sub(r"\s+", " ", s.strip().lower())


class DocumentEntitiesOut(BaseModel):
    document_id: str
    extractor_version: str
    extracted_at: str | None = None
    entities_proposed: dict
    entities_validated: dict
    cached: bool = True


class DocumentEntitiesRunIn(BaseModel):
    force: bool = False
    inline: bool = True


class DocumentTextOut(BaseModel):
    document_id: str
    file_id: str | None
    text: str
    engine: str
    engine_version: str
    sha256: str
    page_count: int | None = None
    bbox_words: list | None = None
    cached: bool = True


class DocumentTextRunIn(BaseModel):
    file_id: uuid.UUID | None = None
    force: bool = Field(
        default=False,
        description=(
            "Re-run OCR even if a cache entry exists for the current "
            "engine version. Useful after bumping the engine."
        ),
    )
    inline: bool = Field(
        default=True,
        description=(
            "When true (default), small files run synchronously and the "
            "extracted text is returned in the response. When false the "
            "request always enqueues an Arq job."
        ),
    )
    language: str | None = Field(
        default=None,
        max_length=64,
        description=(
            'Tesseract language tag. ``None`` / ``"auto"`` (default) loads '
            "all languages configured via ``BVP_OCR_LANGUAGES`` "
            "(``ita+eng+deu+fra`` out of the box) and lets Tesseract pick "
            "per region — works for mixed-language clinical scans without "
            "the caller knowing the language. Force a single tag "
            "(``ita``, ``eng``, ``deu``, ``fra``) when the document "
            "language is known: faster and slightly more accurate. Custom "
            "``+``-joined subsets accepted (``ita+eng``)."
        ),
    )


async def _enqueue_ocr_async(
    *,
    db: AsyncSession,
    user: User,
    doc: Document,
    target_file_id: uuid.UUID | None,
    body: DocumentTextRunIn,
    idem: IdempotencyContext,
    settings: object,
) -> DocumentTextOut:
    """Hand the OCR run to the Arq worker and return a 202 + ``X-Job-Id``.

    Reused both as the explicit ``inline=false`` branch and as the
    sync-fallback branch that activates when the inline pipeline raises
    ``RuntimeError``. Centralising the enqueue keeps the idempotency
    key, job dedup, and Redis enqueue error handling in one place.
    """
    from arq import create_pool

    from bvphoenix.services.arq_redis import redis_settings
    from bvphoenix.services.jobs import (
        JobCapExceededError,
        enqueue_or_get,
        mark_failed,
        set_arq_job_id,
    )

    canonical_input = {
        "document_id": str(doc.id),
        "file_id": str(target_file_id) if target_file_id else None,
        "force": bool(body.force),
        "language": body.language,
    }
    try:
        result = await enqueue_or_get(
            db,
            kind="run_document_ocr",
            owner_subject_id=user.subject_id,
            canonical_input=canonical_input,
            scope_ids=[str(doc.id)],
        )
    except JobCapExceededError as exc:
        raise problem(
            429,
            "rate_limited",
            str(exc),
            extra={"retry_after_seconds": exc.retry_after_seconds},
        ) from exc
    await db.commit()

    if not result.deduped:
        try:
            redis = await create_pool(redis_settings(settings.redis_url))  # type: ignore[attr-defined]
            arq_handle = await redis.enqueue_job("run_document_ocr", str(result.job.id))
            await redis.close()
            if arq_handle is not None:
                await set_arq_job_id(db, result.job.id, arq_handle.job_id)
                await db.commit()
        except Exception as exc:
            await mark_failed(
                db,
                result.job.id,
                error={"code": "enqueue_failed", "message": str(exc)},
            )
            await db.commit()
            raise problem(
                503,
                "service_unavailable",
                "failed to enqueue OCR worker job",
            ) from exc

    # 202: agent polls /api/jobs/:id for completion.
    return idem.capture(  # type: ignore[return-value]
        DocumentTextOut(
            document_id=str(doc.id),
            file_id=str(target_file_id) if target_file_id else None,
            text="",
            engine="pending",
            engine_version="pending",
            sha256="",
            cached=False,
        ).model_dump(),
        status_code=202,
        extra_headers={"X-Job-Id": str(result.job.id)},
    )


class DocumentBinaryUrlOut(BaseModel):
    document_id: str
    file_id: str | None
    url: str
    content_type: str
    size_bytes: int | None = None


def _document_binary_target(
    doc: Document,
    file_id: uuid.UUID | None,
    files: list[DocumentFile],
) -> tuple[str, str | None, uuid.UUID | None, int | None]:
    """Resolve the storage key + content type for a binary read.

    Raises ``problem(404|422)`` on missing file / text-only document.
    Returns ``(storage_key, content_type, file_id, size_bytes)``.
    """
    if file_id is not None:
        for f in files:
            if f.id == file_id:
                if not f.file_s3_key:
                    raise problem(422, "no_binary_payload", "document has no binary file")
                return f.file_s3_key, f.file_content_type, f.id, f.size_bytes
        raise problem(404, "not_found", "document file not found")
    if doc.file_s3_key:
        return doc.file_s3_key, doc.file_content_type, None, None
    f = next(iter(sorted(files, key=lambda x: x.sequence)), None)
    if f is None or not f.file_s3_key:
        raise problem(422, "no_binary_payload", "document has no binary file")
    return f.file_s3_key, f.file_content_type, f.id, f.size_bytes


async def _read_s3_prefix(storage, bucket: str, key: str, length: int) -> bytes | None:
    """Fetch at most ``length`` bytes from the start of an S3 object.

    Returns ``None`` on any S3 error so the caller falls back to a
    presigned redirect instead of surfacing the error to the browser.
    """
    try:
        return await asyncio.to_thread(
            storage.get_object_range, bucket=bucket, key=key, start=0, length=length
        )
    except Exception:
        return None


SEARCH_SECTIONS: tuple[str, ...] = (
    "studies",
    "reports",
    "annotations",
    "documents",
    "consultations",
    "folders",
)


_SEMANTIC_FALLBACK_THRESHOLD = 10


class PatientSearchItem(BaseModel):
    section: str
    id: str
    title: str
    preview: str | None
    rank: float
    created_at: str


class PatientSearchOut(BaseModel):
    patient_id: str
    query: str
    total: int
    by_section: dict[str, int]
    items: list[PatientSearchItem]


def _preview(source: str | None, limit: int = 240) -> str | None:
    """Trim a long text field to a short preview for the UI."""
    if not source:
        return None
    s = source.strip()
    if len(s) <= limit:
        return s
    return s[:limit].rsplit(" ", 1)[0] + "…"


def _parse_sections(raw: str | None) -> list[str]:
    if not raw:
        return list(SEARCH_SECTIONS)
    wanted = [s.strip() for s in raw.split(",") if s.strip()]
    return [s for s in wanted if s in SEARCH_SECTIONS] or list(SEARCH_SECTIONS)


class PublishOut(BaseModel):
    public_patient_id: str
    public_main_commit: str
    cloned_clinical_notes: int
    redaction_count: int


class PublishIn(BaseModel):
    pseudonym: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "Display name for the public clone. Default: an opaque "
            "'OpenData Patient <hash>' derived from the new patient id."
        ),
    )
    use_llm_scrub: bool = Field(
        default=False,
        description=(
            "When true, run the LLM-based de-id pass (proper nouns / "
            "contextual PHI) on top of the regex baseline. Costs LLM "
            "credits; default off so the publish stays free."
        ),
    )


# Auto-generated __all__: ensures child modules' `from ._shared
# import *` pulls in the underscore-prefixed helpers (constants like
# _AGENT_PATIENT_IMAGES and private guards like _get_patient_or_404)
# that python's default `import *` semantics would otherwise drop.
__all__ = [
    "DELETE",
    "PUBLIC_SUBJECT_ID",
    "READ_METADATA",
    "SEARCH_SECTIONS",
    "UTC",
    "_BULK_UPDATE_ASYNC_THRESHOLD",
    "_BULK_UPDATE_HARD_CAP",
    "_DEFAULT_PURGE_AFTER_DAYS",
    "_DOCUMENT_STUDY_LINK_KINDS",
    "_INLINE_TEXT_MAX_BYTES",
    "_PATIENT_SCOPES",
    "_PWD_ALPHABET",
    "_SEMANTIC_FALLBACK_THRESHOLD",
    "APIRouter",
    "ActorContext",
    "Annotated",
    "AsyncSession",
    "AuditDep",
    "BaseModel",
    "BulkDocumentUpdateIn",
    "BulkDocumentUpdateItem",
    "BulkDocumentUpdateItemOut",
    "BulkDocumentUpdateOut",
    "CFDecodeOut",
    "CitationRefOut",
    "ContactCreateIn",
    "ContactDelegateIn",
    "ContactDelegateOut",
    "ContactUpdateIn",
    "ContentRefOut",
    "DeleteDocumentIn",
    "Depends",
    "Document",
    "DocumentBinaryUrlOut",
    "DocumentEntitiesOut",
    "DocumentEntitiesRunIn",
    "DocumentFile",
    "DocumentMergeIn",
    "DocumentMergeOut",
    "DocumentReferencesOut",
    "DocumentStudyLink",
    "DocumentStudyLinkIn",
    "DocumentStudyLinkOut",
    "DocumentTextOut",
    "DocumentTextRunIn",
    "DocumentVersionOut",
    "DocumentVersionsOut",
    "FascicoloIndex",
    "FascicoloSection",
    "Field",
    "File",
    "Folder",
    "FolderItem",
    "FolderMembershipOut",
    "Form",
    "Grant",
    "HTTPException",
    "IdempotencyContext",
    "ImagingStudy",
    "Instance",
    "LabPointOut",
    "LabTimeseriesOut",
    "LabTrendOut",
    "Marker",
    "PaginatedPatients",
    "Patient",
    "PatientContact",
    "PatientCreateIn",
    "PatientDocumentFileOut",
    "PatientDocumentOut",
    "PatientDocumentUpdateIn",
    "PatientOut",
    "PatientSearchItem",
    "PatientSearchOut",
    "PatientUpdateIn",
    "PublishIn",
    "PublishOut",
    "Query",
    "Request",
    "Response",
    "Series",
    "ShareCreateIn",
    "ShareLink",
    "ShareLinkOut",
    "ShareTarget",
    "StreamingResponse",
    "StudyDocumentLinkOut",
    "StudyRefOut",
    "Tag",
    "Text",
    "TimelineItem",
    "UnsupportedThumbnailKindError",
    "UploadFile",
    "User",
    "_autogen_share_password",
    "_content_disposition",
    "_doc_out",
    "_document_binary_target",
    "_document_diff",
    "_document_versioning_payload",
    "_enqueue_ocr_async",
    "_get_patient_or_404",
    "_link_out",
    "_load_patient_contacts",
    "_log",
    "_normalise_analyte",
    "_parse_sections",
    "_patient_diff",
    "_patient_origin",
    "_patient_out",
    "_patient_versioning_payload",
    "_preview",
    "_read_s3_prefix",
    "_resolve_notes_editor_name",
    "_seed_contact_ids",
    "annotations",
    "asyncio",
    "can_patient",
    "coerce_link_kind",
    "date",
    "datetime",
    "desc",
    "dry_run_flag",
    "effective_permissions_on_patient",
    "enforce_agent_patient_scope",
    "enforce_agent_scope",
    "etag_for_branch",
    "format_etag",
    "func",
    "get_db",
    "get_s3_storage",
    "get_settings",
    "hash_password",
    "idempotent",
    "is_supported_thumbnail_mime",
    "level_to_permissions",
    "load_active_catalog_ids",
    "logging",
    "optional_user",
    "or_",
    "parse_if_match",
    "platform_owner_subject_id",
    "problem",
    "proxy_s3_object",
    "publish_patient_to_opendata",
    "record_versioned_change",
    "render_document_thumbnail",
    "require_if_match",
    "require_user",
    "router",
    "secrets",
    "seed_patient_main",
    "select",
    "sqlalchemy_exc",
    "status",
    "timedelta",
    "translate_catalog_fk_violation",
    "uuid",
    "validate_kind_id",
    "validate_mime",
    "validate_size",
    "visible_patients_filter",
]
