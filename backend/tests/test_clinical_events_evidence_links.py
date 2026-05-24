"""Cross-patient guard on ClinicalEvent.narrative.

The PATCH/POST endpoints for ``/api/clinical-events`` accept a
markdown ``narrative`` that may carry the same ``@kind:UUID`` mention
DSL used by clinical_notes and report_contents. Without a server-side
guard, a human or agent could persist a mention pointing at another
patient's resource, which violates the "cross-patient impossible by
construction" invariant (see memory ``cross_patient_links_forbidden``).

This file pins:

1. Structural: ``api/clinical_events.py`` imports + invokes
   ``validate_mentions_or_raise``. Catches accidental removal during
   refactor without needing a live HTTP stack.
2. Functional: the validator raises HTTP 422 when a narrative
   mentions a study owned by a different patient. Skipped when no
   Postgres is available.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from bvphoenix.api import clinical_events as ce_api
from bvphoenix.services.evidence_links import validate_mentions_or_raise

from .conftest import skip_if_no_db


def test_clinical_events_module_imports_validator() -> None:
    """``validate_mentions_or_raise`` must be referenced by the module
    so a future refactor that removes the import is caught here."""
    src = inspect.getsource(ce_api)
    assert "validate_mentions_or_raise" in src, (
        "clinical_events.py must call validate_mentions_or_raise on narrative "
        "writes; without it the cross-patient guard is bypassed for events"
    )


def test_validator_called_in_create_and_patch_paths() -> None:
    """Belt-and-braces: the validator must be invoked from BOTH the
    create and patch handlers, not just one of them. We count call
    sites (``validate_mentions_or_raise(``, with the open paren) so
    the bare import on its own line is not counted; if either handler
    drops the call this test trips."""
    src = inspect.getsource(ce_api)
    occurrences = src.count("validate_mentions_or_raise(")
    assert occurrences >= 2, (
        f"expected ≥2 call sites of validate_mentions_or_raise (create + patch); "
        f"found {occurrences}"
    )


@skip_if_no_db
async def test_cross_patient_study_mention_raises_422(
    db_session,
    make_user,
    make_study,
) -> None:
    """A narrative that mentions a study owned by a different patient
    must be rejected with HTTP 422 + ``cross_patient_or_missing_link``."""
    owner_a = await make_user()
    owner_b = await make_user()
    # Two independent studies under two independent patients.
    study_a, _ = await make_study(owner_a)
    study_b, _ = await make_study(owner_b)

    body = (
        f"Mention of own study @study:{study_a.patient_id} is fine, "
        f"but @study:{study_b.id} is cross-patient."
    )

    with pytest.raises(Exception) as excinfo:
        await validate_mentions_or_raise(db_session, patient_id=study_a.patient_id, body=body)

    # HTTPException from FastAPI: status 422 + detail with violations.
    exc = excinfo.value
    assert getattr(exc, "status_code", None) == 422
    detail = getattr(exc, "detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "cross_patient_or_missing_link"
    violations = detail.get("violations") or []
    raws = {v["raw"] for v in violations}
    assert any(str(study_b.id) in raw for raw in raws), (
        f"expected a violation referencing study_b={study_b.id}, got {raws}"
    )


@skip_if_no_db
async def test_same_patient_study_mention_passes(
    db_session,
    make_user,
    make_study,
) -> None:
    """A narrative that mentions only resources of the same patient
    should pass through ``validate_mentions_or_raise`` and return the
    parsed mention list."""
    owner = await make_user()
    study, _ = await make_study(owner)

    body = f"See [Imaging baseline](@study:{study.id}) for the prior comparison."
    mentions = await validate_mentions_or_raise(db_session, patient_id=study.patient_id, body=body)
    assert len(mentions) == 1
    m = mentions[0]
    assert m.kind == "study"
    assert m.target_id == study.id
    assert m.title == "Imaging baseline"


@skip_if_no_db
async def test_unknown_uuid_does_not_block_save(
    db_session,
    make_user,
) -> None:
    """A mention pointing at a UUID that does not resolve to any
    resource (e.g. a document hard-deleted from production before the
    git-like soft-delete model landed) is **not** treated as a hard
    violation: the mention is rendered inline as broken by the FE
    and the user can edit it out, but a save that happens to touch a
    narrative containing such a stale reference must succeed.
    Cross-patient violations remain a hard 422 — that's the actual
    PHI-leakage concern the validator exists for.
    """
    owner = await make_user()
    # Patient minted via the user's subject so we have a real id even
    # without a study fixture.
    from bvphoenix.db.models import Patient

    pid = uuid.uuid4()
    patient = Patient(
        id=pid,
        managed_by_subject_id=owner.subject_id,
        display_name="Patient",
    )
    db_session.add(patient)
    await db_session.flush()

    body = f"Phantom @study:{uuid.uuid4()} should pass through with a warning."
    mentions = await validate_mentions_or_raise(db_session, patient_id=pid, body=body)
    # The mention list is still surfaced so the renderer can mark the
    # span as broken in the read view; the save itself does not raise.
    assert len(mentions) == 1
    assert mentions[0].kind == "study"
