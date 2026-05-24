"""Q&A Patch 1 — citation metadata enrichment + quote payload parsing.

Three contracts on the citation pipeline:

1. The regex ``_CITATION_RE`` matches both plain ``[kind:UUID]`` and the
   extended ``[kind:UUID "snippet"]`` form (straight or curly quotes).
   The snippet flows onto :class:`Citation.quote`.
2. ``_enrich_citations`` populates ``title`` and ``date`` on every
   citation by joining the source table (report_contents, documents,
   clinical_events, clinical_notes, summaries, text_chunks). Misses
   keep their pre-enrich state and don't crash the pipeline.
3. ``_rewrite_collapsed_markers`` preserves the optional ``"snippet"``
   payload when rewriting a stale UUID to its supersede-chain head.

DB-bound assertions are packed into one async test to avoid the
pytest-asyncio / asyncpg ``event loop is closed`` flakiness pattern
already documented in ``test_qna_orchestrator.py``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    DEFAULT_CHUNKER_VERSION,
    ClinicalEvent,
    ClinicalNote,
    Document,
    FolderItem,
    Patient,
    ReportContent,
    Subject,
)
from bvphoenix.services.folders import get_or_create_root_folder
from bvphoenix.services.qna import (
    Citation,
    _enrich_citations,
    _parse_citation_markers,
    _rewrite_collapsed_markers,
)
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


# ---------------------------------------------------------------------------
# Pure unit tests: regex + marker rewriting (no DB)
# ---------------------------------------------------------------------------


def test_parse_citation_marker_without_quote():
    """Plain ``[kind:UUID]`` marker emits Citation with ``quote=None``."""
    rid = uuid.uuid4()
    citations = _parse_citation_markers(f"Vedi [report:{rid}].")
    assert len(citations) == 1
    assert citations[0].kind == "report_content"
    assert citations[0].ref_id == rid
    assert citations[0].quote is None


def test_parse_citation_marker_with_straight_quote():
    """``[kind:UUID "snippet"]`` populates ``Citation.quote``."""
    rid = uuid.uuid4()
    citations = _parse_citation_markers(
        f'Quattro lesioni periepatiche [report:{rid} "4 lesioni periepatiche max 6×4.5cm"].'
    )
    assert len(citations) == 1
    assert citations[0].kind == "report_content"
    assert citations[0].ref_id == rid
    assert citations[0].quote == "4 lesioni periepatiche max 6×4.5cm"


def test_parse_citation_marker_with_curly_quote():
    """Curly quotes (``“…”``) are accepted as a quote payload too.

    The Italian default keyboard layout / LLM smart-quote autoreplace
    emit curly quotes frequently. Accept both dialects without forcing
    one style on the model.
    """
    rid = uuid.uuid4()
    citations = _parse_citation_markers(f"Riferimento [doc:{rid} “estesi accumuli periepatici”]")
    assert len(citations) == 1
    assert citations[0].quote == "estesi accumuli periepatici"


def test_parse_citation_marker_with_malformed_inner_quote():
    """The model sometimes emits ``[report:UUID "snippet": more]`` with
    the closing quote in the middle. The tolerant parser must still
    recognise the marker, strip the surrounding bracket payload, and
    capture a meaningful snippet — without leaving the raw text in
    the rendered answer (which is what the user reported).
    """
    rid = uuid.UUID("5487f1c8-1087-4170-a5e9-10d1894c5bbf")
    body = (
        f'Si accenna a un "quadro restaging" con "4 lesioni periepatiche" '
        f'([report:{rid} "Quadro restaging": TC 09/03 '
        f"(4 lesioni periepatiche max 6×4.5cm)])."
    )
    citations = _parse_citation_markers(body)
    assert len(citations) == 1
    assert citations[0].ref_id == rid
    # Surrounding quote-pair stripped; embedded ``:`` and parentheses
    # survive intact. Internal whitespace collapsed to single spaces.
    assert citations[0].quote == (
        'Quadro restaging": TC 09/03 (4 lesioni periepatiche max 6×4.5cm)'
    )


def test_parse_citation_marker_accepts_long_form_kind():
    """Long-form kind prefixes (``document``, ``clinical_note``,
    ``report_content``) are accepted alongside the short ones (``doc``,
    ``note``, ``report``). Both normalise to the long form on
    :class:`Citation.kind`.

    Reproduces the second production bug: the LLM emitted
    ``[document:UUID]`` instead of ``[doc:UUID]``, and the previous
    regex only knew about the short form, leaving the marker as raw
    text in the rendered answer.
    """
    rid = uuid.UUID("4e32d6d1-a018-4d3c-adef-7feb2eb94778")
    body = f"Vedi referto patologia [document:{rid}]."
    citations = _parse_citation_markers(body)
    assert len(citations) == 1
    assert citations[0].kind == "document"
    assert citations[0].ref_id == rid


def test_parse_citation_marker_with_unquoted_payload():
    """Free-text payload without any quotes is captured verbatim
    (sanitised). Some prompts elicit this pattern from smaller
    models that drop the quoting hint."""
    rid = uuid.uuid4()
    citations = _parse_citation_markers(f"Vedi [report:{rid} lesione periepatica grande].")
    assert len(citations) == 1
    assert citations[0].quote == "lesione periepatica grande"


def test_parse_citation_marker_dedup_keeps_first_quote():
    """Two markers of the same (kind, ref) dedup to the first; quote stable."""
    rid = uuid.uuid4()
    body = f'[report:{rid} "first snippet"] then later [report:{rid} "second snippet"] again.'
    citations = _parse_citation_markers(body)
    assert len(citations) == 1
    assert citations[0].quote == "first snippet"


def test_rewrite_collapsed_markers_preserves_quote():
    """``[report:OLD "snippet"]`` rewrites to ``[report:HEAD "snippet"]``."""
    old = uuid.UUID("11111111-1111-1111-1111-111111111111")
    head = uuid.UUID("22222222-2222-2222-2222-222222222222")
    body = f'La quota è [report:{old} "lesione periepatica"] confermata.'
    out = _rewrite_collapsed_markers(body, rewrite_map={old: head})
    assert f'[report:{head} "lesione periepatica"]' in out
    assert f"[report:{old}" not in out


def test_rewrite_collapsed_markers_normalises_curly_to_straight():
    """Curly-quoted markers are rewritten with straight quotes.

    Keeps the FE-side regex simple — it only has to match one style on
    output. Both styles still parse on input.
    """
    old = uuid.UUID("11111111-1111-1111-1111-111111111111")
    head = uuid.UUID("22222222-2222-2222-2222-222222222222")
    body = f"Vedi [report:{old} “snippet curly”]."
    out = _rewrite_collapsed_markers(body, rewrite_map={old: head})
    assert f'[report:{head} "snippet curly"]' in out


# ---------------------------------------------------------------------------
# DB-bound fixture + integration test
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def patient_with_diverse_citations(db_session: AsyncSession):
    """Patient carrying every kind enrichable from a citation.

    Created entities:
      * 1 ``clinical_event``
      * 1 ``report_content`` (status=endorsed, title set, extracted_at set)
      * 1 ``document`` (title set, document_date set) + folder bookkeeping
        so the orphan-forbidden trigger from migration 0088 is satisfied
      * 1 ``clinical_note`` (body set)
      * 1 ``text_chunks`` row referencing the document

    The enrichment test then asks for one citation per kind and verifies
    each chip carries a non-empty ``title`` + ``date``.
    """
    pid = uuid.uuid4()
    sid = uuid.uuid4()
    eid = uuid.uuid4()
    rid = uuid.uuid4()
    did = uuid.uuid4()
    nid = uuid.uuid4()
    cid = uuid.uuid4()

    db_session.add(Subject(id=sid, kind="user", display_name=f"qna-meta-{sid}"))
    await db_session.flush()
    patient = Patient(id=pid, managed_by_subject_id=sid, display_name="Q&A Metadata Patient")
    db_session.add(patient)
    await db_session.flush()
    root_folder = await get_or_create_root_folder(db_session, patient)

    db_session.add(
        ClinicalEvent(
            id=eid,
            patient_id=pid,
            kind="pathology_review",
            title="Visita oncologica del 10/04/2026",
            event_date=date(2026, 4, 10),
        )
    )
    await db_session.flush()
    db_session.add(
        ReportContent(
            id=rid,
            clinical_event_id=eid,
            authority_id="derived",
            status="endorsed",
            title="Relazione conclusiva 10/04/2026",
            narrative_md="Quattro lesioni periepatiche.",
            created_by_subject_id=sid,
            author_kind="agent",
        )
    )
    doc_text = "Carcinoma duttale invasivo G2."
    db_session.add(
        Document(
            id=did,
            patient_id=pid,
            uploaded_by_subject_id=sid,
            kind_id="pathology_report",
            provenance_id="digital_native_pdf",
            authority_id="original",
            title="Anatomia patologica 23/11/2020",
            document_date=date(2020, 11, 23),
            text=doc_text,
        )
    )
    await db_session.flush()
    db_session.add(FolderItem(folder_id=root_folder.id, resource_kind="document", resource_id=did))
    db_session.add(
        ClinicalNote(
            id=nid,
            patient_id=pid,
            target_kind="patient",
            target_id=pid,
            author_subject_id=sid,
            body="Paziente collaborante, ECOG 1. Da rivalutare a 2 mesi.",
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
        "event_id": eid,
        "report_content_id": rid,
        "document_id": did,
        "note_id": nid,
        "chunk_id": cid,
    }

    await db_session.execute(
        sql_text("DELETE FROM folder_items WHERE resource_id = :did"),
        {"did": did},
    )
    await db_session.execute(
        sql_text("DELETE FROM text_chunks WHERE patient_id = :pid"),
        {"pid": pid},
    )
    await db_session.execute(
        sql_text("DELETE FROM clinical_notes WHERE patient_id = :pid"),
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


@pytest.mark.asyncio
async def test_enrich_citations_populates_title_and_date(
    db_session: AsyncSession,
    patient_with_diverse_citations: dict,
):
    """Each citation gets a human title + ISO date from its source row.

    One DB-bound test covers all enrichable kinds and a deliberately
    unknown kind (``"chunk"`` with a non-existent id) so the missing-row
    branch is exercised too — the unknown citation must pass through
    unchanged rather than crash or leak ``None`` errors upstream.
    """
    pid = patient_with_diverse_citations["patient_id"]
    citations = [
        Citation(kind="report_content", ref_id=patient_with_diverse_citations["report_content_id"]),
        Citation(kind="document", ref_id=patient_with_diverse_citations["document_id"]),
        Citation(kind="event", ref_id=patient_with_diverse_citations["event_id"]),
        Citation(kind="clinical_note", ref_id=patient_with_diverse_citations["note_id"]),
        Citation(kind="chunk", ref_id=patient_with_diverse_citations["chunk_id"]),
        # Unknown id — enrich must leave it as-is and not blow up.
        Citation(kind="document", ref_id=uuid.uuid4()),
    ]
    enriched = await _enrich_citations(db_session, patient_id=pid, citations=citations)
    assert len(enriched) == len(citations)
    # report_content
    assert enriched[0].title == "Relazione conclusiva 10/04/2026"
    assert enriched[0].date is not None  # extracted_at fallback to created_at
    # document
    assert enriched[1].title == "Anatomia patologica 23/11/2020"
    assert enriched[1].date == "2020-11-23"
    # event
    assert enriched[2].title == "Visita oncologica del 10/04/2026"
    assert enriched[2].date == "2026-04-10"
    # clinical_note — title is the first 80 chars of body
    assert enriched[3].title is not None
    assert "Paziente collaborante" in enriched[3].title
    # chunk — title is the first 80 chars of text
    assert enriched[4].title is not None
    assert "Carcinoma duttale" in enriched[4].title
    # unknown document id — falls through unchanged
    assert enriched[5].title is None
    assert enriched[5].date is None
