"""Generate a multilingual sentence-transformers embedding for a text span.

Runs as an arq background job. Encodes free-text content associated with
a target row (series description, report body, annotation label,
consultation turn, patient document, patient fascicolo, ...) and stores
the resulting 384-dim vector in the dedicated ``text_embeddings``
pgvector table.

Uses ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`` —
small (~120 MB), multilingual, and quality-competitive for semantic
text-to-text retrieval. The model is lazy-loaded as a module-level
global so subsequent jobs on the same worker reuse it without re-reading
weights from disk.

Requires the ``ai`` extra: ``uv sync --extra ai``
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
MODEL_ID = "minilm-multi-v1"
EMBEDDING_DIM = 384

ALLOWED_TARGET_KINDS: frozenset[str] = frozenset(
    {
        "series",
        "report",
        "annotation",
        "consultation",
        "document",
        "patient",
        "document_chunk",
    }
)

_model = None  # lazy: loaded once per worker on first call


def _ensure_model():
    """Load sentence-transformers model on first use."""
    global _model
    if _model is not None:
        return _model

    from sentence_transformers import SentenceTransformer

    _model = SentenceTransformer(MODEL_NAME)
    return _model


def _compute_embedding(text_value: str) -> list[float]:
    """Run the multilingual encoder on a string, return a 384-dim vector.

    Output is L2-normalized so dot-product equals cosine similarity and
    matches the ``vector_cosine_ops`` HNSW index used at query time.
    """
    model = _ensure_model()
    vector = model.encode(
        text_value,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return vector.tolist()


async def embed_text_ml(
    ctx: dict,  # type: ignore[type-arg]
    target_kind: str,
    target_id: str,
    text_value: str,
) -> dict:
    """Arq task: embed ``text_value`` and upsert into text_embeddings.

    ``target_kind`` must be one of the values allowed by the CHECK
    constraint on the table; ``target_id`` is the UUID of the owning row.
    Existing rows for ``(target_kind, target_id, model_id)`` are
    overwritten so re-embedding after text edits is idempotent.
    """
    if target_kind not in ALLOWED_TARGET_KINDS:
        return {
            "status": "invalid_target_kind",
            "target_kind": target_kind,
            "allowed": sorted(ALLOWED_TARGET_KINDS),
        }

    if not text_value or not text_value.strip():
        return {
            "status": "empty_text",
            "target_kind": target_kind,
            "target_id": target_id,
        }

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    tid = uuid.UUID(target_id)

    # CPU-bound encode → offload to a thread so we don't block the event loop.
    vector = await asyncio.to_thread(_compute_embedding, text_value)

    # pgvector literal format: "[v1,v2,...,vN]"
    vec_str = "[" + ",".join(str(v) for v in vector) + "]"

    async with AsyncSession(engine) as db:
        await db.execute(
            text(
                "INSERT INTO text_embeddings "
                "(target_kind, target_id, model_id, vector) "
                "VALUES (:kind, :tid, :model, :vec) "
                "ON CONFLICT (target_kind, target_id, model_id) DO UPDATE SET "
                "vector = EXCLUDED.vector, created_at = NOW()"
            ),
            {"kind": target_kind, "tid": tid, "model": MODEL_ID, "vec": vec_str},
        )
        await db.commit()

    await engine.dispose()
    return {
        "status": "embedded",
        "target_kind": target_kind,
        "target_id": target_id,
        "model_id": MODEL_ID,
        "dim": len(vector),
    }
