"""Q&A Patch 2 — supersede-chain collapse + stale retrieval filter.

Three things must hold after this patch:

1. ``_collapse_superseded_chain`` rewrites a stale report's UUID to the
   head of the chain. Two citations on the same chain collapse to one.
2. ``_rewrite_collapsed_markers`` mirrors the rewrite onto the inline
   ``[report:UUID]`` markers in the answer body.
3. The ``get_event`` executor (qna_tools) hides report_contents with
   ``status='stale'`` so the model never sees them in the candidate set.

Tests run against a real DB session so the WITH RECURSIVE / SQL check
constraints and the model invariants are exercised end-to-end. Skipped
when no DB is available (matches the rest of the integration suite).
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    ClinicalEvent,
    Patient,
    ReportContent,
    Subject,
)
from bvphoenix.services.qna import (
    Citation,
    _collapse_superseded_chain,
    _rewrite_collapsed_markers,
)
from bvphoenix.services.qna_tools import build_executors
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


@pytest_asyncio.fixture
async def patient_with_supersede_chain(db_session: AsyncSession):
    """Patient with one clinical_event and a 2-hop report supersede chain.

    Layout:
        rc_old      -- authority=derived, status=stale,    superseded_by=rc_mid
        rc_mid      -- authority=derived, status=stale,    superseded_by=rc_head
        rc_head     -- authority=derived, status=endorsed, superseded_by=NULL

    Use this fixture for both the collapse-chain function tests and the
    ``get_event`` executor test (the executor must hide rc_old and
    rc_mid because they're stale, only rc_head should be returned).
    """
    pid = uuid.uuid4()
    sid = uuid.uuid4()
    eid = uuid.uuid4()
    rc_old_id = uuid.uuid4()
    rc_mid_id = uuid.uuid4()
    rc_head_id = uuid.uuid4()

    db_session.add(Subject(id=sid, kind="user", display_name=f"qna-supersede-{sid}"))
    await db_session.flush()
    db_session.add(
        Patient(
            id=pid,
            managed_by_subject_id=sid,
            display_name="Q&A Supersede Patient",
        )
    )
    await db_session.flush()
    db_session.add(
        ClinicalEvent(
            id=eid,
            patient_id=pid,
            kind="pathology_review",
            title="Anatomopatologico",
        )
    )
    await db_session.flush()
    # Insert head first so the supersede FKs resolve in dependency
    # order; flush between writes so each row is visible to the next.
    db_session.add(
        ReportContent(
            id=rc_head_id,
            clinical_event_id=eid,
            authority_id="derived",
            status="endorsed",
            title="Referto v3 (canonica)",
            narrative_md="Carcinoma duttale invasivo G2 — versione corrente.",
            created_by_subject_id=sid,
            author_kind="agent",
        )
    )
    await db_session.flush()
    db_session.add(
        ReportContent(
            id=rc_mid_id,
            clinical_event_id=eid,
            authority_id="derived",
            status="stale",
            title="Referto v2",
            narrative_md="Versione intermedia, già sostituita.",
            created_by_subject_id=sid,
            author_kind="agent",
            superseded_by_id=rc_head_id,
        )
    )
    await db_session.flush()
    db_session.add(
        ReportContent(
            id=rc_old_id,
            clinical_event_id=eid,
            authority_id="derived",
            status="stale",
            title="Referto v1",
            narrative_md="Prima estrazione, superata due volte.",
            created_by_subject_id=sid,
            author_kind="agent",
            superseded_by_id=rc_mid_id,
        )
    )
    await db_session.commit()

    yield {
        "patient_id": pid,
        "subject_id": sid,
        "event_id": eid,
        "rc_old": rc_old_id,
        "rc_mid": rc_mid_id,
        "rc_head": rc_head_id,
    }

    # Teardown: report_contents have a self-FK so break the chain head-
    # first by nulling superseded_by_id before deleting, otherwise
    # ON DELETE SET NULL handles it but the order is fragile under
    # CASCADE from clinical_event. Cascading via the event is simpler.
    await db_session.execute(
        sql_text("DELETE FROM clinical_events WHERE patient_id = :pid"),
        {"pid": pid},
    )
    await db_session.execute(
        sql_text("DELETE FROM folders WHERE patient_id = :pid"),
        {"pid": pid},
    )
    await db_session.execute(
        sql_text("DELETE FROM patients WHERE id = :pid"),
        {"pid": pid},
    )
    await db_session.execute(
        sql_text("DELETE FROM subjects WHERE id = :sid"),
        {"sid": sid},
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_supersede_collapse_db_paths(
    db_session: AsyncSession,
    patient_with_supersede_chain: dict,
):
    """Three DB-bound assertions packed into one test to avoid the
    pytest-asyncio / asyncpg ``event loop is closed`` regression when
    multiple async tests share the same DB fixture in one session.
    Same pattern as ``test_qna_orchestrator.test_orchestrator_free_and_standard_paths``.

    Covers:
      * ``_collapse_superseded_chain`` rewrites both 2-hop and 1-hop
        stale rows to the head and deduplicates them.
      * Citations of unrelated kinds (doc/event) pass through unchanged.
      * The ``get_event`` executor hides ``status='stale'`` rows so the
        LLM never sees them in the candidate set.
    """
    head = patient_with_supersede_chain["rc_head"]
    old = patient_with_supersede_chain["rc_old"]
    mid = patient_with_supersede_chain["rc_mid"]

    # ---- (1) collapse: three citations on the same chain → one head ----
    rewritten, rewrite_map = await _collapse_superseded_chain(
        db_session,
        citations=[
            Citation(kind="report_content", ref_id=old),
            Citation(kind="report_content", ref_id=mid),
            Citation(kind="report_content", ref_id=head),
        ],
    )
    assert len(rewritten) == 1
    assert rewritten[0].kind == "report_content"
    assert rewritten[0].ref_id == head
    assert rewrite_map == {old: head, mid: head}

    # ---- (2) unrelated kinds pass through, order preserved ----
    other_event = uuid.uuid4()
    other_doc = uuid.uuid4()
    rewritten2, rewrite_map2 = await _collapse_superseded_chain(
        db_session,
        citations=[
            Citation(kind="event", ref_id=other_event),
            Citation(kind="report_content", ref_id=old),
            Citation(kind="document", ref_id=other_doc),
        ],
    )
    assert [c.kind for c in rewritten2] == ["event", "report_content", "document"]
    assert rewritten2[0].ref_id == other_event
    assert rewritten2[1].ref_id == head
    assert rewritten2[2].ref_id == other_doc
    assert rewrite_map2 == {old: head}

    # ---- (3) get_event executor hides stale reports ----
    executors = build_executors(
        db=db_session,
        patient_id=patient_with_supersede_chain["patient_id"],
    )
    raw = await executors["get_event"]({"event_id": str(patient_with_supersede_chain["event_id"])})
    payload = json.loads(raw)
    rc_ids = [r["id"] for r in payload["report_contents"]]
    assert str(head) in rc_ids
    assert str(old) not in rc_ids
    assert str(mid) not in rc_ids


@pytest.mark.asyncio
async def test_collapse_chain_no_op_without_report_citations(db_session: AsyncSession):
    """Lists without ``report_content`` citations return unchanged.

    Independent test because it does not need the supersede-chain
    fixture, so it can run safely without contending for the same DB
    connection pool slot.
    """
    citations = [
        Citation(kind="document", ref_id=uuid.uuid4()),
        Citation(kind="event", ref_id=uuid.uuid4()),
    ]
    rewritten, rewrite_map = await _collapse_superseded_chain(db_session, citations=citations)
    assert rewritten == citations
    assert rewrite_map == {}


def test_rewrite_markers_replaces_stale_ids():
    """Inline ``[report:OLD]`` markers are rewritten to the head id.

    Mirrors :func:`_collapse_superseded_chain` on the textual side.
    Non-report markers and unmapped report markers are left alone.
    """
    old = uuid.UUID("11111111-1111-1111-1111-111111111111")
    mid = uuid.UUID("22222222-2222-2222-2222-222222222222")
    head = uuid.UUID("33333333-3333-3333-3333-333333333333")
    unrelated_doc = uuid.UUID("44444444-4444-4444-4444-444444444444")

    body = (
        f"La relazione conclusiva [report:{old}] e la sua revisione "
        f"successiva [report:{mid}] confermano il dato; vedi anche "
        f"[doc:{unrelated_doc}]."
    )
    rewrite_map = {old: head, mid: head}

    out = _rewrite_collapsed_markers(body, rewrite_map=rewrite_map)

    assert f"[report:{head}]" in out
    assert f"[report:{old}]" not in out
    assert f"[report:{mid}]" not in out
    assert f"[doc:{unrelated_doc}]" in out  # untouched


def test_rewrite_markers_empty_map_is_noop():
    body = "Body with [report:11111111-1111-1111-1111-111111111111] marker."
    assert _rewrite_collapsed_markers(body, rewrite_map={}) == body
