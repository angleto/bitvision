"""Per-study de-identification provenance read endpoint (Flow 0f706c4a).

GET /studies/{id}/deidentification-provenance turns the text-redaction
ledger into the auditable per-study record that an irreversible
black-box cannot offer. The crux:

* an ANONYMOUS reader of a PUBLIC OpenData study gets the real counts —
  the handler reads ``redaction_events`` in a service-context session
  after authorising at the study boundary, so the table's
  authenticated-only RLS does not silently zero the public record;
* the response is aggregate + storage-isolated — category counts only,
  never an excerpt / prompt hash, actor, note id, or S3 location;
* it claims TEXT de-identification only (the ``scope`` is explicit that
  DICOM PS3.15 header/pixel handling is separate).

We call the handler directly (async, on the test's event loop) rather
than through TestClient, whose separate loop corrupts the shared
``db_session``. A route-registration assertion guards the contract.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from bvphoenix.api.studies.core import (
    DeidentificationProvenanceOut,
    get_study_deidentification_provenance,
)
from bvphoenix.db.models.clinical_notes import ClinicalNote
from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
from bvphoenix.main import app
from tests.conftest import skip_if_no_db

_HASH = b"\x00" * 32  # redaction_events CHECK: octet_length = 32


class _StubAudit:
    async def log(self, **_kw: Any) -> None:
        return None


async def _insert_redaction(
    db, *, note_id: uuid.UUID, kind: str, actor: uuid.UUID, **extra
) -> None:
    await db.execute(
        text(
            "INSERT INTO redaction_events "
            "(target_kind, target_id, field_path, original_excerpt_hash, "
            " redaction_kind, model_id, provider, applied_by_subject_id) "
            "VALUES ('clinical_note', :ti, 'body', :h, :rk, :mi, :pr, :sub)"
        ),
        {
            "ti": str(note_id),
            "h": _HASH,
            "rk": kind,
            "mi": extra.get("model_id"),
            "pr": extra.get("provider"),
            "sub": str(actor),
        },
    )


def test_route_is_registered() -> None:
    assert any("deidentification-provenance" in getattr(r, "path", "") for r in app.routes), (
        "deid-provenance route must be registered"
    )


@skip_if_no_db
async def test_deid_provenance_anonymous_public_study_aggregates_and_isolates(
    db_session, make_user, make_study
) -> None:
    owner = await make_user()
    study, _series = await make_study(owner)

    # Service context: clinical_notes + redaction_events INSERT policies are
    # service-only / author-scoped, and the imaging_studies UPDATE below is
    # owner/service-scoped.
    await set_current_subject(db_session, SERVICE_SUBJECT)
    # A valid public OpenData study: is_public ⇒ t4
    # (ck_imaging_studies_public_tier_t4), and t4 ⇒ license + source
    # (ck_imaging_studies_t4_license). This is the audience for the record.
    study.contribution_tier = "t4"
    study.is_public = True
    study.license_spdx = "CC-BY-4.0"
    study.source_collection = "test-collection"
    db_session.add(study)
    await db_session.flush()

    note = ClinicalNote(
        patient_id=study.patient_id,
        target_kind="study",
        target_id=study.id,
        author_subject_id=owner.subject_id,
        body="redacted note",
    )
    db_session.add(note)
    await db_session.flush()

    for kind in ("regex_codice_fiscale", "regex_codice_fiscale", "regex_email"):
        await _insert_redaction(db_session, note_id=note.id, kind=kind, actor=owner.subject_id)
    await _insert_redaction(
        db_session,
        note_id=note.id,
        kind="llm_scrub_via_mcp",
        actor=owner.subject_id,
        model_id="claude-opus-4-8",
        provider="anthropic",
    )
    await db_session.commit()

    try:
        # user=None ⇒ anonymous; the public study passes the boundary gate and
        # the service-context sub-read returns the real counts.
        out = await get_study_deidentification_provenance(study.id, db_session, None, _StubAudit())
        assert isinstance(out, DeidentificationProvenanceOut)

        cats = {c.category: c for c in out.text_redactions}
        assert cats["regex_codice_fiscale"].count == 2
        assert cats["regex_email"].count == 1
        assert cats["llm_scrub_via_mcp"].count == 1
        assert cats["llm_scrub_via_mcp"].model_id == "claude-opus-4-8"
        assert cats["llm_scrub_via_mcp"].provider == "anthropic"
        assert out.total_text_redactions == 4
        assert out.notes_redacted == 1
        assert out.is_public is True
        assert out.contribution_tier is not None
        assert "PS3.15" in out.scope  # honest about what it does NOT claim

        # Storage isolation: aggregate only — no per-event row leaks.
        payload = out.model_dump_json()
        for forbidden in (
            str(note.id),
            str(owner.subject_id),
            "hash",
            "excerpt",
            "applied_by",
            "s3",
            "bucket",
        ):
            assert forbidden not in payload, f"storage isolation leak: {forbidden!r}"
    finally:
        await db_session.execute(
            text("DELETE FROM redaction_events WHERE target_id = :ti"), {"ti": str(note.id)}
        )
        await db_session.execute(
            text("DELETE FROM clinical_notes WHERE id = :id"), {"id": str(note.id)}
        )
        await db_session.commit()
