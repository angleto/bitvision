"""Cross-patient leakage CI gate for the Q&A path.

The orchestrator binds ``patient_id`` server-side in every tool
executor; the model never sees a patient identifier. This file
asserts the invariant *empirically*: build two patients (A and B)
whose records contain near-identical histology chunks, run the
orchestrator scoped to A, verify that not one of the returned
citations / tool results references B.

Two layers are exercised:

* ``services.chunk_search.search_chunks`` — the lowest-level
  retrieval primitive. Bound to ``patient_id=A``, it must never
  surface a chunk owned by patient B even when the textual query
  matches B's content.
* ``services.qna.answer_question`` — the orchestrator on the free
  tier (no LLM, deterministic output). Citations array must contain
  ONLY refs to A's documents.
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
    Document,
    FolderItem,
    Patient,
    Subject,
)
from bvphoenix.services.ai_tiers import AiTier
from bvphoenix.services.chunk_search import search_chunks
from bvphoenix.services.folders import get_or_create_root_folder
from bvphoenix.services.qna import answer_question
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db

SHARED_TEXT = (
    "Carcinoma duttale invasivo della mammella. Stadiazione pT2N1 G2. Recettori ormonali positivi."
)


@pytest_asyncio.fixture
async def two_patients_same_histology(db_session: AsyncSession):
    """Build two patients with near-identical pathology reports.

    Both reports carry the same SHARED_TEXT body, so any FTS or
    embedding search would happily return either if patient scoping
    were broken. The documents and chunks are inserted with each
    patient's UUID denormalised into the chunk row, exactly mirroring
    the worker's persistence path.
    """
    pid_a = uuid.uuid4()
    pid_b = uuid.uuid4()
    sid = uuid.uuid4()
    did_a = uuid.uuid4()
    did_b = uuid.uuid4()
    cid_a = uuid.uuid4()
    cid_b = uuid.uuid4()

    db_session.add(Subject(id=sid, kind="user", display_name=f"sec-{sid}"))
    await db_session.flush()
    pat_a = Patient(id=pid_a, managed_by_subject_id=sid, display_name="Patient A")
    pat_b = Patient(id=pid_b, managed_by_subject_id=sid, display_name="Patient B")
    db_session.add(pat_a)
    db_session.add(pat_b)
    await db_session.flush()

    folder_a = await get_or_create_root_folder(db_session, pat_a)
    folder_b = await get_or_create_root_folder(db_session, pat_b)

    for did, pid in [(did_a, pid_a), (did_b, pid_b)]:
        db_session.add(
            Document(
                id=did,
                patient_id=pid,
                uploaded_by_subject_id=sid,
                kind_id="pathology_report",
                provenance_id="digital_native_pdf",
                authority_id="original",
                title="Referto anatomopatologico",
                text=SHARED_TEXT,
            )
        )
    await db_session.flush()
    db_session.add(FolderItem(folder_id=folder_a.id, resource_kind="document", resource_id=did_a))
    db_session.add(FolderItem(folder_id=folder_b.id, resource_kind="document", resource_id=did_b))
    await db_session.flush()

    sha = hashlib.sha256(SHARED_TEXT.encode("utf-8")).hexdigest()
    for cid, did, pid in [(cid_a, did_a, pid_a), (cid_b, did_b, pid_b)]:
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
                "end": len(SHARED_TEXT),
                "body": SHARED_TEXT,
                "sha": sha,
            },
        )
    await db_session.commit()

    yield {
        "patient_a": pid_a,
        "patient_b": pid_b,
        "subject_id": sid,
        "doc_a": did_a,
        "doc_b": did_b,
        "chunk_a": cid_a,
        "chunk_b": cid_b,
    }

    for pid in (pid_a, pid_b):
        await db_session.execute(
            sql_text(
                "DELETE FROM folder_items WHERE folder_id IN ("
                "SELECT id FROM folders WHERE patient_id = :pid)"
            ),
            {"pid": pid},
        )
        await db_session.execute(
            sql_text("DELETE FROM text_chunks WHERE patient_id = :pid"),
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
# Layer 1: chunk_search service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qna_path_is_strictly_patient_scoped_end_to_end(
    db_session: AsyncSession, two_patients_same_histology: dict
):
    """End-to-end cross-patient isolation: one fixture, both layers.

    Single test instead of three because pytest-asyncio (auto mode)
    plus asyncpg's connection pool stops cooperating across tests
    that share a heavyweight fixture in this codebase. Folding
    everything into one body keeps the gate sharp without fighting
    the harness — the assertions remain independent.

    Layer 1 (chunk_search): bound to A excludes B's chunk; bound to
    B excludes A's. Layer 2 (orchestrator free tier): scoped to A,
    citations contain ONLY A's documents and the markdown body never
    surfaces B's ids.
    """
    fixture = two_patients_same_histology

    # ---- Layer 1: chunk_search both ways ----
    hits_a = await search_chunks(
        db_session,
        patient_id=fixture["patient_a"],
        query="carcinoma duttale",
        k=8,
    )
    chunk_ids_a = {h.chunk_id for h in hits_a}
    source_ids_a = {h.source_id for h in hits_a}
    assert fixture["chunk_b"] not in chunk_ids_a
    assert fixture["doc_b"] not in source_ids_a
    if hits_a:
        assert all(h.source_kind == "document" for h in hits_a)

    hits_b = await search_chunks(
        db_session,
        patient_id=fixture["patient_b"],
        query="carcinoma duttale",
        k=8,
    )
    chunk_ids_b = {h.chunk_id for h in hits_b}
    source_ids_b = {h.source_id for h in hits_b}
    assert fixture["chunk_a"] not in chunk_ids_b
    assert fixture["doc_a"] not in source_ids_b

    # ---- Layer 2: orchestrator free-tier scoped to A ----
    result = await answer_question(
        db_session,
        patient_id=fixture["patient_a"],
        query="carcinoma duttale",
        lang="it",
        user_subject_id=fixture["subject_id"],
        tier_override=AiTier.FREE,
    )
    cited_ref_ids = {str(c.ref_id) for c in result.citations}
    assert str(fixture["doc_b"]) not in cited_ref_ids
    assert str(fixture["chunk_b"]) not in cited_ref_ids
    # Belt and suspenders: the markdown body must not contain B's ids
    # either, since the FE renders both forms (citations + markdown).
    assert str(fixture["doc_b"]) not in result.answer_md
    assert str(fixture["chunk_b"]) not in result.answer_md
