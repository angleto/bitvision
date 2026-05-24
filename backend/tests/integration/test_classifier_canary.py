"""Golden test for the care-phase classifier on patient Patient X (canary).

The acceptance bar is **100% match**: 7/7 phases by slug AND every
event assigned to the correct phase. No tolerance threshold (see
memory ``feedback_real_patient_golden_test``).

The test is automatically skipped when the configured LLM provider is
``stub`` (``BVP_LLM_PROVIDER=stub``), because the stub provider does
not produce structured output. To run the real evaluation:

    BVP_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... \\
      uv run pytest tests/integration/test_classifier_canary.py -q

If the test fails:
1. inspect the divergence (printed in the failure message);
2. iterate on the prompt in
   ``src/bvphoenix/services/care_phase_classifier.py``;
3. if a single LLM stage cannot reach 7/7, leave ``use_verifier=True``
   (default) so the second-stage verifier runs;
4. as a last resort, escalate the architecture (e.g. richer event
   context, schema-stricter prompt) until 7/7 is reached. Never relax
   the bar, never hard-code the expected output to match buggy LLM
   output.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from bvphoenix.config import get_settings
from bvphoenix.db.models import ClinicalEvent, Patient
from bvphoenix.services.care_phase_classifier import propose_for_patient

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "care_phases"
    / "canary_patient_expected.json"
)


def _load_expected() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


pytestmark = pytest.mark.skipif(
    get_settings().llm_provider != "anthropic",
    reason=(
        "Golden classifier test requires BVP_LLM_PROVIDER=anthropic and a real "
        "ANTHROPIC_API_KEY; the stub provider cannot satisfy the 7/7 bar."
    ),
)


@pytest.fixture
async def canary_patient_id() -> uuid.UUID:
    """Resolve patient Patient X (canary) on the local DB.

    The bulk-imported patient lives at id ``00000000-...`` (see memory
    ``import_session_2026_05_03``). Override via env ``CANARY_PATIENT_ID``
    when running against a different snapshot.
    """
    raw = os.environ.get("CANARY_PATIENT_ID")
    if raw:
        return uuid.UUID(raw)
    pytest.skip("set CANARY_PATIENT_ID env var to run the golden test")


@pytest.fixture
async def db() -> AsyncSession:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sm = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as session:
        yield session
    await engine.dispose()


def _normalize_title(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _event_matches(event_title: str, expected_pattern: str) -> bool:
    return _normalize_title(expected_pattern) in _normalize_title(event_title)


@pytest.mark.asyncio
async def test_canary_classifier_seven_out_of_seven(
    db: AsyncSession,
    canary_patient_id: uuid.UUID,
) -> None:
    expected = _load_expected()
    expected_phases = expected["phases"]
    assert len(expected_phases) == 7

    # Sanity: the patient must exist on the DB hosting this test.
    patient = (
        await db.execute(select(Patient).where(Patient.id == canary_patient_id))
    ).scalar_one_or_none()
    if patient is None:
        pytest.skip(f"patient {canary_patient_id} not found in this database")

    # Run the classifier (no cache bypass — if a previous run produced a
    # cached proposal with the same input_hash it will be returned and
    # we still check it against the golden).
    out = await propose_for_patient(
        patient_id=canary_patient_id,
        actor_id=None,
        lang="it",
        db=db,
        bypass_cache=True,
        use_verifier=True,
    )

    proposed_slugs = [p.slug for p in out.payload.phases]
    expected_slugs = [p["slug"] for p in expected_phases]

    assert sorted(proposed_slugs) == sorted(expected_slugs), (
        f"phase slugs mismatch.\n"
        f"  expected (sorted): {sorted(expected_slugs)}\n"
        f"  proposed (sorted): {sorted(proposed_slugs)}\n"
    )
    assert proposed_slugs == expected_slugs, (
        "phase slug ORDERING differs from chronological ground truth.\n"
        f"  expected order: {expected_slugs}\n"
        f"  proposed order: {proposed_slugs}\n"
    )

    # Build event_id → phase_slug from the proposal.
    by_event_id = {a.event_id: a.phase_slug for a in out.payload.assignments}

    # Re-fetch all of the patient's events so we can compare titles.
    events = (
        (
            await db.execute(
                select(ClinicalEvent)
                .where(ClinicalEvent.patient_id == canary_patient_id)
                .order_by(ClinicalEvent.event_date.asc().nulls_last())
            )
        )
        .scalars()
        .all()
    )

    # For each expected (date, title_pattern), find the matching event in
    # the DB and assert the classifier put it in the expected phase.
    failures: list[str] = []
    for exp_phase in expected_phases:
        for exp_event in exp_phase["events"]:
            exp_date = date.fromisoformat(exp_event["date"])
            pattern = exp_event["title_pattern"]
            candidates = [
                e for e in events if e.event_date == exp_date and _event_matches(e.title, pattern)
            ]
            if not candidates:
                failures.append(f"missing input event: date={exp_date} pattern={pattern!r}")
                continue
            ev = candidates[0]
            actual_slug = by_event_id.get(ev.id)
            if actual_slug != exp_phase["slug"]:
                failures.append(
                    f"event {ev.id} ({exp_date} {pattern!r}) "
                    f"expected phase {exp_phase['slug']!r} "
                    f"but classifier said {actual_slug!r}"
                )

    assert not failures, "classifier did not reach 7/7 on Patient X (canary).\n" + "\n".join(
        f"  - {f}" for f in failures
    )
