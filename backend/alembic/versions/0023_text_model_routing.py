"""Seed text-model routing into the embedding_models registry rows.

Moves the model_id -> (arq_task, store_table[, sparse/colbert stores])
routing fact from the in-code ``services/text_models.TEXT_MODELS`` map
(commit 866899b's intermediate) into ``model_metadata`` JSONB on the
registry row itself, so the query path (which already loads the row via
``get_default_model``), the backfill CLI, the worker dual-write loop and
the admin coverage/embed-missing endpoints all read routing from data.
``dim`` stays the row's own column.

``TEXT_ROUTING`` is module-level so the unit guard in
``tests/test_text_models.py`` can anchor these literals to the ORM store
tables / vector widths -- the same drift protection the in-code map had.

Idempotent: ``model_metadata || ...`` converges on re-run; rows absent in
a deployment are simply not touched.

Revision ID: 0023_text_model_routing
Revises: 0022_segmentations_provenance
Create Date: 2026-06-12
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

revision = "0023_text_model_routing"
down_revision = "0022_segmentations_provenance"
branch_labels = None
depends_on = None


# Routing facts as shipped today. embed_bge_m3_all: one FlagEmbedding
# forward writes dense + sparse + colbert in one txn (degrades to
# sentence-transformers dense-only when FlagEmbedding is unavailable).
TEXT_ROUTING: dict[str, dict[str, str]] = {
    "minilm-multi-v1": {
        "arq_task": "embed_text_ml",
        "store_table": "text_embeddings",
    },
    "bge-m3-v1": {
        "arq_task": "embed_bge_m3_all",
        "store_table": "text_embeddings_bge_m3",
        "sparse_store_table": "text_embeddings_bge_m3_sparse",
        "colbert_store_table": "text_embeddings_bge_m3_colbert",
    },
}

_ROUTING_KEYS = ("arq_task", "store_table", "sparse_store_table", "colbert_store_table")


def upgrade() -> None:
    conn = op.get_bind()
    for name, routing in TEXT_ROUTING.items():
        conn.execute(
            text(
                "UPDATE public.embedding_models "
                "SET model_metadata = model_metadata || (:routing)::jsonb "
                "WHERE name = :name"
            ),
            {"name": name, "routing": json.dumps(routing)},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for name in TEXT_ROUTING:
        for key in _ROUTING_KEYS:
            conn.execute(
                text(
                    "UPDATE public.embedding_models "
                    "SET model_metadata = model_metadata - :key "
                    "WHERE name = :name"
                ),
                {"name": name, "key": key},
            )
