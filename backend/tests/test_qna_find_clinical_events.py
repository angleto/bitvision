"""Regression: find_clinical_events must accept ISO date-string args.

The Q&A orchestrator passes LLM-supplied ``since`` / ``until`` as ISO
strings. asyncpg binds a DATE column expecting a ``datetime.date`` (it
calls ``.toordinal()``), so a raw string raised ``DataError`` and the AI
assistant failed every time-filtered clinical-events query (the reported
"esami del sangue dell'ultimo anno" / "problema tecnico").

Needs a migrated Postgres; point BVP_DATABASE_URL at bvphoenix_test.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ClinicalEvent, Patient
from bvphoenix.services.qna_tools import build_executors
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


async def test_find_clinical_events_accepts_iso_date_strings(
    db_session: AsyncSession, make_user
) -> None:
    owner = await make_user(email=f"qna-{uuid.uuid4().hex[:8]}@test.local")
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="QnA patient",
    )
    db_session.add(patient)
    await db_session.flush()
    ev = ClinicalEvent(
        patient_id=patient.id,
        kind="lab_batch",
        event_date=date(2025, 9, 1),
        title="Emocromo completo",
        source="manual",
    )
    db_session.add(ev)
    await db_session.flush()

    execs = build_executors(db=db_session, patient_id=patient.id)

    # ISO date strings exactly as the LLM supplies them — pre-fix this
    # raised asyncpg DataError ('str' object has no attribute 'toordinal').
    out = await execs["find_clinical_events"](
        {"kind": "lab_batch", "since": "2025-01-01", "until": "2025-12-31"}
    )
    assert "Emocromo completo" in json.dumps(json.loads(out))

    # A window that excludes the event still returns cleanly (no error).
    out2 = await execs["find_clinical_events"](
        {"kind": "lab_batch", "since": "2024-01-01", "until": "2024-12-31"}
    )
    assert "Emocromo completo" not in json.dumps(json.loads(out2))

    # An unparseable date drops the filter instead of crashing the tool.
    out3 = await execs["find_clinical_events"]({"since": "not-a-date"})
    json.loads(out3)  # parseable JSON, no exception raised

    await db_session.rollback()
