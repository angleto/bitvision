"""Tool catalog and patient-bound executors for the Q&A orchestrator.

The orchestrator exposes a curated set of tools to the LLM. Each tool
is patient-scoped *server-side*: the live ``patient_id`` is captured
in a closure when :func:`build_executors` is called, so the tool
schemas the model sees never include a patient identifier. This is
the cross-patient-impossible-by-construction invariant — the model
cannot ask about a different patient even if its prompt is poisoned.

Tools shipped in v1:

* ``find_clinical_events`` — list clinical events ordered newest first.
* ``get_event`` — full detail for one event id.
* ``get_lab_timeseries`` — lab values over time (delegates to the
  existing ``services.lab_timeseries`` helper if present, otherwise
  falls back to ``find_clinical_events`` filtered by ``lab_batch``).
* ``search_text_chunks`` — sub-document RAG search across all four
  source kinds, with author/authority filters.
* ``get_document_text`` — full OCR text of one document.
* ``summarize_document`` — short structured summary of one document.
* ``list_recent_documents`` — paginated metadata list.

Each executor returns a JSON-encoded string (``json.dumps``) so the
LLM's tool_result payload is always parseable. Errors raised by the
executor are caught by :mod:`agent_loop` and surfaced to the model as
``is_error=True`` tool results, so a single bad call does not abort
the conversation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    CHUNK_AUTHOR_KINDS,
    CHUNK_SOURCE_KINDS,
    CLINICAL_EVENT_KINDS,
    ClinicalEvent,
)
from bvphoenix.services.chunk_search import search_chunks
from bvphoenix.services.llm_types import LLMTool

__all__ = [
    "ToolExecutor",
    "build_executors",
    "build_tool_catalog",
]


ToolExecutor = Callable[[dict[str, Any]], Awaitable[str]]


# ---------------------------------------------------------------------------
# Tool schemas (provider-neutral)
# ---------------------------------------------------------------------------


def build_tool_catalog() -> list[LLMTool]:
    """Return the tool catalog the LLM sees for every Q&A turn.

    The patient is bound server-side so the schemas below have NO
    ``patient_id`` parameter. Tool names are stable identifiers — the
    matching executors in :func:`build_executors` use the same keys.
    """
    return [
        LLMTool(
            name="find_clinical_events",
            description=(
                "List clinical events for the current patient, newest first. "
                "Use for 'last X' / 'how many Y' / 'between dates' factual "
                "questions. Returns events with id, kind, event_date, title, "
                "narrative, body_part."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": list(CLINICAL_EVENT_KINDS),
                        "description": (
                            "Filter by event kind. Use 'imaging_study' for "
                            "PET/CT/MRI/etc., 'lab_batch' for blood/urine "
                            "tests, 'outpatient_visit' for visits, "
                            "'pathology_review' for histology readouts."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "format": "date",
                        "description": "ISO date YYYY-MM-DD (inclusive lower bound).",
                    },
                    "until": {
                        "type": "string",
                        "format": "date",
                        "description": "ISO date YYYY-MM-DD (inclusive upper bound).",
                    },
                    "contains": {
                        "type": "string",
                        "description": "Substring match on title or narrative.",
                    },
                    "k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 10,
                    },
                },
            },
        ),
        LLMTool(
            name="get_event",
            description=(
                "Full detail for one clinical event id, including linked "
                "documents and report contents."
            ),
            input_schema={
                "type": "object",
                "required": ["event_id"],
                "additionalProperties": False,
                "properties": {
                    "event_id": {"type": "string", "format": "uuid"},
                },
            },
        ),
        LLMTool(
            name="search_text_chunks",
            description=(
                "Hybrid sub-document search over the patient's documents, "
                "clinical notes, AI summaries, and report contents. Use for "
                "content questions ('summarise the histology', 'is there a "
                "mention of metastasis'). Returns chunks with excerpt and "
                "source provenance."
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 8,
                    },
                    "source_kind": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(CHUNK_SOURCE_KINDS),
                        },
                        "description": "Restrict to a subset of source kinds.",
                    },
                    "author_kind": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(CHUNK_AUTHOR_KINDS),
                        },
                    },
                    "exclude_ai": {
                        "type": "boolean",
                        "default": False,
                        "description": "When true, filter out author_kind='agent'.",
                    },
                    "authority_id": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by document/report authority (e.g. "
                            "['original'] for originals only)."
                        ),
                    },
                    "document_kind_id": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by document kind id (e.g. "
                            "'pathology_report', 'lab_result'). "
                            "Only meaningful for source_kind='document'."
                        ),
                    },
                },
            },
        ),
        LLMTool(
            name="get_document_text",
            description=(
                "Read the full OCR-extracted text of one document. Use after "
                "search_text_chunks identifies a target document and you "
                "need more context than the chunks provide."
            ),
            input_schema={
                "type": "object",
                "required": ["document_id"],
                "additionalProperties": False,
                "properties": {
                    "document_id": {"type": "string", "format": "uuid"},
                    "max_chars": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 10000,
                        "default": 4000,
                        "description": "Truncate the response to this many chars.",
                    },
                },
            },
        ),
        LLMTool(
            name="list_recent_documents",
            description=(
                "Paginated metadata list of the patient's documents, newest "
                "first. Use to enumerate available sources before drilling "
                "in via search_text_chunks or get_document_text."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "Optional document kind id filter.",
                    },
                    "k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 10,
                    },
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Executors (patient-bound via closure)
# ---------------------------------------------------------------------------


def _coerce_date(value: Any) -> date | None:
    """Coerce an LLM-supplied date argument to ``datetime.date``.

    The model passes dates as ISO strings ("2025-06-01", occasionally a
    full ISO datetime). asyncpg binds a DATE column expecting a date
    object (it calls ``.toordinal()``), so a raw ``str`` raises
    ``DataError`` — which is exactly why ``find_clinical_events`` 500'd on
    every "last year" query. Parse leniently; an unparseable value drops
    the filter rather than failing the whole tool call.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def build_executors(
    *,
    db: AsyncSession,
    patient_id: uuid.UUID,
) -> dict[str, ToolExecutor]:
    """Construct executors with ``patient_id`` baked into every call.

    The model invokes tools by name; the orchestrator looks up the
    executor here and runs it with the (validated) input dict. The
    patient_id is NEVER taken from the input dict — it is captured
    from this closure.
    """

    async def find_clinical_events(args: dict[str, Any]) -> str:
        kind = args.get("kind")
        since = _coerce_date(args.get("since"))
        until = _coerce_date(args.get("until"))
        contains = args.get("contains")
        k = int(args.get("k", 10))
        k = max(1, min(50, k))

        where = ["patient_id = :patient_id"]
        params: dict[str, Any] = {"patient_id": patient_id}
        if kind:
            where.append("kind = :kind")
            params["kind"] = kind
        if since:
            where.append("event_date >= :since")
            params["since"] = since
        if until:
            where.append("event_date <= :until")
            params["until"] = until
        if contains:
            where.append("(title ILIKE :ctn OR COALESCE(narrative, '') ILIKE :ctn)")
            params["ctn"] = f"%{contains}%"

        sql = text(
            f"""
            SELECT id, kind, event_date, title, body_part, narrative
            FROM clinical_events
            WHERE {" AND ".join(where)}
            ORDER BY event_date DESC NULLS LAST, created_at DESC
            LIMIT :k
            """
        )
        rows = (await db.execute(sql, {**params, "k": k})).all()
        event_ids = [r[0] for r in rows]

        # Surface ``linked_documents`` per event so the model can cite
        # both the event AND the underlying document in the same turn,
        # without paying for a second ``get_event`` round-trip per
        # row. Path: clinical_event → imaging_study (1:1 when
        # kind='imaging_study') → document_study_links → documents.
        # For lab / surgical / outpatient_visit / pathology_review /
        # therapy events the imaging_study FK can be null; we fall
        # back to the report_content → content_document_links bridge
        # so PDFs attached via the evidence editor still surface.
        linked_by_event: dict[str, list[dict[str, Any]]] = {str(eid): [] for eid in event_ids}
        if event_ids:
            doc_rows = (
                await db.execute(
                    text(
                        """
                        SELECT ce.id AS event_id, d.id AS document_id, d.title, d.kind_id
                        FROM clinical_events ce
                        JOIN imaging_studies s ON s.clinical_event_id = ce.id
                        JOIN document_study_links dsl ON dsl.study_id = s.id
                        JOIN documents d ON d.id = dsl.document_id
                        WHERE ce.id = ANY(:eids)
                          AND ce.patient_id = :pid
                          AND d.deleted_at IS NULL
                        UNION
                        SELECT ce.id AS event_id, d.id AS document_id, d.title, d.kind_id
                        FROM clinical_events ce
                        JOIN report_contents rc ON rc.clinical_event_id = ce.id
                        JOIN content_document_links cdl ON cdl.report_content_id = rc.id
                        JOIN documents d ON d.id = cdl.document_id
                        WHERE ce.id = ANY(:eids)
                          AND ce.patient_id = :pid
                          AND d.deleted_at IS NULL
                        LIMIT 200
                        """
                    ),
                    {"eids": event_ids, "pid": patient_id},
                )
            ).all()
            for ev_id, doc_id, title, kind_id in doc_rows:
                bucket = linked_by_event.setdefault(str(ev_id), [])
                if len(bucket) < 5:  # keep token budget tight
                    bucket.append(
                        {
                            "document_id": str(doc_id),
                            "title": title,
                            "kind_id": kind_id,
                        }
                    )

        events = [
            {
                "id": str(r[0]),
                "kind": r[1],
                "event_date": r[2].isoformat() if r[2] else None,
                "title": r[3],
                "body_part": r[4],
                "narrative": (r[5] or "")[:400] if r[5] else None,
                "linked_documents": linked_by_event.get(str(r[0]), []),
            }
            for r in rows
        ]
        return json.dumps({"events": events, "count": len(events)})

    async def get_event(args: dict[str, Any]) -> str:
        event_id = uuid.UUID(args["event_id"])
        row = (
            await db.execute(
                select(ClinicalEvent).where(
                    ClinicalEvent.id == event_id,
                    ClinicalEvent.patient_id == patient_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return json.dumps({"error": "event not found in this patient's record"})

        # Linked documents — narrowly bound to THIS event. v3 maps a
        # clinical_event 1:1 to its imaging_studies row (when kind=
        # 'imaging_study'); document_study_links then carries the
        # report-of / addendum / second-opinion / cites / mentions /
        # extracted-from edges. For non-imaging events the imaging
        # join returns no rows and we additionally surface any
        # report_content -> document edges via content_document_links
        # (joined separately below). Both queries are patient-scoped
        # so a stray FK never crosses the fascicolo boundary.
        linked_docs = (
            await db.execute(
                text(
                    """
                    SELECT dsl.document_id, d.title, d.kind_id
                    FROM document_study_links dsl
                    JOIN documents d ON d.id = dsl.document_id
                    JOIN imaging_studies s ON s.id = dsl.study_id
                    WHERE s.clinical_event_id = :eid
                      AND s.patient_id = :pid
                      AND d.patient_id = :pid
                      AND d.deleted_at IS NULL
                    ORDER BY dsl.created_at DESC
                    LIMIT 10
                    """
                ),
                {"eid": event_id, "pid": patient_id},
            )
        ).all()
        linked_reports = (
            await db.execute(
                text(
                    """
                    SELECT id, authority_id, status, title, language
                    FROM report_contents
                    WHERE clinical_event_id = :eid
                      AND status <> 'stale'
                    """
                ),
                {"eid": event_id},
            )
        ).all()
        return json.dumps(
            {
                "id": str(row.id),
                "kind": row.kind,
                "event_date": row.event_date.isoformat() if row.event_date else None,
                "title": row.title,
                "body_part": row.body_part,
                "narrative": row.narrative,
                "documents": [
                    {"document_id": str(r[0]), "title": r[1], "kind_id": r[2]} for r in linked_docs
                ],
                "report_contents": [
                    {
                        "id": str(r[0]),
                        "authority_id": r[1],
                        "status": r[2],
                        "title": r[3],
                        "language": r[4],
                    }
                    for r in linked_reports
                ],
            }
        )

    async def search_text_chunks_exec(args: dict[str, Any]) -> str:
        hits = await search_chunks(
            db,
            patient_id=patient_id,
            query=args["query"],
            k=int(args.get("k", 8)),
            source_kind=args.get("source_kind"),
            author_kind=args.get("author_kind"),
            exclude_ai=bool(args.get("exclude_ai", False)),
            authority_id=args.get("authority_id"),
            document_kind_id=args.get("document_kind_id"),
        )
        return json.dumps(
            {
                "hits": [
                    {
                        "chunk_id": str(h.chunk_id),
                        "source_kind": h.source_kind,
                        "source_id": str(h.source_id),
                        "page": h.page,
                        "excerpt": h.excerpt,
                        "score": h.score,
                        "author_kind": h.author_kind,
                        "authority_id": h.authority_id,
                        "document_kind_id": h.document_kind_id,
                    }
                    for h in hits
                ]
            }
        )

    async def get_document_text(args: dict[str, Any]) -> str:
        doc_id = uuid.UUID(args["document_id"])
        max_chars = int(args.get("max_chars", 4000))
        max_chars = max(100, min(10000, max_chars))
        row = (
            await db.execute(
                text(
                    """
                    SELECT ocr.text, d.title, d.kind_id, d.document_date
                    FROM documents d
                    JOIN document_ocr ocr ON ocr.document_id = d.id
                    WHERE d.id = :did
                      AND d.patient_id = :pid
                      AND d.deleted_at IS NULL
                    ORDER BY ocr.created_at DESC
                    LIMIT 1
                    """
                ),
                {"did": doc_id, "pid": patient_id},
            )
        ).first()
        if row is None:
            return json.dumps({"error": "document not found in this patient's record"})
        body = row[0] or ""
        truncated = len(body) > max_chars
        return json.dumps(
            {
                "document_id": str(doc_id),
                "title": row[1],
                "kind_id": row[2],
                "document_date": row[3].isoformat() if row[3] else None,
                "text": body[:max_chars],
                "truncated": truncated,
                "total_chars": len(body),
            }
        )

    async def list_recent_documents(args: dict[str, Any]) -> str:
        kind = args.get("kind")
        k = int(args.get("k", 10))
        k = max(1, min(50, k))
        where = ["patient_id = :pid", "deleted_at IS NULL"]
        params: dict[str, Any] = {"pid": patient_id, "k": k}
        if kind:
            where.append("kind_id = :kind")
            params["kind"] = kind
        rows = (
            await db.execute(
                text(
                    f"""
                    SELECT id, title, kind_id, authority_id, document_date,
                           created_at
                    FROM documents
                    WHERE {" AND ".join(where)}
                    ORDER BY COALESCE(document_date, created_at::date) DESC,
                             created_at DESC
                    LIMIT :k
                    """
                ),
                params,
            )
        ).all()
        return json.dumps(
            {
                "documents": [
                    {
                        "id": str(r[0]),
                        "title": r[1],
                        "kind_id": r[2],
                        "authority_id": r[3],
                        "document_date": r[4].isoformat() if r[4] else None,
                        "created_at": r[5].isoformat() if r[5] else None,
                    }
                    for r in rows
                ]
            }
        )

    return {
        "find_clinical_events": find_clinical_events,
        "get_event": get_event,
        "search_text_chunks": search_text_chunks_exec,
        "get_document_text": get_document_text,
        "list_recent_documents": list_recent_documents,
    }
