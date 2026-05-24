"""Clinical notes — free-text per-item commentary scoped to a patient.

A clinical note is what the radiologist or referring clinician writes
on the side: "iron is high here", "follow-up CT in 6 months", "pt
allergic to ramipril, switch to losartan". Notes attach to anything
in the fascicolo (study, series, document, consultation, the patient
record itself) and aggregate into a single chronological evidence
view at ``GET /api/patients/{id}/notes``.

Permissions:
- Reading + creating require ``read:metadata`` on the patient (any
  reader can write notes, encouraging collaborative consultation).
- Updating / deleting an existing note is restricted to the author
  and the patient owner (RLS enforces this independently).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.deps import require_user
from bvphoenix.db.models import ClinicalNote, Patient, User
from bvphoenix.db.models.clinical_notes import CLINICAL_NOTE_TARGET_KINDS
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.evidence_links import validate_mentions_or_raise
from bvphoenix.services.permissions import (
    READ_METADATA,
    can_patient,
)
from bvphoenix.services.versioning import (
    ActorContext,
    EntityChange,
    commit_change,
    resolve_branch_for_write,
)

router = APIRouter(tags=["clinical-notes"])


class NoteAnchor(BaseModel):
    """Voxel anchor pinned to a specific ``(x, y, z)`` of the active
    series volume. Coordinates are zero-based integer indices."""

    x: int
    y: int
    z: int


class ClinicalNoteOut(BaseModel):
    id: str
    patient_id: str
    target_kind: str
    target_id: str
    author_subject_id: str
    author_kind: str
    # Hard boolean: True iff the note was authored by an AI agent.
    # Frontend must render with a visual treatment that cannot be
    # confused with human-authored content. See consultations.py for
    # the same flag rationale.
    is_ai_generated: bool
    model_id: str | None
    provider: str | None
    agent_token_id: str | None
    body: str
    pinned: bool
    # Optional spatial anchor (viewer-pinned notes). ``null`` for
    # plain text notes that don't reference a specific voxel.
    anchor: NoteAnchor | None = None
    created_at: str
    updated_at: str


class ClinicalNoteCreateIn(BaseModel):
    target_kind: str = Field(..., description=f"One of {CLINICAL_NOTE_TARGET_KINDS}")
    target_id: uuid.UUID
    body: str = Field(..., min_length=1, max_length=8000)
    pinned: bool = False
    # Optional viewer-pinned anchor. The backend stores it as JSONB
    # without further validation beyond the Pydantic schema; clients
    # send it when the note was authored from inside the viewer.
    anchor: NoteAnchor | None = None
    # Agent callers (MCP) populate these. For human callers they're
    # ignored — author_kind is derived from the request, not the body.
    model_id: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=64)


class ClinicalNoteUpdateIn(BaseModel):
    body: str | None = Field(default=None, min_length=1, max_length=8000)
    pinned: bool | None = None
    # Set to ``null`` to remove the anchor; omit to leave it as is.
    anchor: NoteAnchor | None = None


def _versioning_payload(n: ClinicalNote) -> dict:
    """Snapshot of the clinical note that lands in ``entity_objects``.

    Excludes server timestamps (the commit's own ``created_at`` is the
    canonical event time; including them here would defeat content
    dedup across no-op edits).
    """
    return {
        "id": str(n.id),
        "patient_id": str(n.patient_id),
        "target_kind": n.target_kind,
        "target_id": str(n.target_id),
        "body": n.body,
        "pinned": n.pinned,
        "anchor": n.anchor,
        "author_subject_id": str(n.author_subject_id),
        "author_kind": n.author_kind,
        "model_id": n.model_id,
        "provider": n.provider,
        "agent_token_id": str(n.agent_token_id) if n.agent_token_id else None,
        "schema_version": 1,
    }


async def _record_clinical_note_change(
    db: AsyncSession,
    *,
    patient: Patient,
    note: ClinicalNote | None,
    note_id: uuid.UUID,
    user: User,
    request: Request,
    message: str,
    consultation_id: uuid.UUID | None,
) -> None:
    """Add a versioning commit for a clinical-note mutation.

    Routing rule (see ``services/versioning.resolve_branch_for_write``):
      - if a consultation_id is supplied and the consultation is in a
        mutable state for this user, the commit goes on its branch
      - otherwise, owner writes go to ``main`` directly
      - non-owner writes without an active consultation are rejected
        with 403 (the user must open a consultation first)

    ``note`` is None for delete (the new manifest drops the entry); otherwise
    the canonical payload is taken from the ORM row.
    """
    is_owner = (
        patient.managed_by_subject_id == user.subject_id
        or patient.self_user_subject_id == user.subject_id
        or user.is_admin
    )
    try:
        branch_ref = await resolve_branch_for_write(
            db,
            patient_id=patient.id,
            user_subject_id=user.subject_id,
            consultation_id=consultation_id,
            is_owner=is_owner,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    request_agent_token = getattr(request.state, "agent_token", None)
    if request_agent_token is not None and note is not None and note.author_kind == "agent":
        actor = ActorContext(
            subject_id=user.subject_id,
            kind="agent",
            model_id=note.model_id,
            provider=note.provider,
            agent_token_id=note.agent_token_id,
        )
    else:
        actor = ActorContext(subject_id=user.subject_id, kind="human")

    payload = _versioning_payload(note) if note is not None else None
    await commit_change(
        db,
        patient_id=patient.id,
        branch_ref=branch_ref,
        actor=actor,
        message=message,
        changes=[
            EntityChange(
                entity_kind="clinical_note",
                entity_id=note_id,
                payload=payload,
            )
        ],
    )


def _anchor_out(raw: dict | None) -> NoteAnchor | None:
    """Coerce the raw JSONB blob into the typed schema. Defensive: a
    note row with malformed anchor (legacy import, manual SQL) shouldn't
    blow the whole list endpoint up."""
    if not isinstance(raw, dict):
        return None
    try:
        return NoteAnchor(x=int(raw["x"]), y=int(raw["y"]), z=int(raw["z"]))
    except (KeyError, TypeError, ValueError):
        return None


def _out(n: ClinicalNote) -> ClinicalNoteOut:
    return ClinicalNoteOut(
        id=str(n.id),
        patient_id=str(n.patient_id),
        target_kind=n.target_kind,
        target_id=str(n.target_id),
        author_subject_id=str(n.author_subject_id),
        author_kind=n.author_kind,
        is_ai_generated=(n.author_kind == "agent"),
        model_id=n.model_id,
        provider=n.provider,
        agent_token_id=str(n.agent_token_id) if n.agent_token_id else None,
        body=n.body,
        pinned=n.pinned,
        anchor=_anchor_out(n.anchor),
        created_at=n.created_at.isoformat(),
        updated_at=n.updated_at.isoformat(),
    )


async def _get_patient_or_404(
    db: AsyncSession, patient_id: uuid.UUID, user: User, request: Request
) -> Patient:
    """Mirror of api/patients.py::_get_patient_or_404 for this router.

    Re-implemented locally to avoid a circular import with the patients
    module; the gating is the same: row exists, scope OK, READ_METADATA.
    """
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    # Reuse the agent-token patient scope guard exposed by auth helpers.
    from bvphoenix.auth import enforce_agent_patient_scope

    enforce_agent_patient_scope(request, patient.id, scope="patient:read")
    if not await can_patient(db, user=user, action=READ_METADATA, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")
    return patient


@router.get(
    "/patients/{patient_id}/notes",
    response_model=list[ClinicalNoteOut],
)
async def list_clinical_notes(
    request: Request,
    patient_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    target_kind: str | None = Query(None, max_length=32),
    target_id: uuid.UUID | None = Query(None),
    author_kind: str | None = Query(
        None,
        pattern="^(human|agent)$",
        description="Filter by author kind (human / agent).",
    ),
    exclude_models: list[str] | None = Query(
        default=None,
        description=(
            "Hide notes generated by specific AI models. Repeat the "
            "param: ``?exclude_models=claude-opus-4-6&exclude_models=gpt-5``."
        ),
    ),
    limit: int = Query(200, ge=1, le=500),
) -> list[ClinicalNoteOut]:
    """List clinical notes for a patient, newest first.

    With no filter the response is the **aggregated evidence view** —
    every note attached to anything under this patient (human + AI).
    Filtering options:

      - ``target_kind`` + ``target_id`` — scope to a single item.
      - ``author_kind=human`` — hide all AI-generated notes.
      - ``author_kind=agent`` — show only AI-generated notes.
      - ``exclude_models=claude-opus-4-6`` (repeatable) — hide notes
        from a specific model. Useful when retiring an older model and
        keeping only fresh outputs.
    """
    patient = await _get_patient_or_404(db, patient_id, user, request)

    q = select(ClinicalNote).where(ClinicalNote.patient_id == patient.id)
    if target_kind:
        if target_kind not in CLINICAL_NOTE_TARGET_KINDS:
            raise HTTPException(status_code=400, detail="invalid target_kind")
        q = q.where(ClinicalNote.target_kind == target_kind)
    if target_id is not None:
        q = q.where(ClinicalNote.target_id == target_id)
    if author_kind:
        q = q.where(ClinicalNote.author_kind == author_kind)
    if exclude_models:
        q = q.where(
            (ClinicalNote.model_id.is_(None)) | (~ClinicalNote.model_id.in_(exclude_models))
        )
    # Pinned first, then newest. Pinned notes are clinically important
    # context the next reader should always see at the top.
    q = q.order_by(ClinicalNote.pinned.desc(), ClinicalNote.created_at.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [_out(n) for n in rows]


@router.post(
    "/patients/{patient_id}/notes",
    response_model=ClinicalNoteOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_clinical_note(
    request: Request,
    patient_id: uuid.UUID,
    body: ClinicalNoteCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    consultation: uuid.UUID | None = Query(
        None,
        description=(
            "Active consultation id; when set, the write is routed to that "
            "consultation's branch instead of main. Required for non-owners."
        ),
    ),
) -> ClinicalNoteOut:
    patient = await _get_patient_or_404(db, patient_id, user, request)
    if body.target_kind not in CLINICAL_NOTE_TARGET_KINDS:
        raise HTTPException(status_code=400, detail="invalid target_kind")

    # Cross-patient guard for the Evidenze e sintesi DSL: every
    # ``@kind:UUID`` mention in the body must resolve to a resource of
    # this same patient. The validator raises 422 with a structured
    # detail so the editor can highlight the offending span(s).
    await validate_mentions_or_raise(db, patient_id=patient.id, body=body.body)

    # Authorship is derived from the request, not the body. If the
    # caller is using an MCP agent token, we record author_kind='agent'
    # plus the token id — even if the body claimed something else. A
    # human caller's body model_id / provider are ignored (irrelevant
    # for human authorship).
    request_agent_token = getattr(request.state, "agent_token", None)
    if request_agent_token is not None:
        author_kind = "agent"
        agent_token_id = request_agent_token.id
        model_id = body.model_id
        provider = body.provider
    else:
        author_kind = "human"
        agent_token_id = None
        model_id = None
        provider = None

    note = ClinicalNote(
        patient_id=patient.id,
        target_kind=body.target_kind,
        target_id=body.target_id,
        author_subject_id=user.subject_id,
        author_kind=author_kind,
        model_id=model_id,
        provider=provider,
        agent_token_id=agent_token_id,
        body=body.body.strip(),
        pinned=body.pinned,
        anchor=body.anchor.model_dump() if body.anchor is not None else None,
    )
    db.add(note)
    # Flush to get the server-side id and timestamps, then version.
    # Both the row and the commit_change writes commit together, atomic.
    await db.flush()
    await db.refresh(note)
    await _record_clinical_note_change(
        db,
        patient=patient,
        note=note,
        note_id=note.id,
        user=user,
        request=request,
        message=f"[clinical-notes] add note on {note.target_kind}",
        consultation_id=consultation,
    )
    await db.commit()
    await db.refresh(note)

    await audit.log(
        action="clinical_note_create",
        actor_subject_id=user.subject_id,
        resource_kind="clinical_note",
        resource_id=note.id,
        metadata={
            "patient_id": str(patient.id),
            "target_kind": note.target_kind,
            "target_id": str(note.target_id),
        },
    )
    return _out(note)


@router.patch(
    "/patients/{patient_id}/notes/{note_id}",
    response_model=ClinicalNoteOut,
)
async def update_clinical_note(
    request: Request,
    patient_id: uuid.UUID,
    note_id: uuid.UUID,
    body: ClinicalNoteUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    consultation: uuid.UUID | None = Query(
        None,
        description=(
            "Active consultation id; required when the editor is not the "
            "patient owner. Routes the edit to that branch."
        ),
    ),
) -> ClinicalNoteOut:
    patient = await _get_patient_or_404(db, patient_id, user, request)
    note = (
        await db.execute(
            select(ClinicalNote).where(
                ClinicalNote.id == note_id,
                ClinicalNote.patient_id == patient_id,
            )
        )
    ).scalar_one_or_none()
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")
    # RLS enforces author / owner gating on UPDATE; we mirror it for a
    # clearer 403 instead of the empty result the policy would produce.
    if note.author_subject_id != user.subject_id and not user.is_admin:
        # Patient owner check via the loaded patient
        owner = (
            await db.execute(select(Patient.managed_by_subject_id).where(Patient.id == patient_id))
        ).scalar_one_or_none()
        if owner != user.subject_id:
            raise HTTPException(
                status_code=403, detail="only the author or the patient owner can edit"
            )
    if body.body is not None:
        # Same cross-patient guard as create: an edit that introduces
        # a new mention must still be patient-scoped.
        await validate_mentions_or_raise(db, patient_id=patient.id, body=body.body)
        note.body = body.body.strip()
    if body.pinned is not None:
        note.pinned = body.pinned
    # ``anchor`` is tri-state in the PATCH body: omitted (leave as is),
    # explicit ``null`` (clear the anchor), or a populated payload
    # (replace). ``model_fields_set`` distinguishes omission from
    # explicit-null since both serialize as ``None`` on the attribute.
    if "anchor" in body.model_fields_set:
        note.anchor = body.anchor.model_dump() if body.anchor is not None else None
    note.updated_at = datetime.now(UTC)
    await db.flush()
    await _record_clinical_note_change(
        db,
        patient=patient,
        note=note,
        note_id=note.id,
        user=user,
        request=request,
        message=f"[clinical-notes] edit note on {note.target_kind}",
        consultation_id=consultation,
    )
    await db.commit()
    await db.refresh(note)
    return _out(note)


@router.delete(
    "/patients/{patient_id}/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_clinical_note(
    request: Request,
    patient_id: uuid.UUID,
    note_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    consultation: uuid.UUID | None = Query(
        None,
        description=(
            "Active consultation id; required when the deleter is not the "
            "patient owner. Routes the delete to that branch."
        ),
    ),
) -> None:
    patient = await _get_patient_or_404(db, patient_id, user, request)
    note = (
        await db.execute(
            select(ClinicalNote).where(
                ClinicalNote.id == note_id,
                ClinicalNote.patient_id == patient_id,
            )
        )
    ).scalar_one_or_none()
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")
    if note.author_subject_id != user.subject_id and not user.is_admin:
        owner = (
            await db.execute(select(Patient.managed_by_subject_id).where(Patient.id == patient_id))
        ).scalar_one_or_none()
        if owner != user.subject_id:
            raise HTTPException(
                status_code=403, detail="only the author or the patient owner can delete"
            )
    target_kind = note.target_kind
    await db.delete(note)
    await db.flush()
    await _record_clinical_note_change(
        db,
        patient=patient,
        note=None,
        note_id=note_id,
        user=user,
        request=request,
        message=f"[clinical-notes] delete note on {target_kind}",
        consultation_id=consultation,
    )
    await db.commit()
    await audit.log(
        action="clinical_note_delete",
        actor_subject_id=user.subject_id,
        resource_kind="clinical_note",
        resource_id=note_id,
        metadata={"patient_id": str(patient_id)},
    )
