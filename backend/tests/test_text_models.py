"""Unit guards for the registry-backed text-model routing.

The routing (model -> arq task + pgvector store tables) lives in
``embedding_models.model_metadata`` (migration 0023); a desync between
that seed and the actual stores (wrong table, wrong vector width) would
silently route the backfill CLI, the query path (chunk_search), the
worker dual-write loop and the admin API to the wrong place. These
ungated, no-DB tests anchor the migration's literals to the ORM and
exercise the parser/validator that every consumer goes through.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from bvphoenix.db.models import (
    TextEmbedding,
    TextEmbeddingBgeM3,
    TextEmbeddingBgeM3Colbert,
    TextEmbeddingBgeM3Sparse,
)
from bvphoenix.db.models.text_embeddings import TEXT_EMBEDDING_DIM
from bvphoenix.db.models.text_embeddings_bge_m3 import BGE_M3_DENSE_DIM, BGE_M3_SPARSE_DIM
from bvphoenix.services.text_models import (
    BGE_M3_MODEL_ID,
    MULTILINGUAL_MODEL_ID,
    spec_from_registry,
)


def _load_migration_routing() -> dict[str, dict[str, str]]:
    """Import ``TEXT_ROUTING`` from migration 0023 by file path.

    Alembic version modules are not on the import path; loading the file
    directly lets the test anchor the seeded literals without copying
    them (a copy would be exactly the drift this guard exists to catch).
    """
    path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0023_text_model_routing.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0023", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TEXT_ROUTING


TEXT_ROUTING = _load_migration_routing()


def test_migration_seeds_the_shipped_models() -> None:
    # The model ids the query-path encoder dispatch is keyed on must be
    # exactly the rows the migration routes.
    assert set(TEXT_ROUTING) == {MULTILINGUAL_MODEL_ID, BGE_M3_MODEL_ID}


def test_minilm_routing_matches_orm() -> None:
    spec = spec_from_registry(
        MULTILINGUAL_MODEL_ID, TEXT_EMBEDDING_DIM, TEXT_ROUTING[MULTILINGUAL_MODEL_ID]
    )
    assert spec is not None
    assert spec.model_id == MULTILINGUAL_MODEL_ID
    assert spec.arq_task == "embed_text_ml"
    assert spec.store_table == TextEmbedding.__tablename__ == "text_embeddings"
    assert spec.dim == TEXT_EMBEDDING_DIM == 384
    # A registry flip-back to MiniLM (dense-only) must disable the sparse +
    # ColBERT arms; chunk_search keys those off these being None.
    assert spec.sparse_store_table is None
    assert spec.colbert_store_table is None


def test_bge_m3_routing_matches_orm() -> None:
    spec = spec_from_registry(BGE_M3_MODEL_ID, BGE_M3_DENSE_DIM, TEXT_ROUTING[BGE_M3_MODEL_ID])
    assert spec is not None
    # embed_bge_m3_all: one FlagEmbedding forward -> dense + sparse + colbert.
    assert spec.arq_task == "embed_bge_m3_all"
    # Store tables + vector width are anchored to the ORM models / migration,
    # so editing the seed without the column (or vice versa) fails here.
    assert spec.store_table == TextEmbeddingBgeM3.__tablename__ == "text_embeddings_bge_m3"
    assert spec.sparse_store_table == TextEmbeddingBgeM3Sparse.__tablename__
    assert spec.colbert_store_table == TextEmbeddingBgeM3Colbert.__tablename__
    assert spec.dim == BGE_M3_DENSE_DIM == 1024
    assert BGE_M3_SPARSE_DIM == 250002


def test_unrouted_row_returns_none() -> None:
    # The dormant biomedclip-text-v1 row (and any pre-0023 metadata) has no
    # routing keys: consumers must SKIP it, not crash on it.
    assert spec_from_registry("biomedclip-text-v1", 512, {}) is None
    assert spec_from_registry("biomedclip-text-v1", 512, None) is None
    assert spec_from_registry("bge-m3-v1", 1024, {"dense_dim": 1024, "sparse": True}) is None


def test_partial_routing_raises() -> None:
    # Half a route is an operator mistake that must surface, not silently
    # behave like "no route".
    with pytest.raises(ValueError, match="routing requires both"):
        spec_from_registry("m", 384, {"arq_task": "embed_text_ml"})
    with pytest.raises(ValueError, match="routing requires both"):
        spec_from_registry("m", 384, {"store_table": "text_embeddings"})


@pytest.mark.parametrize(
    "bad",
    [
        "text_embeddings; DROP TABLE patients",
        "text_embeddings--",
        'text_embeddings"',
        "Text_Embeddings",
        "1text",
        "",
        None,
        42,
    ],
)
def test_malformed_identifiers_are_rejected(bad) -> None:
    # Routing values are f-string-interpolated into SQL by every consumer;
    # anything that is not a plain lowercase identifier must never parse.
    with pytest.raises(ValueError, match="not a plain lowercase identifier"):
        spec_from_registry("m", 384, {"arq_task": "embed_text_ml", "store_table": bad})


def test_malformed_aux_store_is_rejected() -> None:
    # Aux stores are optional (None == absent) but, when present, ride the
    # same identifier guard as the dense store.
    with pytest.raises(ValueError, match="not a plain lowercase identifier"):
        spec_from_registry(
            "m",
            384,
            {
                "arq_task": "embed_text_ml",
                "store_table": "text_embeddings",
                "sparse_store_table": "x; DELETE FROM users",
            },
        )
