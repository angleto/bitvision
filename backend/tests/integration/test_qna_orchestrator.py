"""Golden integration tests for the Q&A orchestrator.

Exercises the end-to-end path with the deterministic :class:`StubLLM`
provider so the test never spends real tokens. The stub's agentic
behaviour is well-defined: first turn ⇒ ``tool_use`` of the first
declared tool, second turn ⇒ ``end_turn``. The orchestrator drives
the loop accordingly.

We deliberately stay in the ``free`` tier here so no LLM is needed
at all — the free path returns deterministic chunk hits formatted in
markdown — and use the shared ``answer_question`` entry point. The
chunk_search dependency on a live MiniLM model is sidestepped by
pointing the embedding to a fake stored row built directly in
``text_embeddings``; for FREE tier we skip the LLM but the search is
exercised via the chunk excerpt path.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    DEFAULT_CHUNKER_VERSION,
    ClinicalEvent,
    Document,
    FolderItem,
    Patient,
    Subject,
)
from bvphoenix.services.ai_tiers import AiTier
from bvphoenix.services.folders import get_or_create_root_folder
from bvphoenix.services.qna import answer_question
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db

# ---------------------------------------------------------------------------
# Fixture: lightweight patient with one document + one event + one chunk
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def patient_with_chunk(db_session: AsyncSession):
    """Build a tiny synthetic record: one patient, one document with a
    matching ``text_chunks`` row, one ``clinical_events`` row.

    Keeps the shape close to a real fascicolo (composite FK targets,
    document_kind_id present, etag etc.) so the executors that the
    orchestrator hands to the model see realistic data shapes.
    """
    pid = uuid.uuid4()
    sid = uuid.uuid4()
    did = uuid.uuid4()
    eid = uuid.uuid4()
    cid = uuid.uuid4()

    db_session.add(Subject(id=sid, kind="user", display_name=f"qna-test-{sid}"))
    await db_session.flush()
    patient = Patient(
        id=pid,
        managed_by_subject_id=sid,
        display_name="Q&A Patient",
    )
    db_session.add(patient)
    await db_session.flush()
    # Documents have an "orphan forbidden" trigger (migration 0088): the
    # row must be in at least one folder before commit. Create the
    # patient root folder + a folder_items entry so the trigger is
    # satisfied.
    root_folder = await get_or_create_root_folder(db_session, patient)

    doc_text = "Carcinoma duttale invasivo della mammella destra. Stadiazione pT2N1 G2."
    db_session.add(
        Document(
            id=did,
            patient_id=pid,
            uploaded_by_subject_id=sid,
            kind_id="pathology_report",
            provenance_id="digital_native_pdf",
            authority_id="original",
            title="Referto anatomopatologico",
            text=doc_text,
        )
    )
    await db_session.flush()
    db_session.add(
        FolderItem(
            folder_id=root_folder.id,
            resource_kind="document",
            resource_id=did,
        )
    )
    db_session.add(
        ClinicalEvent(
            id=eid,
            patient_id=pid,
            kind="pathology_review",
            title="Anatomopatologico",
        )
    )
    await db_session.flush()

    sha = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
    await db_session.execute(
        sql_text(
            """
            INSERT INTO text_chunks (
                id, source_kind, source_id, patient_id, author_kind,
                authority_id, document_kind_id, chunker_version,
                page, char_start, char_end, text, content_sha256
            ) VALUES (
                :id, 'document', :source_id, :patient_id, 'human',
                'original', 'pathology_report', :ver,
                NULL, 0, :end, :body, :sha
            )
            """
        ),
        {
            "id": cid,
            "source_id": did,
            "patient_id": pid,
            "ver": DEFAULT_CHUNKER_VERSION,
            "end": len(doc_text),
            "body": doc_text,
            "sha": sha,
        },
    )
    await db_session.commit()

    yield {
        "patient_id": pid,
        "subject_id": sid,
        "document_id": did,
        "event_id": eid,
        "chunk_id": cid,
        "doc_text": doc_text,
    }

    # Teardown — order matters: drop folder_items first to free the
    # orphan-forbidden trigger before deleting documents, then chunks,
    # events, documents, patient (cascades to folders), subject.
    await db_session.execute(
        sql_text("DELETE FROM folder_items WHERE resource_id = :did"),
        {"did": did},
    )
    await db_session.execute(
        sql_text("DELETE FROM text_chunks WHERE patient_id = :pid"),
        {"pid": pid},
    )
    await db_session.execute(
        sql_text("DELETE FROM clinical_events WHERE patient_id = :pid"),
        {"pid": pid},
    )
    await db_session.execute(
        sql_text("DELETE FROM documents WHERE patient_id = :pid"),
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_free_and_standard_paths(
    db_session: AsyncSession, patient_with_chunk: dict
):
    """End-to-end smoke covering both deterministic tier paths.

    Single test reusing one fixture instance because pytest-asyncio in
    auto mode + asyncpg connection pool get unhappy when the fixture
    is rebuilt between tests. Both branches share the same patient.

    Free tier: skips the LLM, returns chunk hits as markdown with
    ``[doc:UUID]`` markers and a structured citation list.

    Standard tier: with no real LLM keys configured the resolver
    degrades to ``StubLLM``, which calls the first registered tool
    once and ends the turn. The trace must record exactly that.
    """
    # ---- free tier ----
    free_result = await answer_question(
        db_session,
        patient_id=patient_with_chunk["patient_id"],
        query="carcinoma duttale",
        lang="it",
        user_subject_id=None,
        tier_override=AiTier.FREE,
    )
    assert free_result.tier is AiTier.FREE
    assert free_result.iterations == 0
    assert free_result.stop_reason == "end_turn"
    assert str(patient_with_chunk["document_id"]) in free_result.answer_md
    assert any(
        c.kind == "document" and str(c.ref_id) == str(patient_with_chunk["document_id"])
        for c in free_result.citations
    )

    # ---- standard tier (degrades to StubLLM in this env) ----
    standard_result = await answer_question(
        db_session,
        patient_id=patient_with_chunk["patient_id"],
        query="quali eventi clinici?",
        lang="it",
        user_subject_id=patient_with_chunk["subject_id"],
        tier_override=AiTier.STANDARD,
    )
    assert standard_result.tier is AiTier.STANDARD
    assert standard_result.iterations >= 1
    assert standard_result.stop_reason == "end_turn"
    assert "find_clinical_events" in standard_result.used_tools
    assert all(tc.is_error is False for tc in standard_result.tool_calls)
