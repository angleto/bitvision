"""Canonical routing table for text-chunk embedding models.

Single source of truth for the fact that maps a registry text model (by
``model_id`` -- equal to the worker ``MODEL_ID`` and to the value written
into the store's ``model_id`` column) to (a) the arq task that produces
its vectors and (b) the pgvector store table those vectors live in.

Before this module the same fact was hand-duplicated across the read path
(``services/chunk_search.py``), the write path (workers
``chunk_and_embed.py``), the backfill CLI, and the admin coverage API,
with a "keep these byte-identical" comment. The admin copy had already
drifted (MiniLM-only after the BGE-M3 rollout), which is exactly the
coherence tax the no-duplication rule exists to prevent. Import
``TEXT_MODELS`` everywhere instead.

Data-only on purpose (no model weights, no encoder callables) so the lean
CLI and the worker can import it without dragging ``sentence-transformers``
into import time. The query-time encoder dispatch stays in
``chunk_search`` (which owns the lazy ``ai``-extra import).

Longer term this should be backed by the ``embedding_models`` registry row
(``store_table`` / ``arq_task`` in ``model_metadata`` JSONB) so routing is
fully data-driven from the row ``get_default_model`` already loads; this
in-code table is the intermediate that version can replace.
"""

from __future__ import annotations

from dataclasses import dataclass

# Registry model_id values (== worker MODEL_ID == the stored model_id
# column == the value pinned by each store table's CHECK constraint).
MULTILINGUAL_MODEL_ID = "minilm-multi-v1"
BGE_M3_MODEL_ID = "bge-m3-v1"


@dataclass(frozen=True)
class TextModelSpec:
    """How to produce, and where to store, one text model's chunk vectors."""

    model_id: str
    arq_task: str
    store_table: str
    dim: int


# One entry per text embedding model. ``dim`` must match the pgvector
# column width in the store table's migration (guarded by a unit test in
# tests/test_text_models.py so a future edit cannot silently desync).
TEXT_MODELS: dict[str, TextModelSpec] = {
    MULTILINGUAL_MODEL_ID: TextModelSpec(
        model_id=MULTILINGUAL_MODEL_ID,
        arq_task="embed_text_ml",
        store_table="text_embeddings",
        dim=384,
    ),
    BGE_M3_MODEL_ID: TextModelSpec(
        model_id=BGE_M3_MODEL_ID,
        arq_task="embed_bge_m3_dense",
        store_table="text_embeddings_bge_m3",
        dim=1024,
    ),
}

# What the query path resolves to when the registry default cannot be read.
DEFAULT_TEXT_MODEL_ID = MULTILINGUAL_MODEL_ID
