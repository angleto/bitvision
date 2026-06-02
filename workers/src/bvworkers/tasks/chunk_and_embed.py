"""Generic chunk-and-embed pipeline for any sub-document source.

Powers natural-language Q&A retrieval at chunk granularity across
every text-bearing entity in the platform:

* ``chunk_and_embed_document(document_id)`` — uploaded patient
  documents, sourced from the latest ``DocumentOCR`` row.
* ``chunk_and_embed_clinical_note(note_id)`` — clinician-authored or
  agent-authored ``clinical_notes.body``.
* ``chunk_and_embed_summary(summary_id)`` — AI-generated executive
  summaries from ``summaries.summary_md``.
* ``chunk_and_embed_report_content(rc_id)`` — extracted report bodies
  (concat of ``narrative_md`` + ``findings_md`` + ``recommendations_md``)
  from ``report_contents``.

All four tasks share a single private helper that:
1. Validates the source row and resolves ``patient_id``,
   ``author_kind``, ``authority_id``, and ``document_kind_id`` from
   the source itself (never from a caller-supplied parameter — this is
   the cross-patient defence-in-depth invariant).
2. Compares ``content_sha256`` against any existing chunk set tagged
   with the same ``chunker_version``: short-circuits as
   ``already_chunked`` on hash match, otherwise wipes the stale rows
   and re-chunks.
3. Slices the body via :func:`bvphoenix.services.chunking.chunk_document_text`
   and bulk-inserts into ``text_chunks``.
4. Enqueues one ``embed_text_ml`` job per chunk so the MiniLM 384-d
   vector lands in ``text_embeddings`` under
   ``target_kind='document_chunk'``.

Idempotency is keyed on the (source, version, hash) triple. Re-runs
are safe: matched hash → no-op, mismatched → atomic replace.

Requires the ``ai`` extra: ``uv sync --extra ai``.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from functools import lru_cache
from typing import Any

from bvphoenix.services.chunking import (
    DEFAULT_CHUNKER_VERSION,
    chunk_document_text,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from bvworkers.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _engine() -> AsyncEngine:
    """Module-cached async engine.

    Each chunk-and-embed task used to spin up its own engine with
    ``create_async_engine(...)`` and dispose it at the end. Under a
    bulk backfill (thousands of tasks per minute on a 4-CPU worker)
    this saturated Postgres ``max_connections`` and added 50-200 ms
    of TCP setup per task. We now share a single engine for the
    process lifetime: ``arq`` workers have a stable lifecycle, so
    reusing the connection pool matches the rest of the platform's
    backend pattern.
    """
    settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=8)


__all__ = [
    "chunk_and_embed_clinical_note",
    "chunk_and_embed_document",
    "chunk_and_embed_report_content",
    "chunk_and_embed_summary",
]


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


async def _resolve_document_source(db: AsyncSession, source_id: uuid.UUID) -> dict | None:
    """Resolve a document source: patient_id, author_kind, authority_id,
    document_kind_id, OCR text, content_sha256.

    ``subjects.kind`` is a Postgres enum (``subject_kind``) whose
    values are ``user``/``organization``/``group``/``public``/``agent``.
    The LEFT JOIN can return NULL for an orphan / pre-PLATFORM_OWNER
    upload; we cast to text first so the COALESCE substitute string
    ``'unknown'`` does not have to be a valid enum value (otherwise
    asyncpg raises ``InvalidTextRepresentationError`` on the bind).
    """
    row = await db.execute(
        text(
            """
            SELECT d.patient_id, d.kind_id, d.authority_id,
                   COALESCE(s.kind::text, 'unknown') AS subject_kind,
                   ocr.text, ocr.content_sha256
            FROM documents d
            LEFT JOIN subjects s ON s.id = d.uploaded_by_subject_id
            JOIN document_ocr ocr ON ocr.document_id = d.id
            WHERE d.id = :sid
              AND d.deleted_at IS NULL
            ORDER BY ocr.created_at DESC
            LIMIT 1
            """
        ),
        {"sid": source_id},
    )
    res = row.first()
    if res is None:
        return None
    return {
        "patient_id": res[0],
        "document_kind_id": res[1],
        "authority_id": res[2],
        "author_kind": _subject_to_author_kind(res[3]),
        "body": res[4] or "",
        "content_sha256": res[5],
    }


async def _resolve_clinical_note_source(db: AsyncSession, source_id: uuid.UUID) -> dict | None:
    row = await db.execute(
        text(
            """
            SELECT patient_id, body, author_kind
            FROM clinical_notes
            WHERE id = :sid
            """
        ),
        {"sid": source_id},
    )
    res = row.first()
    if res is None:
        return None
    body = res[1] or ""
    return {
        "patient_id": res[0],
        "document_kind_id": None,
        "authority_id": None,
        "author_kind": _normalise_author_kind(res[2]),
        "body": body,
        "content_sha256": _sha256(body),
    }


async def _resolve_summary_source(db: AsyncSession, source_id: uuid.UUID) -> dict | None:
    """Summaries don't carry patient_id directly; resolve via target."""
    row = await db.execute(
        text(
            """
            SELECT target_kind, target_id, summary_md
            FROM summaries
            WHERE id = :sid
            """
        ),
        {"sid": source_id},
    )
    res = row.first()
    if res is None:
        return None
    target_kind, target_id, body = res[0], res[1], res[2] or ""
    patient_id = await _patient_for_target(db, target_kind, target_id)
    if patient_id is None:
        return None
    return {
        "patient_id": patient_id,
        "document_kind_id": None,
        "authority_id": None,
        "author_kind": "agent",  # summaries are AI-generated by definition
        "body": body,
        "content_sha256": _sha256(body),
    }


async def _resolve_report_content_source(db: AsyncSession, source_id: uuid.UUID) -> dict | None:
    row = await db.execute(
        text(
            """
            SELECT ce.patient_id, rc.authority_id, rc.author_kind,
                   rc.title, rc.narrative_md, rc.findings_md, rc.recommendations_md
            FROM report_contents rc
            JOIN clinical_events ce ON ce.id = rc.clinical_event_id
            WHERE rc.id = :sid
            """
        ),
        {"sid": source_id},
    )
    res = row.first()
    if res is None:
        return None
    title, narrative, findings, recs = res[3], res[4], res[5], res[6]
    body_parts = [p for p in (title, narrative, findings, recs) if p]
    body = "\n\n".join(body_parts)
    return {
        "patient_id": res[0],
        "document_kind_id": None,
        "authority_id": res[1],
        "author_kind": _normalise_author_kind(res[2]),
        "body": body,
        "content_sha256": _sha256(body),
    }


async def _patient_for_target(
    db: AsyncSession, target_kind: str, target_id: uuid.UUID
) -> uuid.UUID | None:
    """Resolve the patient owning a target row.

    The summary's ``target_kind`` is one of {'patient','study','series',
    'document'} in current usage; only those four paths need to resolve
    to a patient. Unknown kinds yield ``None`` and the caller skips.
    """
    if target_kind == "patient":
        return target_id
    if target_kind == "study":
        row = await db.execute(
            text("SELECT patient_id FROM imaging_studies WHERE id = :tid"),
            {"tid": target_id},
        )
    elif target_kind == "series":
        row = await db.execute(
            text(
                """
                SELECT s.patient_id
                FROM series ser
                JOIN imaging_studies s ON s.id = ser.study_id
                WHERE ser.id = :tid
                """
            ),
            {"tid": target_id},
        )
    elif target_kind == "document":
        row = await db.execute(
            text("SELECT patient_id FROM documents WHERE id = :tid"),
            {"tid": target_id},
        )
    else:
        return None
    res = row.first()
    return res[0] if res else None


def _subject_to_author_kind(subject_kind: str | None) -> str:
    """Map a ``subjects.kind`` value to a chunk ``author_kind``."""
    if subject_kind is None:
        return "unknown"
    if subject_kind == "agent":
        return "agent"
    if subject_kind in ("user", "organization", "group"):
        return "human"
    return "system"


def _normalise_author_kind(value: str | None) -> str:
    """Coerce arbitrary author_kind strings into the chunk enum."""
    if value is None:
        return "unknown"
    v = value.lower().strip()
    if v in ("human", "agent", "system", "unknown"):
        return v
    if v in ("user", "clinician", "doctor"):
        return "human"
    if v in ("ai", "model", "llm"):
        return "agent"
    return "unknown"


def _sha256(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Persistence helper
# ---------------------------------------------------------------------------


async def _existing_match(
    db: AsyncSession,
    source_kind: str,
    source_id: uuid.UUID,
    chunker_version: str,
    content_sha256: str,
) -> bool:
    """True iff the source already has at least one chunk row whose
    ``content_sha256`` matches the live body.

    ``bool_and`` over an empty set returns NULL, which the previous
    implementation treated as ``False`` and therefore re-ran the full
    delete+chunk+embed pipeline on every backfill — even for sources
    whose body never produced any chunk (empty notes, deleted OCR
    rows). We now check the row count explicitly: zero rows means
    "never chunked", which the caller handles by either chunking
    (if body is non-empty) or short-circuiting as ``empty_text``.
    """
    row = await db.execute(
        text(
            """
            SELECT COUNT(*) AS n,
                   bool_and(content_sha256 = :sha) AS all_match
            FROM text_chunks
            WHERE source_kind = :sk AND source_id = :sid
              AND chunker_version = :ver
            """
        ),
        {
            "sk": source_kind,
            "sid": source_id,
            "ver": chunker_version,
            "sha": content_sha256,
        },
    )
    res = row.first()
    if res is None:
        return False
    count = int(res[0] or 0)
    if count == 0:
        return False
    return bool(res[1])


async def _delete_stale(
    db: AsyncSession,
    source_kind: str,
    source_id: uuid.UUID,
    chunker_version: str,
) -> None:
    # Wipe old text_embeddings rows BEFORE deleting the chunks they
    # reference, since the chunk rows are the source of the IN clause.
    await db.execute(
        text(
            """
            DELETE FROM text_embeddings
            WHERE target_kind = 'document_chunk'
              AND target_id IN (
                  SELECT id FROM text_chunks
                  WHERE source_kind = :sk AND source_id = :sid
                    AND chunker_version = :ver
              )
            """
        ),
        {"sk": source_kind, "sid": source_id, "ver": chunker_version},
    )
    await db.execute(
        text(
            """
            DELETE FROM text_chunks
            WHERE source_kind = :sk AND source_id = :sid
              AND chunker_version = :ver
            """
        ),
        {"sk": source_kind, "sid": source_id, "ver": chunker_version},
    )


async def _insert_chunks(
    db: AsyncSession,
    *,
    source_kind: str,
    source_id: uuid.UUID,
    patient_id: uuid.UUID,
    author_kind: str,
    authority_id: str | None,
    document_kind_id: str | None,
    chunker_version: str,
    content_sha256: str,
    chunks: list,
) -> list[uuid.UUID]:
    chunk_ids: list[uuid.UUID] = []
    for ch in chunks:
        new_id = uuid.uuid4()
        await db.execute(
            text(
                """
                INSERT INTO text_chunks (
                    id, source_kind, source_id, patient_id, author_kind,
                    authority_id, document_kind_id, chunker_version,
                    page, char_start, char_end, text, content_sha256
                ) VALUES (
                    :id, :sk, :sid, :pid, :ak,
                    :auth, :dkind, :ver,
                    :page, :char_start, :char_end, :body, :sha
                )
                """
            ),
            {
                "id": new_id,
                "sk": source_kind,
                "sid": source_id,
                "pid": patient_id,
                "ak": author_kind,
                "auth": authority_id,
                "dkind": document_kind_id,
                "ver": chunker_version,
                "page": ch.page,
                "char_start": ch.char_start,
                "char_end": ch.char_end,
                "body": ch.text,
                "sha": content_sha256,
            },
        )
        chunk_ids.append(new_id)
    return chunk_ids


async def _persist_chunks_and_embed(
    ctx: dict,
    *,
    source_kind: str,
    source_id: uuid.UUID,
    patient_id: uuid.UUID,
    author_kind: str,
    authority_id: str | None,
    document_kind_id: str | None,
    body: str,
    content_sha256: str,
    chunker_version: str,
    page_offsets: list[int] | None,
) -> dict:
    async with AsyncSession(_engine()) as db:
        if not body.strip():
            return {
                "status": "empty_text",
                "source_kind": source_kind,
                "source_id": str(source_id),
                "chunker_version": chunker_version,
            }

        if await _existing_match(db, source_kind, source_id, chunker_version, content_sha256):
            return {
                "status": "already_chunked",
                "source_kind": source_kind,
                "source_id": str(source_id),
                "chunker_version": chunker_version,
            }

        await _delete_stale(db, source_kind, source_id, chunker_version)

        chunks = chunk_document_text(body, page_offsets=page_offsets)
        if not chunks:
            await db.commit()
            return {
                "status": "no_chunks",
                "source_kind": source_kind,
                "source_id": str(source_id),
                "chunker_version": chunker_version,
            }

        chunk_ids = await _insert_chunks(
            db,
            source_kind=source_kind,
            source_id=source_id,
            patient_id=patient_id,
            author_kind=author_kind,
            authority_id=authority_id,
            document_kind_id=document_kind_id,
            chunker_version=chunker_version,
            content_sha256=content_sha256,
            chunks=chunks,
        )
        await db.commit()

    redis = ctx.get("redis") if isinstance(ctx, dict) else None
    enqueued = 0
    if redis is not None:
        for cid, ch in zip(chunk_ids, chunks, strict=True):
            try:
                # MiniLM (current default) + BGE-M3 dense (the upgrade):
                # populate both stores during the transition so flipping
                # the registry default to bge-m3-v1 has data to read.
                await redis.enqueue_job("embed_text_ml", "document_chunk", str(cid), ch.text)
                await redis.enqueue_job("embed_bge_m3_dense", "document_chunk", str(cid), ch.text)
                enqueued += 1
            except Exception:
                logger.exception("failed to enqueue embed jobs for chunk %s", cid)

    return {
        "status": "chunked",
        "source_kind": source_kind,
        "source_id": str(source_id),
        "chunker_version": chunker_version,
        "chunks": len(chunks),
        "embeddings_enqueued": enqueued,
    }


# ---------------------------------------------------------------------------
# Public Arq tasks (one per source kind)
# ---------------------------------------------------------------------------


async def _run_for_source(
    ctx: dict,
    source_kind: str,
    source_id_str: str,
    chunker_version: str | None,
    page_offsets: list[int] | None,
    resolver: Any,
) -> dict:
    version = chunker_version or DEFAULT_CHUNKER_VERSION
    source_uuid = uuid.UUID(source_id_str)

    async with AsyncSession(_engine()) as db:
        resolved = await resolver(db, source_uuid)

    if resolved is None:
        return {
            "status": "no_source",
            "source_kind": source_kind,
            "source_id": source_id_str,
            "chunker_version": version,
        }

    return await _persist_chunks_and_embed(
        ctx,
        source_kind=source_kind,
        source_id=source_uuid,
        patient_id=resolved["patient_id"],
        author_kind=resolved["author_kind"],
        authority_id=resolved["authority_id"],
        document_kind_id=resolved["document_kind_id"],
        body=resolved["body"],
        content_sha256=resolved["content_sha256"],
        chunker_version=version,
        page_offsets=page_offsets,
    )


async def chunk_and_embed_document(
    ctx: dict,  # type: ignore[type-arg]
    document_id: str,
    chunker_version: str | None = None,
    page_offsets: list[int] | None = None,
) -> dict:
    """Chunk a document's OCR text and queue per-chunk MiniLM embeddings."""
    return await _run_for_source(
        ctx,
        "document",
        document_id,
        chunker_version,
        page_offsets,
        _resolve_document_source,
    )


async def chunk_and_embed_clinical_note(
    ctx: dict,  # type: ignore[type-arg]
    note_id: str,
    chunker_version: str | None = None,
) -> dict:
    """Chunk a clinical note body for retrieval."""
    return await _run_for_source(
        ctx,
        "clinical_note",
        note_id,
        chunker_version,
        None,
        _resolve_clinical_note_source,
    )


async def chunk_and_embed_summary(
    ctx: dict,  # type: ignore[type-arg]
    summary_id: str,
    chunker_version: str | None = None,
) -> dict:
    """Chunk an AI summary body for retrieval."""
    return await _run_for_source(
        ctx,
        "summary",
        summary_id,
        chunker_version,
        None,
        _resolve_summary_source,
    )


async def chunk_and_embed_report_content(
    ctx: dict,  # type: ignore[type-arg]
    rc_id: str,
    chunker_version: str | None = None,
) -> dict:
    """Chunk a report-content narrative (title + narrative + findings + recs)."""
    return await _run_for_source(
        ctx,
        "report_content",
        rc_id,
        chunker_version,
        None,
        _resolve_report_content_source,
    )
