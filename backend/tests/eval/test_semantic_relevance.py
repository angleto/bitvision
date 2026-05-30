"""ai-gated semantic relevance eval (dense retrieval).

Completes the eval harness: where ``test_relevance.py`` validates the
LEXICAL (FTS) path model-free, this validates the DENSE path end-to-end
with the REAL multilingual MiniLM model. It seeds a patient's text chunks
plus their actual embeddings, then asserts that a *paraphrased*,
low-lexical-overlap query retrieves the semantically-matching chunk over
unrelated distractors — something pure FTS cannot do.

Gated twice so it never runs by accident:
* ``importorskip('sentence_transformers')`` — only with the ``ai`` extra
  (torch + sentence-transformers, ~600 MB), so the fast lexical
  ``search-eval`` CI gate stays model-free.
* ``BVP_RUN_SEARCH_INTEGRATION=1`` + a migrated Postgres.

Runs in the dedicated ``search-eval-ai`` CI job (tag releases).
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
import pytest_asyncio

pytest.importorskip("sentence_transformers")

from sqlalchemy import text

from bvphoenix.services import chunk_search
from bvphoenix.services.chunk_search import (
    MULTILINGUAL_MODEL_ID,
    search_chunks,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("BVP_RUN_SEARCH_INTEGRATION") != "1",
    reason="semantic eval needs a migrated Postgres; opt in via BVP_RUN_SEARCH_INTEGRATION=1",
)

# Three clinically distinct topics so the nearest chunk to a paraphrase is
# unambiguous (robust against the model's modest absolute recall).
_CHUNKS: dict[str, str] = {
    "lung": (
        "Riscontro di neoplasia maligna del lobo polmonare superiore destro, "
        "con aspetti francamente sospetti per malignità."
    ),
    "femur": (
        "Frattura composta della diafisi femorale sinistra, trattata con "
        "osteosintesi mediante placca e viti."
    ),
    "diabetes": (
        "Diabete mellito di tipo 2 in discreto compenso glicemico, in terapia "
        "con metformina e dieta ipoglucidica."
    ),
}
# Lung-cancer query that shares little lexical surface with the lung chunk
# ("cancro"/"polmoni" vs "neoplasia"/"polmonare"): FTS alone would struggle,
# so a correct top-1 hit is driven by the dense embedding.
_SEMANTIC_QUERY = "cancro ai polmoni"
_EXPECTED_LABEL = "lung"


async def _insert_chunk(session, *, patient_id, body: str) -> uuid.UUID:
    """Insert one text_chunk + its real multilingual embedding."""
    chunk_id = uuid.uuid4()
    sha = hashlib.sha256(body.encode()).hexdigest()
    await session.execute(
        text(
            "INSERT INTO text_chunks "
            "(id, source_kind, source_id, patient_id, author_kind, chunker_version, "
            " char_start, char_end, text, content_sha256) "
            "VALUES (:id, 'document', :sid, :pid, 'human', :cv, 0, :ce, :txt, :sha)"
        ),
        {
            "id": chunk_id,
            "sid": uuid.uuid4(),
            "pid": patient_id,
            "cv": chunk_search.DEFAULT_CHUNKER_VERSION,
            "ce": len(body),
            "txt": body,
            "sha": sha,
        },
    )
    vector = await chunk_search._embed_query(body)
    await session.execute(
        text(
            "INSERT INTO text_embeddings (target_kind, target_id, model_id, vector) "
            "VALUES ('document_chunk', :tid, :model, (:vec)::vector)"
        ),
        {"tid": chunk_id, "model": MULTILINGUAL_MODEL_ID, "vec": chunk_search._vec_literal(vector)},
    )
    return chunk_id


@pytest_asyncio.fixture
async def seeded_chunks(db_session, make_user, make_study):
    """Seed three topically-distinct chunks (+ real embeddings) for one
    patient; return (patient_id, {label: chunk_id})."""
    user = await make_user()
    study, _ = await make_study(user)
    patient_id = study.patient_id
    label_to_id: dict[str, uuid.UUID] = {}
    for label, body in _CHUNKS.items():
        label_to_id[label] = await _insert_chunk(db_session, patient_id=patient_id, body=body)
    await db_session.commit()
    return patient_id, label_to_id


@pytest.mark.asyncio
async def test_semantic_chunk_retrieval(seeded_chunks, db_session) -> None:
    """A paraphrased lung-cancer query ranks the lung chunk first via the
    dense signal, over the unrelated femur / diabetes chunks."""
    patient_id, label_to_id = seeded_chunks
    hits = await search_chunks(db_session, patient_id=patient_id, query=_SEMANTIC_QUERY, k=3)
    assert hits, "dense retrieval returned nothing"
    assert str(hits[0].chunk_id) == str(label_to_id[_EXPECTED_LABEL]), (
        f"semantic query {_SEMANTIC_QUERY!r} ranked {hits[0].chunk_id} first; "
        f"expected the lung chunk {label_to_id[_EXPECTED_LABEL]}"
    )


@pytest.mark.asyncio
async def test_rerank_path_runs(seeded_chunks, db_session) -> None:
    """``rerank=True`` loads + runs the cross-encoder without error and
    still returns results. (We assert the path runs, not a specific order:
    the English cross-encoder's Italian quality is out of scope here.)"""
    patient_id, _ = seeded_chunks
    hits = await search_chunks(
        db_session, patient_id=patient_id, query=_SEMANTIC_QUERY, k=3, rerank=True
    )
    assert hits, "rerank path returned nothing"
