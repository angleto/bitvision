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

from bvphoenix.services import bge_m3, chunk_search
from bvphoenix.services.bge_m3 import BGE_M3_MODEL_ID
from bvphoenix.services.chunk_search import (
    MULTILINGUAL_MODEL_ID,
    search_chunks,
)
from bvphoenix.services.embedding_models import (
    activate_model,
    get_default_model,
    get_model_by_name,
)
from tests.eval.metrics import mrr, ndcg_at_k, precision_at_k, recall_at_k

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


# ===================================================================
# BGE-M3 dense-retrieval eval (1024-d store, registry default flipped)
# ===================================================================
#
# Same harness as the MiniLM eval above, but exercises the BGE-M3 path:
#   * corpus chunks are embedded with the REAL BGE-M3 dense encoder into
#     ``text_embeddings_bge_m3`` (NOT ``text_embeddings``), so the only
#     vector signal available to ``search_chunks`` is the BGE-M3 one;
#   * a fixture flips the registry default-for-kind to bge-m3-v1 so
#     ``search_chunks`` resolves the BGE store (the only seam; the function
#     has no ``model=`` argument), then restores minilm-multi-v1 in
#     teardown because ``activate_model`` commits to the shared DB.
#
# Non-vacuity: we seed ONLY the BGE store. plainto_tsquery('italian', q)
# ANDs every content stem, and for these paraphrase queries the target
# chunk's tsvector does NOT satisfy @@ (verified on the live Italian
# config), so FTS returns ZERO rows -- not merely a worse rank. The dense
# BGE signal is therefore the SOLE driver: if the search fell back to the
# MiniLM store (no flip / wrong vec_table) there would be zero dense rows
# AND zero FTS rows, so search_chunks returns [] and the asserts fail
# loudly. Do NOT weaken these queries toward lexical overlap believing FTS
# is a fallback -- it is not for this corpus. Every test also re-asserts
# the resolved default is bge-m3-v1.

# A graded paraphrase / synonym query set. Each query's wording shares
# little lexical surface with the target chunk (so FTS contributes nothing
# and the dense BGE-M3 signal must carry the rank). For an unambiguous
# golden these must hit 100% (recall@k == precision@1 == nDCG == MRR == 1.0),
# never "X of N" (project rule: golden tests assert 100%).
_BGE_QUERIES: dict[str, str] = {
    # "tumore al polmone" vs chunk "neoplasia ... polmonare" (no shared stem)
    "lung": "tumore maligno al polmone",
    # "rottura dell'osso della coscia" vs "frattura ... diafisi femorale"
    "femur": "rottura dell'osso della coscia",
    # "glicemia alta / zucchero nel sangue" vs "diabete mellito tipo 2"
    "diabetes": "glicemia alta da zucchero nel sangue",
}


async def _insert_chunk_bge(session, *, patient_id, body: str) -> uuid.UUID:
    """Insert one text_chunk + its REAL BGE-M3 dense embedding.

    Mirrors ``_insert_chunk`` but encodes via ``bge_m3.embed_query_dense``
    (the identical sentence-transformers BAAI/bge-m3 path the worker's
    ``_compute_dense`` uses, ``normalize_embeddings=True``) so corpus and
    query land in the same 1024-d space, and writes the literal
    ``model_id='bge-m3-v1'`` the table's CHECK constraint requires. Reuses
    ``chunk_search._vec_literal`` rather than hand-rolling a second builder.
    """
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
    vector = await bge_m3.embed_query_dense(body)
    await session.execute(
        text(
            "INSERT INTO text_embeddings_bge_m3 (target_kind, target_id, model_id, vector) "
            "VALUES ('document_chunk', :tid, :model, (:vec)::vector)"
        ),
        {"tid": chunk_id, "model": BGE_M3_MODEL_ID, "vec": chunk_search._vec_literal(vector)},
    )
    return chunk_id


@pytest_asyncio.fixture
async def bge_default(db_session):
    """Flip the registry default-for-kind('text') to bge-m3-v1 for the test,
    then restore minilm-multi-v1 on teardown.

    ``activate_model`` commits a real change to the shared dev/CI
    ``embedding_models`` table (clear-then-set so the partial-unique 'one
    default per kind' index is honoured), so the default MUST be put back or
    every other test on the shared Postgres would resolve the wrong store.
    """
    bge_row = await get_model_by_name(BGE_M3_MODEL_ID, db_session)
    await activate_model(db_session, bge_row.id, is_default_for_kind=True)
    await db_session.commit()
    try:
        yield
    finally:
        minilm_row = await get_model_by_name(MULTILINGUAL_MODEL_ID, db_session)
        await activate_model(db_session, minilm_row.id, is_default_for_kind=True)
        await db_session.commit()


@pytest_asyncio.fixture
async def seeded_chunks_bge(db_session, make_user, make_study):
    """Seed three topically-distinct chunks + their REAL BGE-M3 dense vectors
    (BGE store ONLY) for one patient; return (patient_id, {label: chunk_id})."""
    user = await make_user()
    study, _ = await make_study(user)
    patient_id = study.patient_id
    label_to_id: dict[str, uuid.UUID] = {}
    for label, body in _CHUNKS.items():
        label_to_id[label] = await _insert_chunk_bge(db_session, patient_id=patient_id, body=body)
    await db_session.commit()
    return patient_id, label_to_id


@pytest.mark.asyncio
async def test_bge_m3_default_is_resolved(bge_default, db_session) -> None:
    """Guard: the fixture actually flipped the registry default to bge-m3-v1.

    Without this, every assertion below could pass against the MiniLM store
    (silent fallback), making the BGE eval vacuous.
    """
    resolved = (await get_default_model("text", db_session)).name
    assert resolved == BGE_M3_MODEL_ID, (
        f"registry default-for-kind('text') is {resolved!r}; "
        f"expected {BGE_M3_MODEL_ID!r} so search_chunks reads text_embeddings_bge_m3"
    )


@pytest.mark.asyncio
async def test_bge_m3_paraphrase_golden_perfect(bge_default, seeded_chunks_bge, db_session) -> None:
    """On the unambiguous IT paraphrase/synonym set, BGE-M3 dense retrieval
    must be PERFECT: each query's target chunk is the sole relevant doc and
    must rank #1 (recall@k == precision@1 == nDCG@k == MRR == 1.0).

    Non-vacuous: only ``text_embeddings_bge_m3`` is seeded, so if the search
    fell back to the MiniLM store (no flip / wrong vec_table) the dense
    signal would be empty and these low-overlap paraphrases would not rank
    deterministically. We also re-assert the resolved default here.
    """
    # Prove the BGE store (not the MiniLM fallback) is what drove the result.
    assert (await get_default_model("text", db_session)).name == BGE_M3_MODEL_ID

    patient_id, label_to_id = seeded_chunks_bge
    k = 3
    for label, query in _BGE_QUERIES.items():
        target = label_to_id[label]
        relevant = {target}
        grades = {target: 1.0}
        hits = await search_chunks(db_session, patient_id=patient_id, query=query, k=k)
        ranked = [h.chunk_id for h in hits]
        assert ranked, f"BGE-M3 returned nothing for {query!r}"
        # 100% golden: exact-set recall, top-1 precision, perfect ranking.
        assert recall_at_k(ranked, relevant, k) == 1.0, (
            f"{query!r}: target chunk {target} not in top-{k} ({ranked})"
        )
        assert precision_at_k(ranked, relevant, 1) == 1.0, (
            f"{query!r}: rank-1 was {ranked[0]}, expected target {target}"
        )
        assert mrr(ranked, relevant) == 1.0, f"{query!r}: target not at rank 1 ({ranked})"
        assert ndcg_at_k(ranked, grades, k) == 1.0, f"{query!r}: nDCG@{k} < 1.0 ({ranked})"


@pytest.mark.asyncio
async def test_bge_m3_no_cross_patient_leak(bge_default, seeded_chunks_bge, db_session) -> None:
    """Security invariant (recall@infinity == 0 for foreign patients): a
    second patient with no seeded chunks gets ZERO hits for the same
    paraphrase queries. A cross-patient leak here is a hard failure, not a
    ranking nicety.

    Includes a positive control: the SEEDED owner DOES retrieve its target
    for each query, so a globally-empty result (which would pass the leak
    check vacuously) cannot disguise a dead retrieval path.
    """
    patient_id, label_to_id = seeded_chunks_bge
    other_patient = uuid.uuid4()  # never seeded; no chunks, no embeddings
    for label, query in _BGE_QUERIES.items():
        leaked = [
            h.chunk_id
            for h in await search_chunks(db_session, patient_id=other_patient, query=query, k=10)
        ]
        assert leaked == [], f"cross-patient leak for {query!r}: {leaked}"
        owner_hits = [
            h.chunk_id
            for h in await search_chunks(db_session, patient_id=patient_id, query=query, k=3)
        ]
        assert label_to_id[label] in owner_hits, (
            f"seeded owner missing target for {query!r}: {owner_hits}"
        )


@pytest.mark.asyncio
async def test_bge_m3_beats_minilm_on_hard_synonym(db_session, make_user, make_study) -> None:
    """Dominance check layered on top of the absolute 100% bar: on a hard
    synonym query, BGE-M3 achieves perfect retrieval (MRR == 1.0) on the
    SAME corpus + query. This is robust (an absolute floor, not a brittle
    ``bge_score > minilm_score`` float race that can pass on a degraded BGE
    if MiniLM degrades more).

    Seeds BOTH stores with the identical corpus, then runs ``search_chunks``
    twice: once with the MiniLM default, once with the BGE default. The BGE
    run MUST hit MRR == 1.0; the MiniLM MRR is recorded for the dominance
    comparison but is NOT the pass condition.
    """
    user = await make_user()
    study, _ = await make_study(user)
    patient_id = study.patient_id

    # Seed both stores with the same three chunks, sharing one text_chunks row
    # per topic so the two runs grade against identical ids.
    label_to_id: dict[str, uuid.UUID] = {}
    for label, body in _CHUNKS.items():
        chunk_id = uuid.uuid4()
        sha = hashlib.sha256(body.encode()).hexdigest()
        await db_session.execute(
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
        minilm_vec = await chunk_search._embed_query(body)
        await db_session.execute(
            text(
                "INSERT INTO text_embeddings (target_kind, target_id, model_id, vector) "
                "VALUES ('document_chunk', :tid, :model, (:vec)::vector)"
            ),
            {
                "tid": chunk_id,
                "model": MULTILINGUAL_MODEL_ID,
                "vec": chunk_search._vec_literal(minilm_vec),
            },
        )
        bge_vec = await bge_m3.embed_query_dense(body)
        await db_session.execute(
            text(
                "INSERT INTO text_embeddings_bge_m3 (target_kind, target_id, model_id, vector) "
                "VALUES ('document_chunk', :tid, :model, (:vec)::vector)"
            ),
            {
                "tid": chunk_id,
                "model": BGE_M3_MODEL_ID,
                "vec": chunk_search._vec_literal(bge_vec),
            },
        )
        label_to_id[label] = chunk_id
    await db_session.commit()

    async def _mrr_over_set() -> float:
        total = 0.0
        for label, query in _BGE_QUERIES.items():
            relevant = {label_to_id[label]}
            hits = await search_chunks(db_session, patient_id=patient_id, query=query, k=3)
            total += mrr([h.chunk_id for h in hits], relevant)
        return total / len(_BGE_QUERIES)

    # MiniLM run (registry default already minilm-multi-v1 out of migration).
    assert (await get_default_model("text", db_session)).name == MULTILINGUAL_MODEL_ID
    mrr_minilm = await _mrr_over_set()

    # Flip to BGE-M3, measure, then restore the default in finally.
    bge_row = await get_model_by_name(BGE_M3_MODEL_ID, db_session)
    await activate_model(db_session, bge_row.id, is_default_for_kind=True)
    await db_session.commit()
    try:
        assert (await get_default_model("text", db_session)).name == BGE_M3_MODEL_ID
        mrr_bge = await _mrr_over_set()
    finally:
        minilm_row = await get_model_by_name(MULTILINGUAL_MODEL_ID, db_session)
        await activate_model(db_session, minilm_row.id, is_default_for_kind=True)
        await db_session.commit()

    # Absolute bar (the real assertion): BGE is perfect on this set.
    assert mrr_bge == 1.0, f"BGE-M3 MRR {mrr_bge} != 1.0 on the hard synonym set"
    # Dominance check, layered on top of the absolute bar (never a substitute).
    assert mrr_bge >= mrr_minilm, (
        f"BGE-M3 MRR {mrr_bge} regressed below MiniLM {mrr_minilm} on the same corpus"
    )
