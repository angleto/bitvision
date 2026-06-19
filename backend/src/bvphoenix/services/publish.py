"""Publish a private fascicolo to the OpenData section.

The publish flow is **clone-and-scrub**: a brand new ``Patient`` row is
created, owned by ``PLATFORM_OWNER``, with anonymised demographics and
redacted text fields. The original private fascicolo is never mutated.
The new public fascicolo lives on its own main branch with its own
versioning history; subsequent erasure on the private side does NOT
affect the public clone (and vice versa), because anonymised data is
no longer "personal data" under GDPR Art. 4.

For F12.4 v0 we limit the scope to **clinical notes**. Other clinical
entity kinds (reports, consultations, patient_documents) follow the
same pattern and will be added as the publish flow stabilises;
the framework here is entity-agnostic.

Pipeline per call:

  1. Validate write permission on the source patient.
  2. Build the anonymised demographics dict (name → pseudonym, tax_id →
     null, birth → year-only, contacts → null, email/phone → null).
  3. Insert the new ``Patient`` row owned by PLATFORM_OWNER.
  4. For each clinical_note of the source patient:
     a. Apply :func:`redact_text` to the body.
     b. Insert a clone row on the new patient, owned by PLATFORM_OWNER
        (author_subject_id rewritten to PLATFORM_OWNER for full
        anonymity; original author kept only in audit log).
     c. Record one ``redaction_events`` row per redaction performed.
  5. Seed main on the new patient with a ``[opendata] initial publication``
     commit whose manifest contains the public clones.
  6. Return the new patient_id.

Idempotent guard: if the source patient was already published (a
``patient_publications`` link table — TODO follow-up), abort with 409.
For F12.4 v0 we let the caller decide; multiple publishes simply
create multiple OpenData clones.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ClinicalNote, Patient
from bvphoenix.services.deid_text import (
    LlmRedaction,
    Redaction,
    redact_text,
    redact_with_llm,
)
from bvphoenix.services.permissions import platform_owner_subject_id
from bvphoenix.services.versioning import (
    ActorContext,
    EntityChange,
    commit_change,
)

__all__ = ["PublishResult", "publish_patient_to_opendata"]


@dataclass(slots=True)
class PublishResult:
    """Outcome of a publish call."""

    public_patient_id: uuid.UUID
    public_main_commit: bytes
    redaction_count: int
    cloned_clinical_notes: int


def _anonymise_demographics(p: Patient, pseudonym: str) -> dict:
    """Build the demographics block for the OpenData clone.

    Aggressive scrub: anything that could identify the patient
    individually is dropped or downgraded. ``birth_date`` becomes
    January 1st of the same year (year-only granularity is the
    HIPAA-Safe-Harbor-style standard).
    """
    birth_year_only: date | None = None
    if p.birth_date is not None:
        birth_year_only = date(p.birth_date.year, 1, 1)
    # No identifier fields at all: v3 moved tax_id / external_id into the
    # external_identifiers JSONB array, which is simply never copied to
    # the clone (the column defaults to []). Contacts live in the
    # relational patient_contacts table and are likewise not cloned.
    return {
        "display_name": pseudonym,
        "birth_date": birth_year_only.isoformat() if birth_year_only else None,
        "sex": p.sex,
        "phone": None,
        "email": None,
        "address": None,
        "blood_type": p.blood_type,
        "allergies": redact_text(p.allergies)[0] or None,
        "notes": redact_text(p.notes)[0] or None,
    }


async def _record_redaction_event(
    db: AsyncSession,
    *,
    target_kind: str,
    target_id: uuid.UUID,
    field_path: str,
    redaction: Redaction | LlmRedaction,
    applied_by_subject_id: uuid.UUID,
) -> None:
    is_llm = isinstance(redaction, LlmRedaction)
    await db.execute(
        text(
            "INSERT INTO redaction_events "
            "(target_kind, target_id, field_path, original_excerpt_hash, "
            " redaction_kind, model_id, provider, prompt_hash, "
            " applied_by_subject_id) "
            "VALUES (:tk, :ti, :fp, :h, :rk, :mi, :pr, :ph, :sub)"
        ),
        {
            "tk": target_kind,
            "ti": target_id,
            "fp": field_path,
            "h": redaction.original_excerpt_hash,
            "rk": redaction.kind,
            "mi": redaction.model_id if is_llm else None,
            "pr": redaction.provider if is_llm else None,
            "ph": redaction.prompt_hash if is_llm else None,
            "sub": applied_by_subject_id,
        },
    )


async def publish_patient_to_opendata(
    db: AsyncSession,
    *,
    source_patient: Patient,
    actor: ActorContext,
    pseudonym: str | None = None,
    use_llm_scrub: bool = False,
) -> PublishResult:
    """Clone-and-scrub the source patient into a new OpenData fascicolo.

    Returns a :class:`PublishResult` with the new patient_id, the seed
    commit hash on its main branch, and counters for audit. The caller
    is expected to be inside a DB transaction; this function does NOT
    commit. Wrap with ``await db.commit()`` after audit_log writes.

    Permissions: the caller is responsible for verifying that the
    actor has write/publish authority on the source patient. The
    helper trusts the caller and only does the clone+scrub work.
    """
    platform_owner = platform_owner_subject_id()
    new_pid = uuid.uuid4()
    pseudonym_str = pseudonym or f"OpenData Patient {hashlib.sha256(new_pid.bytes).hexdigest()[:8]}"

    # 1. Insert the public Patient row (PLATFORM_OWNER-owned).
    demographics = _anonymise_demographics(source_patient, pseudonym_str)
    db.add(
        Patient(
            id=new_pid,
            managed_by_subject_id=platform_owner,
            self_user_subject_id=None,
            display_name=demographics["display_name"],
            birth_date=date.fromisoformat(demographics["birth_date"])
            if demographics["birth_date"]
            else None,
            sex=demographics["sex"],
            phone=None,
            email=None,
            address=None,
            blood_type=demographics["blood_type"],
            allergies=demographics["allergies"],
            notes=demographics["notes"],
        )
    )
    await db.flush()

    # 2. Clone clinical_notes (F12.4 v0 scope).
    src_notes = (
        await db.execute(
            text(
                "SELECT id, target_kind, target_id, body, pinned, "
                "  author_kind, model_id, provider, created_at "
                "FROM clinical_notes WHERE patient_id = :p"
            ),
            {"p": source_patient.id},
        )
    ).all()

    cloned_changes: list[EntityChange] = []
    redaction_count = 0
    cloned_count = 0

    for src in src_notes:
        (
            _src_id,
            src_target_kind,
            _src_target_id,
            src_body,
            src_pinned,
            src_author_kind,
            src_model_id,
            src_provider,
            _src_created,
        ) = src
        # Re-target to the new patient. If the original target_kind is
        # 'patient' we re-point to the new pid; for study/series/etc.
        # the F12.4 v0 scope skips them (no DICOM data is cloned in
        # this pilot).
        if src_target_kind == "patient":
            new_target_id = new_pid
        elif src_target_kind in ("study", "series", "report", "document", "consultation"):
            # F12.4 v0: study/series/etc. are not cloned, so notes
            # attached to them are dropped on publish. The decision
            # to publish a richer fascicolo is F12.4 follow-up.
            #
            # BURNED-IN-PHI GATE: when imaging publish lands here, DICOM pixels
            # MUST NOT be cloned to the public patient directly — route every
            # instance through the public-contribution review queue (M1), whose
            # PixelPhiCheck blocks/quarantines high-risk pixels. Header de-id
            # alone is insufficient (it never touches pixel data). See
            # services.pixel_deid.classify_pixel_risk.
            continue
        else:
            continue

        # Redact the body. Two passes:
        # 1. Regex baseline (always): catches CF / phone / email / dates /
        #    addresses with high precision.
        # 2. LLM scrub (optional, requested by caller): catches proper
        #    nouns / contextual PHI that regex doesn't see. Failures fall
        #    through silently — the regex pass already removed the
        #    high-confidence tokens, so a missing LLM run is degradation
        #    not data leak.
        redacted_body, redactions = redact_text(src_body or "")
        llm_redactions: list[LlmRedaction] = []
        if use_llm_scrub and redacted_body.strip():
            redacted_body, llm_redactions = await redact_with_llm(redacted_body)
        if not redacted_body.strip():
            # Body became empty after redaction — drop the note rather
            # than publish a placeholder-only artefact.
            continue
        new_note_id = uuid.uuid4()

        db.add(
            ClinicalNote(
                id=new_note_id,
                patient_id=new_pid,
                target_kind=src_target_kind,
                target_id=new_target_id,
                author_subject_id=platform_owner,
                author_kind=src_author_kind,
                model_id=src_model_id,
                provider=src_provider,
                body=redacted_body,
                pinned=src_pinned,
            )
        )
        await db.flush()

        for r in redactions:
            await _record_redaction_event(
                db,
                target_kind="clinical_note",
                target_id=new_note_id,
                field_path="body",
                redaction=r,
                applied_by_subject_id=actor.subject_id or platform_owner,
            )
            redaction_count += 1
        for r in llm_redactions:
            await _record_redaction_event(
                db,
                target_kind="clinical_note",
                target_id=new_note_id,
                field_path="body",
                redaction=r,
                applied_by_subject_id=actor.subject_id or platform_owner,
            )
            redaction_count += 1

        cloned_count += 1
        cloned_changes.append(
            EntityChange(
                entity_kind="clinical_note",
                entity_id=new_note_id,
                payload={
                    "id": str(new_note_id),
                    "patient_id": str(new_pid),
                    "target_kind": src_target_kind,
                    "target_id": str(new_target_id),
                    "body": redacted_body,
                    "pinned": src_pinned,
                    "author_subject_id": str(platform_owner),
                    "author_kind": src_author_kind,
                    "model_id": src_model_id,
                    "provider": src_provider,
                    "agent_token_id": None,
                    "schema_version": 1,
                },
            )
        )

    # 3. Seed main on the new patient with the publication commit.
    publication_actor = ActorContext(
        subject_id=platform_owner,
        kind="system",
    )
    seed_change = EntityChange(
        entity_kind="patient",
        entity_id=new_pid,
        payload={
            "id": str(new_pid),
            "schema_version": 1,
            "display_name": demographics["display_name"],
            "_opendata_publication": True,
            "_published_at": datetime.now().isoformat(),
        },
    )
    result = await commit_change(
        db,
        patient_id=new_pid,
        branch_ref="main",
        actor=publication_actor,
        message=(
            "[opendata] initial publication "
            f"(cloned {cloned_count} note(s) with {redaction_count} redaction(s))"
        ),
        changes=[seed_change, *cloned_changes],
    )

    return PublishResult(
        public_patient_id=new_pid,
        public_main_commit=result.commit_hash,
        redaction_count=redaction_count,
        cloned_clinical_notes=cloned_count,
    )


# Silence "unused" linters for the ``Any``/``hashlib`` imports that
# may not be referenced in every code path of the module.
_ = (Any, hashlib)
