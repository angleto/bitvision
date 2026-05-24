"""Generate a BiomedCLIP *text* embedding for a textual artefact.

Runs as an arq background job. Tokenises the input text with the
BiomedCLIP PubMedBERT tokenizer, runs the text tower of the dual
encoder model, L2-normalises the output, and stores the resulting
512-dim vector in the ``embeddings`` table under ``model_id =
'biomedclip-text-v1'``.

Because BiomedCLIP is a single contrastive model trained jointly on
image/text pairs, the vectors produced here live in the *same* 512-d
latent space as the image vectors emitted by ``embed_series``. A cosine
query built from a clinician's free-text prompt can therefore be scored
directly against series embeddings for cross-modal retrieval.

Supported ``target_kind`` values mirror the extended CHECK constraint
introduced in migration ``0014_extend_embedding_target_kind``:
``series``, ``report``, ``annotation``, ``consultation``, ``document``.

Requires the ``ai`` extra: ``uv sync --extra ai``
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings
from bvworkers.tasks.embed_series import EMBEDDING_DIM, _text_encoder

MODEL_ID = "biomedclip-text-v1"

# Target kinds that carry natural-language content we can embed. Keep in
# sync with ``ck_embeddings_target_kind`` in migration 0014.
ALLOWED_TARGET_KINDS = {
    "series",
    "report",
    "consultation",
    "document",
}


def _compute_text_embedding(text_content: str) -> list[float]:
    """Run BiomedCLIP text encoder on a string, return 512-dim vector."""
    import torch

    model, tokenizer = _text_encoder()
    tokens = tokenizer([text_content])
    with torch.no_grad():
        text_features = model.encode_text(tokens)
        # L2-normalize — matches the image path so cosine similarity is
        # well-defined across modalities.
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
    return text_features.squeeze(0).tolist()


async def _fetch_text_from_db(db: AsyncSession, target_kind: str, tid: uuid.UUID) -> str | None:
    """Pull the natural-language body for a target row directly from Postgres.

    The worker runs inside the trusted network so we read the DB directly
    rather than round-tripping through the backend HTTP API — same pattern
    as ``embed_series`` which talks to S3/Postgres without an HTTP hop.
    """
    if target_kind == "report":
        row = await db.execute(
            text("SELECT text FROM reports WHERE id = :tid"), {"tid": tid}
        )
        res = row.first()
        return res[0] if res else None

    if target_kind == "consultation":
        row = await db.execute(
            text(
                "SELECT concat_ws(E'\\n\\n', title, summary_md, findings_md, "
                "recommendations_md) FROM consultations WHERE id = :tid"
            ),
            {"tid": tid},
        )
        res = row.first()
        return res[0] if res else None

    if target_kind == "series":
        # Compose a short descriptor from DICOM metadata so the image
        # series is searchable by text queries too.
        row = await db.execute(
            text(
                "SELECT concat_ws(' | ', modality, body_part_examined, "
                "series_description) FROM series WHERE id = :tid"
            ),
            {"tid": tid},
        )
        res = row.first()
        return res[0] if res else None

    if target_kind == "document":
        # No first-class documents table yet — callers that embed a
        # document artefact must pass ``text`` explicitly.
        return None

    return None


async def embed_text_target(
    ctx: dict,  # type: ignore[type-arg]
    target_kind: str,
    target_id: str,
    text_content: str | None = None,
) -> dict:
    """Arq task: generate a BiomedCLIP text embedding for a textual target.

    Args:
        target_kind: one of ``series``, ``report``, ``annotation``,
            ``consultation``, ``document``.
        target_id: UUID of the target row (as a string).
        text_content: optional raw text to embed. When ``None`` the worker
            looks the text up in Postgres using ``target_kind`` + ``target_id``.
    """
    if target_kind not in ALLOWED_TARGET_KINDS:
        return {
            "status": "invalid_target_kind",
            "target_kind": target_kind,
            "allowed": sorted(ALLOWED_TARGET_KINDS),
        }

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    tid = uuid.UUID(target_id)

    async with AsyncSession(engine) as db:
        # Skip if we already have an up-to-date text embedding for this row.
        existing = await db.execute(
            text(
                "SELECT id FROM embeddings "
                "WHERE target_kind = :kind AND target_id = :tid "
                "AND model_id = :model"
            ),
            {"kind": target_kind, "tid": tid, "model": MODEL_ID},
        )
        if existing.first():
            await engine.dispose()
            return {
                "status": "already_embedded",
                "target_kind": target_kind,
                "target_id": target_id,
            }

        body = text_content
        if body is None:
            body = await _fetch_text_from_db(db, target_kind, tid)

        if not body or not body.strip():
            await engine.dispose()
            return {
                "status": "no_text",
                "target_kind": target_kind,
                "target_id": target_id,
            }

        # Tokenisation + inference is CPU-bound; offload so we don't block
        # the arq event loop.
        vector = await asyncio.to_thread(_compute_text_embedding, body)

        if len(vector) != EMBEDDING_DIM:
            await engine.dispose()
            raise RuntimeError(
                f"text encoder returned dim={len(vector)}, expected {EMBEDDING_DIM}"
            )

        vec_str = "[" + ",".join(str(v) for v in vector) + "]"
        await db.execute(
            text(
                "INSERT INTO embeddings (target_kind, target_id, model_id, vector) "
                "VALUES (:kind, :tid, :model, :vec) "
                "ON CONFLICT (target_kind, target_id, model_id) DO UPDATE SET "
                "vector = EXCLUDED.vector, created_at = NOW()"
            ),
            {
                "kind": target_kind,
                "tid": tid,
                "model": MODEL_ID,
                "vec": vec_str,
            },
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
