"""Unit guards for the shared text-model routing spec.

A desync between ``TEXT_MODELS`` and the actual pgvector store (wrong store
table, wrong vector width) would silently route the backfill CLI, the
query path (chunk_search) and the write path (chunk_and_embed) to the
wrong place. These ungated, no-DB tests run in the normal CI pytest and
fail loudly on drift -- the cheap guard the reviewer asked for when the
mapping was unified out of four hand-synced copies.
"""

from __future__ import annotations

from bvphoenix.db.models import (
    TextEmbeddingBgeM3,
    TextEmbeddingBgeM3Colbert,
    TextEmbeddingBgeM3Sparse,
)
from bvphoenix.db.models.text_embeddings_bge_m3 import BGE_M3_DENSE_DIM, BGE_M3_SPARSE_DIM
from bvphoenix.services.text_models import (
    BGE_M3_MODEL_ID,
    DEFAULT_TEXT_MODEL_ID,
    MULTILINGUAL_MODEL_ID,
    TEXT_MODELS,
)


def test_keys_and_self_consistency() -> None:
    assert set(TEXT_MODELS) == {MULTILINGUAL_MODEL_ID, BGE_M3_MODEL_ID}
    # The dict key must equal the spec's own model_id (== worker MODEL_ID
    # == the value pinned by the store table's CHECK constraint).
    for model_id, spec in TEXT_MODELS.items():
        assert spec.model_id == model_id


def test_minilm_routing() -> None:
    spec = TEXT_MODELS[MULTILINGUAL_MODEL_ID]
    assert spec.arq_task == "embed_text_ml"
    assert spec.store_table == "text_embeddings"
    assert spec.dim == 384


def test_bge_m3_routing_matches_orm() -> None:
    spec = TEXT_MODELS[BGE_M3_MODEL_ID]
    # embed_bge_m3_all: one FlagEmbedding forward -> dense + sparse + colbert.
    assert spec.arq_task == "embed_bge_m3_all"
    # Store tables + vector width are anchored to the ORM models / migration,
    # so editing the spec without the column (or vice versa) fails here.
    assert spec.store_table == TextEmbeddingBgeM3.__tablename__ == "text_embeddings_bge_m3"
    assert spec.sparse_store_table == TextEmbeddingBgeM3Sparse.__tablename__
    assert spec.colbert_store_table == TextEmbeddingBgeM3Colbert.__tablename__
    assert spec.dim == BGE_M3_DENSE_DIM == 1024
    assert BGE_M3_SPARSE_DIM == 250002


def test_minilm_has_no_aux_stores() -> None:
    # A registry flip-back to MiniLM (dense-only) must disable the sparse +
    # ColBERT arms; chunk_search keys those off these being None.
    spec = TEXT_MODELS[MULTILINGUAL_MODEL_ID]
    assert spec.sparse_store_table is None
    assert spec.colbert_store_table is None


def test_default_is_minilm() -> None:
    # Search falls back to MiniLM when the registry default cannot be read;
    # that fallback model must exist in the spec.
    assert DEFAULT_TEXT_MODEL_ID == MULTILINGUAL_MODEL_ID
    assert DEFAULT_TEXT_MODEL_ID in TEXT_MODELS
