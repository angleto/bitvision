# ruff: noqa: F405
# Auto-split from api/patients.py on 2026-05-21.
# Section: ``publish``. Decorators register against the
# local ``router`` below; the package __init__.py
# aggregates every child via include_router so main.py's
# wiring stays a single line.

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.patients import _shared  # for runtime access
from bvphoenix.api.patients._shared import *  # noqa: F403

router = APIRouter()


@router.post(
    "/patients/{patient_id}/publish",
    response_model=PublishOut,
    status_code=status.HTTP_201_CREATED,
)
async def publish_patient(
    request: Request,
    patient_id: uuid.UUID,
    body: PublishIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> PublishOut:
    """Publish a private fascicolo to the OpenData public dataset.

    Clone-and-scrub: a new ``Patient`` row is created, owned by
    ``PLATFORM_OWNER``, with redacted demographics and clinical notes.
    The original private fascicolo is never mutated. Subsequent erasure
    on either side does not affect the other.

    Permission: only the patient owner (or admin) can publish. The
    operation is irreversible at the user level: unpublishing requires
    a separate erasure-on-public-clone flow (F12.4 follow-up).
    """
    # Cross-patient guard for agent tokens. Publishing is a one-way
    # release of PHI into OpenData; an agent token scoped to patient
    # A must not be able to publish patient B even with a valid
    # bearer string.
    enforce_agent_patient_scope(request, patient_id)
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    is_owner = (
        patient.managed_by_subject_id == user.subject_id
        or patient.self_user_subject_id == user.subject_id
    )
    if not (is_owner or user.is_admin):
        raise HTTPException(status_code=403, detail="only the owner can publish a patient")

    actor = ActorContext(subject_id=user.subject_id, kind="human")
    result = await publish_patient_to_opendata(
        db,
        source_patient=patient,
        actor=actor,
        pseudonym=body.pseudonym,
        use_llm_scrub=body.use_llm_scrub,
    )
    await db.commit()

    await audit.log(
        action="patient_publish_opendata",
        actor_subject_id=user.subject_id,
        resource_kind="patient",
        resource_id=patient_id,
        metadata={
            "public_patient_id": str(result.public_patient_id),
            "cloned_clinical_notes": result.cloned_clinical_notes,
            "redaction_count": result.redaction_count,
        },
    )

    return PublishOut(
        public_patient_id=str(result.public_patient_id),
        public_main_commit=result.public_main_commit.hex(),
        cloned_clinical_notes=result.cloned_clinical_notes,
        redaction_count=result.redaction_count,
    )
