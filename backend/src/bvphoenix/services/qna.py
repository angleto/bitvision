"""Q&A orchestrator.

Single entry point used by both the REST endpoint
(``POST /api/patients/{id}/ask``) and the MCP tool
(``ask_about_patient``). Given a patient id, a user query, and a live
session, it:

1. Resolves the active AI tier for the caller (admin default + per-user
   override, see :mod:`ai_tiers`).
2. Builds a fresh provider instance for that tier.
3. Builds the patient-bound tool catalog (:mod:`qna_tools`).
4. Runs the agent loop (:mod:`agent_loop`) up to a tier-aware
   iteration cap.
5. Parses the model's inline citation markers (``[doc:UUID]``,
   ``[event:UUID]``, ...) into a structured ``citations`` list.
6. Returns the textual answer + citations + a tool-call trace.

Wallet billing and pre-flight gates are wired in by the API layer
(:mod:`api.qna`) — this service stays free of HTTP concerns and is
testable in isolation. The audit / provenance hooks that connect the
:class:`AgentLoopResult` to ``provenance_events`` and the credit ledger
also live in the API layer.

Citation parser: matches ``[<kind>:<uuid>]`` where ``<kind>`` is one
of ``doc``, ``event``, ``note``, ``summary``, ``report``, ``chunk``.
References to UUIDs not seen during the loop are dropped (defence in
depth — the model should never cite an id it didn't get from a
tool).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.services.agent_loop import AgentLoopResult, ToolCallTrace, agent_loop
from bvphoenix.services.ai_tiers import (
    AiTier,
    config_for_tier,
    provider_for_tier,
    resolve_tier_for_user,
)
from bvphoenix.services.qna_prompts import build_system_prompt
from bvphoenix.services.qna_tools import build_executors, build_tool_catalog

logger = logging.getLogger(__name__)

__all__ = [
    "AnswerResult",
    "Citation",
    "answer_question",
]


# Maximum iterations of the tool-use loop, by tier. Premium gets more
# turns to allow deeper retrieval; free skips LLM altogether (the
# orchestrator returns deterministic chunk_search hits without LLM
# synthesis when tier=free).
_MAX_ITERATIONS_BY_TIER: dict[AiTier, int] = {
    AiTier.FREE: 0,
    AiTier.STANDARD: 6,
    AiTier.PREMIUM: 8,
}

_MAX_TOKENS_PER_TURN = 1024

_CITATION_RE = re.compile(
    # Both short prefixes (``doc``, ``note``, ``report``) and their
    # long-form aliases (``document``, ``clinical_note``,
    # ``report_content``) are accepted. The prompt asks for the short
    # form but models frequently emit the long form anyway, especially
    # after tool outputs that surface the canonical
    # ``source_kind='document'`` label. Both alphabets normalise to
    # the long form through :data:`_CITATION_KIND_LABEL`.
    r"\[(?P<kind>doc|document|event|note|clinical_note|summary|report|report_content|chunk):"
    r"(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    # Optional payload after the UUID: any text up to the closing
    # ``]`` that is not itself a bracket. The model often emits a
    # quoted snippet (``[report:UUID "frase"]``) but mis-formats it
    # routinely — closing the quote too early, mixing curly and
    # straight, dropping the quotes entirely. We accept *any* trailing
    # text here and sanitise it in :func:`_parse_citation_markers` so
    # the chip still renders even when the LLM's quoting is messy.
    r"(?:\s+(?P<quote_payload>[^\[\]]+?))?"
    r"\]"
)

_CITATION_KIND_LABEL: dict[str, str] = {
    "doc": "document",
    "document": "document",
    "event": "event",
    "note": "clinical_note",
    "clinical_note": "clinical_note",
    "summary": "summary",
    "report": "report_content",
    "report_content": "report_content",
    "chunk": "chunk",
}


@dataclass(frozen=True, slots=True)
class Citation:
    """A reference cited by the model and validated against the trace.

    ``title`` and ``date`` are populated server-side from the target
    row (one bulk SELECT per kind, see :func:`_enrich_citations`) so
    the FE can render a human label like ``📋 Relazione conclusiva
    10/04/2026`` instead of the raw ``📋 report_content:cbd2aa0f``
    UUID short. ``quote`` is the literal snippet the model emitted
    between curly or straight double quotes inside the marker (e.g.
    ``[report:UUID "4 lesioni periepatiche"]``); the FE highlights it
    in the preview pane so the user sees immediately why the report
    was cited.
    """

    kind: str  # 'document' | 'event' | 'clinical_note' | 'summary' | 'report_content' | 'chunk'
    ref_id: uuid.UUID
    title: str | None = None
    date: str | None = None
    quote: str | None = None


@dataclass
class AnswerResult:
    """Outcome of one ``/ask`` invocation, ready for serialisation."""

    answer_md: str
    citations: list[Citation]
    tool_calls: list[ToolCallTrace]
    used_tools: list[str]
    iterations: int
    stop_reason: str
    tier: AiTier
    model_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


async def answer_question(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    query: str,
    lang: str = "it",
    user_subject_id: uuid.UUID | None = None,
    user: Any = None,
    request: Any = None,
    tier_override: AiTier | None = None,
    model_override: str | None = None,
) -> AnswerResult:
    """Run the Q&A loop end-to-end and return a structured answer.

    Parameters
    ----------
    db:
        Live :class:`AsyncSession`. Re-used by every executor; do not
        commit inside the loop (executors are read-only).
    patient_id:
        Patient whose record is being queried. Bound to every tool
        executor server-side; the model never sees this value.
    query:
        Natural-language question.
    lang:
        Reply language (``it`` / ``en``). Defaults to Italian.
    user_subject_id:
        For tier resolution. ``None`` → anonymous → free tier.
    tier_override:
        When provided, skip the resolver and use this tier. Used by
        the auto-downgrade path in the wallet gate.
    """

    if tier_override is not None:
        tier = tier_override
    else:
        tier = await resolve_tier_for_user(db, user_subject_id=user_subject_id)

    cfg = config_for_tier(tier)
    if model_override is not None and tier is not AiTier.FREE:
        # Per-call override: discover the provider for the requested
        # model_id from the rate cards and build a one-off provider.
        from bvphoenix.services.llm_factory import provider_for_model

        provider = await provider_for_model(db, model_override)
        cfg = type(cfg)(
            tier=tier,
            llm_provider_kind=cfg.llm_provider_kind,
            llm_model_id=model_override,
            embedding_provider_kind=cfg.embedding_provider_kind,
            embedding_model_id=cfg.embedding_model_id,
        )
    else:
        provider = provider_for_tier(tier)
    tools = build_tool_catalog()
    executors = build_executors(db=db, patient_id=patient_id, user=user, request=request)
    system = await build_system_prompt(db, lang)

    max_iterations = _MAX_ITERATIONS_BY_TIER.get(tier, 6)

    if max_iterations <= 0:
        # Free tier: skip the LLM, run deterministic retrieval directly
        # and assemble a minimal answer. Keeps the freemium path useful
        # without spending tokens.
        return await _free_tier_answer(
            db, patient_id=patient_id, query=query, tier=tier, model_id=cfg.llm_model_id
        )

    loop_result = await agent_loop(
        provider,
        system=system,
        user_message=query,
        tools=tools,
        executors=executors,
        max_iterations=max_iterations,
        max_tokens_per_turn=_MAX_TOKENS_PER_TURN,
    )

    raw_citations = _parse_citation_markers(loop_result.final_text)
    # Authoritative cross-patient defence: drop any cited UUID that is
    # not actually owned by the current patient. The model could
    # hallucinate a UUID, the LLM could echo an id from a tool result
    # that referenced something outside scope (impossible by
    # construction but cheap to verify), or a future bug could let a
    # foreign id slip through. SQL is the source of truth.
    citations = await _validate_citations(db, patient_id=patient_id, citations=raw_citations)
    # Collapse superseded report_content chains: when the model cites
    # ``[report:OLD]`` and OLD has been superseded, rewrite the citation
    # (and the inline marker) to point at the canonical head. Without
    # this, a single relazione clinica that has been superseded once
    # shows up as two chips that look like independent evidence.
    citations, rewrite_map = await _collapse_superseded_chain(db, citations=citations)
    # Attach human title + ISO date to every citation so the FE chip
    # reads as a clinician would expect (``📋 Relazione conclusiva
    # 10/04/2026``) rather than a raw UUID-short. Done after the
    # supersede collapse so we hydrate against the head, not the stale
    # row.
    citations = await _enrich_citations(db, patient_id=patient_id, citations=citations)
    answer_md = _rewrite_collapsed_markers(loop_result.final_text, rewrite_map=rewrite_map)
    answer_md = _strip_invalid_citation_markers(answer_md, valid_citations=citations, lang=lang)
    used_tools = sorted({tc.name for tc in loop_result.tool_calls})

    return AnswerResult(
        answer_md=answer_md,
        citations=citations,
        tool_calls=loop_result.tool_calls,
        used_tools=used_tools,
        iterations=loop_result.iterations,
        stop_reason=loop_result.stop_reason,
        tier=tier,
        model_id=loop_result.model_id or cfg.llm_model_id,
        usage=loop_result.usage.as_dict(),
    )


# ---------------------------------------------------------------------------
# Free tier: deterministic chunk retrieval, no LLM
# ---------------------------------------------------------------------------


async def _free_tier_answer(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    query: str,
    tier: AiTier,
    model_id: str,
) -> AnswerResult:
    """Free tier: run search_text_chunks and format the hits in markdown.

    No LLM, no orchestrator, zero cost. Useful as a freemium hook and
    as a fallback when wallet balance hits zero.
    """
    from bvphoenix.services.chunk_search import search_chunks

    hits = await search_chunks(db, patient_id=patient_id, query=query, k=8)
    if not hits:
        return AnswerResult(
            answer_md=(
                "Nessun risultato pertinente trovato nel fascicolo per la "
                "query proposta. Per risposte sintetizzate da un assistente "
                "AI è necessario un piano `standard` o `premium`."
            ),
            citations=[],
            tool_calls=[],
            used_tools=[],
            iterations=0,
            stop_reason="end_turn",
            tier=tier,
            model_id=model_id,
        )

    lines = [
        "**Risultati dal fascicolo del paziente** (modalità freemium — "
        "elenco diretto senza sintesi AI):",
        "",
    ]
    citations: list[Citation] = []
    for h in hits:
        ref_id = h.source_id
        kind_label = h.source_kind
        marker = _kind_to_marker(h.source_kind)
        lines.append(f"- *{kind_label}* ({h.author_kind}): {h.excerpt} [{marker}:{ref_id}]")
        # Use the chunk excerpt as quote so the FE preview can highlight
        # the matched span — free tier returns chunks as evidence, not
        # synthesized prose, so the chunk text IS the citation reason.
        citations.append(Citation(kind=h.source_kind, ref_id=ref_id, quote=h.excerpt))
    # Enrich titles/dates from the underlying tables. Free tier skipped
    # the LLM but the chips still benefit from human labels.
    citations = await _enrich_citations(db, patient_id=patient_id, citations=citations)
    return AnswerResult(
        answer_md="\n".join(lines),
        citations=citations,
        tool_calls=[],
        used_tools=["search_text_chunks"],
        iterations=0,
        stop_reason="end_turn",
        tier=tier,
        model_id=model_id,
    )


def _kind_to_marker(source_kind: str) -> str:
    mapping = {
        "document": "doc",
        "clinical_note": "note",
        "summary": "summary",
        "report_content": "report",
    }
    return mapping.get(source_kind, "chunk")


# ---------------------------------------------------------------------------
# Citation extraction + validation
# ---------------------------------------------------------------------------


def _parse_citation_markers(text_value: str) -> list[Citation]:
    """Parse ``[kind:UUID]`` and ``[kind:UUID "quote"]`` markers.

    Returns a deduped list preserving first-occurrence order. UUIDs
    are normalised (case-insensitive parse → canonical lowercase) so
    a model that emits mixed-case still produces stable citations the
    FE can match against. The optional quoted snippet is attached to
    the first occurrence; subsequent markers of the same (kind, ref)
    are dropped even if they carry different quotes — emitting two
    different quotes for the same target is on the model, and surfacing
    only the first keeps the FE highlight stable. Validation against
    the patient is done in a separate step (:func:`_validate_citations`).
    """
    if not text_value:
        return []
    seen: set[tuple[str, uuid.UUID]] = set()
    out: list[Citation] = []
    for match in _CITATION_RE.finditer(text_value):
        try:
            ref = uuid.UUID(match.group("uuid"))
        except ValueError:
            continue
        kind_short = match.group("kind")
        kind = _CITATION_KIND_LABEL.get(kind_short, kind_short)
        key = (kind, ref)
        if key in seen:
            continue
        seen.add(key)
        quote = _sanitise_quote_payload(match.group("quote_payload"))
        out.append(Citation(kind=kind, ref_id=ref, quote=quote))
    return out


# Max quote length the chip preview surfaces. Past this we truncate
# so a runaway model output doesn't blow up the SSE event payload or
# overwhelm the preview pane. 280 chars is roughly two long
# sentences — enough to recognise the cited passage, short enough
# not to cover the whole report.
_QUOTE_MAX_CHARS = 280


def _sanitise_quote_payload(raw: str | None) -> str | None:
    """Normalise the optional snippet payload inside a citation marker.

    The model emits the snippet in one of several flavours: properly
    quoted (``"text"``), curly-quoted (``"text"``), partially quoted
    (``"text": more``), or unquoted free text. We strip a single pair
    of matching surrounding quotes when present, collapse internal
    whitespace runs, and cap the length so the FE chip stays inline.
    Returns ``None`` when the payload is missing or empty after
    sanitisation.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    # Strip one pair of matching surrounding quotes (straight, curly,
    # or mixed). Embedded quotes survive because they often appear
    # inside the cited passage. When the model closed its opening
    # quote mid-snippet (``"Quadro restaging": TC 09/03 …``), the
    # leading orphan ``"`` is stripped too so the chip preview reads
    # cleanly; the same goes for a lone trailing ``"``.
    _OPENERS = {'"', "“"}
    _CLOSERS = {'"', "”"}
    if len(text) >= 2:
        first, last = text[0], text[-1]
        if first in _OPENERS and last in _CLOSERS:
            text = text[1:-1].strip()
        elif first in _OPENERS:
            text = text[1:].strip()
        elif last in _CLOSERS:
            text = text[:-1].strip()
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) > _QUOTE_MAX_CHARS:
        text = text[: _QUOTE_MAX_CHARS - 1].rstrip() + "…"
    return text


# SQL fragments for SQL-level patient ownership verification, one per
# citation kind. Each must be a query that, given ``:ids`` (uuid[])
# and ``:pid`` (uuid), returns the subset of those ids actually owned
# by the patient. No orchestration heuristic — the DB is the source
# of truth, period.
_OWNERSHIP_QUERIES: dict[str, str] = {
    "document": (
        "SELECT id FROM documents WHERE id = ANY(:ids) AND patient_id = :pid AND deleted_at IS NULL"
    ),
    "event": "SELECT id FROM clinical_events WHERE id = ANY(:ids) AND patient_id = :pid",
    "clinical_note": "SELECT id FROM clinical_notes WHERE id = ANY(:ids) AND patient_id = :pid",
    "report_content": (
        "SELECT rc.id FROM report_contents rc "
        "JOIN clinical_events ce ON ce.id = rc.clinical_event_id "
        "WHERE rc.id = ANY(:ids) AND ce.patient_id = :pid"
    ),
    "chunk": "SELECT id FROM text_chunks WHERE id = ANY(:ids) AND patient_id = :pid",
    "summary": (
        "SELECT s.id FROM summaries s "
        "WHERE s.id = ANY(:ids) AND ("
        "  (s.target_kind = 'patient' AND s.target_id = :pid)"
        "  OR EXISTS (SELECT 1 FROM imaging_studies x WHERE x.id = s.target_id"
        "             AND s.target_kind = 'study' AND x.patient_id = :pid)"
        "  OR EXISTS (SELECT 1 FROM documents d WHERE d.id = s.target_id"
        "             AND s.target_kind = 'document' AND d.patient_id = :pid)"
        ")"
    ),
}


async def _validate_citations(
    db: AsyncSession,  # forward ref to avoid heavy import cycles
    *,
    patient_id: uuid.UUID,
    citations: list[Citation],
) -> list[Citation]:
    """Drop any cited UUID that the DB does not confirm belongs to ``patient_id``.

    One small SELECT per cited kind. Citations are typically <10 per
    answer so the round trips are negligible (and bound by
    ``max_iterations × output_blocks``). Unknown kinds are dropped
    outright — better silent than misleading.

    Returns the citations in their original order, filtered.
    """
    if not citations:
        return []
    by_kind: dict[str, list[uuid.UUID]] = {}
    for c in citations:
        by_kind.setdefault(c.kind, []).append(c.ref_id)

    valid: set[tuple[str, uuid.UUID]] = set()
    for kind, ids in by_kind.items():
        sql = _OWNERSHIP_QUERIES.get(kind)
        if sql is None:
            logger.warning("qna citation: unknown kind=%s — dropping all", kind)
            continue
        rows = (await db.execute(sql_text(sql), {"ids": ids, "pid": patient_id})).all()
        for row in rows:
            valid.add((kind, row[0]))

    dropped = [c for c in citations if (c.kind, c.ref_id) not in valid]
    if dropped:
        # Route patient_id and dropped refs through ``extra={...}`` so
        # the PHIRedactionFilter scrubs them in production logs (per
        # the lint_phi_safe.py rule).
        logger.warning(
            "qna dropped %d citation(s) not owned by current patient",
            len(dropped),
            extra={
                "patient_id": str(patient_id),
                "dropped": [(c.kind, str(c.ref_id)) for c in dropped],
            },
        )
    return [c for c in citations if (c.kind, c.ref_id) in valid]


async def _collapse_superseded_chain(
    db: AsyncSession,
    *,
    citations: list[Citation],
) -> tuple[list[Citation], dict[uuid.UUID, uuid.UUID]]:
    """Rewrite ``report_content`` citations to the head of the supersede chain.

    When a relazione clinica is superseded, the old row stays in the
    database with ``status='stale'`` and a ``superseded_by_id`` pointing
    at its replacement (which may itself have been superseded again).
    If the model cites the stale row — either because it asked an older
    tool result, or because retrieval surfaced a leftover before Patch 2
    fully propagated — the chip would open a dead version. Worse, when
    the model cites both OLD and NEW versions of the same report in the
    same answer, the user sees two chips that look like independent
    evidence.

    This function walks ``superseded_by_id`` for every cited
    ``report_content`` UUID up to depth 10 (a safety cap; chains in
    practice are 1-2 hops) and emits the head instead. Citations that
    collapse onto the same head are deduplicated, preserving the order
    of first occurrence. Returns the rewritten list plus a
    ``{old_id: head_id}`` mapping so the caller can rewrite the inline
    ``[report:UUID]`` markers in the answer body to match.
    """
    rc_ids = [c.ref_id for c in citations if c.kind == "report_content"]
    if not rc_ids:
        return citations, {}

    sql = sql_text(
        """
        WITH RECURSIVE chain(start_id, current_id, depth) AS (
            SELECT id, id, 0
            FROM report_contents
            WHERE id = ANY(:ids)
            UNION ALL
            SELECT c.start_id, rc.superseded_by_id, c.depth + 1
            FROM chain c
            JOIN report_contents rc ON rc.id = c.current_id
            WHERE rc.superseded_by_id IS NOT NULL
              AND c.depth < 10
        )
        SELECT DISTINCT ON (start_id) start_id, current_id
        FROM chain
        ORDER BY start_id, depth DESC
        """
    )
    rows = (await db.execute(sql, {"ids": rc_ids})).all()
    rewrite_map: dict[uuid.UUID, uuid.UUID] = {}
    for start_id, head_id in rows:
        if head_id is not None and start_id != head_id:
            rewrite_map[start_id] = head_id

    if not rewrite_map:
        return citations, {}

    rewritten: list[Citation] = []
    seen: set[tuple[str, uuid.UUID]] = set()
    for c in citations:
        new_ref = rewrite_map.get(c.ref_id, c.ref_id) if c.kind == "report_content" else c.ref_id
        key = (c.kind, new_ref)
        if key in seen:
            continue
        seen.add(key)
        if new_ref != c.ref_id:
            # Drop title/date populated against the stale row — they get
            # rehydrated against the head by :func:`_enrich_citations` in
            # the next step. Preserve the quote: it is the literal text
            # the model cited and is independent of which version of the
            # row we open.
            rewritten.append(Citation(kind=c.kind, ref_id=new_ref, quote=c.quote))
        else:
            rewritten.append(c)
    return rewritten, rewrite_map


def _rewrite_collapsed_markers(
    answer_md: str,
    *,
    rewrite_map: dict[uuid.UUID, uuid.UUID],
) -> str:
    """Rewrite ``[report:OLD]`` markers to point at the supersede-chain head.

    Mirrors :func:`_collapse_superseded_chain` on the textual side so
    the chip the user clicks opens the canonical version, not the
    stale row. Preserves the optional ``"quote"`` payload on the
    marker (the quote refers to the cited text, which is independent
    of which version of the row we link). No-op when the map is empty
    or the answer has no ``[report:UUID]`` markers.
    """
    if not answer_md or not rewrite_map:
        return answer_md

    def _sub(m: re.Match[str]) -> str:
        kind_long = _CITATION_KIND_LABEL.get(m.group("kind"), m.group("kind"))
        if kind_long != "report_content":
            return m.group(0)
        try:
            ref = uuid.UUID(m.group("uuid"))
        except ValueError:
            return m.group(0)
        new_ref = rewrite_map.get(ref)
        if new_ref is None or new_ref == ref:
            return m.group(0)
        # Re-emit the marker in canonical form: short prefix
        # (``report``), straight quotes, single spaces, no stray ``:``
        # after the closing quote. The sanitiser already handled curly
        # quotes / unquoted payloads / runaway whitespace, so whatever
        # it returns is safe to splice back in.
        quote = _sanitise_quote_payload(m.group("quote_payload"))
        if quote:
            return f'[report:{new_ref} "{quote}"]'
        return f"[report:{new_ref}]"

    return _CITATION_RE.sub(_sub, answer_md)


# Bulk SELECT used by :func:`_enrich_citations` — one query per kind.
# Each statement returns ``(id, title, date_iso)`` rows; ``patient_id``
# is bound for defence-in-depth even though :func:`_validate_citations`
# has already filtered foreign rows out.
_ENRICH_QUERIES: dict[str, str] = {
    "document": (
        "SELECT id, title, document_date FROM documents "
        "WHERE id = ANY(:ids) AND patient_id = :pid AND deleted_at IS NULL"
    ),
    "event": (
        "SELECT id, title, event_date FROM clinical_events "
        "WHERE id = ANY(:ids) AND patient_id = :pid"
    ),
    "report_content": (
        "SELECT rc.id, rc.title, "
        "       COALESCE(rc.signed_at::date, rc.extracted_at::date, rc.created_at::date) "
        "FROM report_contents rc "
        "JOIN clinical_events ce ON ce.id = rc.clinical_event_id "
        "WHERE rc.id = ANY(:ids) AND ce.patient_id = :pid"
    ),
    "clinical_note": (
        "SELECT id, LEFT(body, 80), created_at::date FROM clinical_notes "
        "WHERE id = ANY(:ids) AND patient_id = :pid"
    ),
    # ``summaries`` has no ``title`` column; we synthesize one from the
    # target_kind so the chip reads ``✦ Sintesi paziente`` rather than
    # leaking the raw 8-char UUID. Patch 4 picks a richer label.
    "summary": (
        "SELECT id, 'Sintesi ' || target_kind, updated_at::date FROM summaries WHERE id = ANY(:ids)"
    ),
    # text_chunks have no title; fall back to the first 80 chars of the
    # chunk body so the chip reads as a meaningful excerpt.
    "chunk": (
        "SELECT id, LEFT(text, 80), created_at::date FROM text_chunks "
        "WHERE id = ANY(:ids) AND patient_id = :pid"
    ),
}


async def _enrich_citations(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    citations: list[Citation],
) -> list[Citation]:
    """Populate ``title`` and ``date`` on each citation via one bulk SELECT per kind.

    The model emits ``[kind:UUID]`` markers carrying only the kind and
    the id; this function joins each cited row against its source
    table to attach a human-readable label (``title``) and a sortable
    ISO date (``date``) so the FE renders chips like ``📋 Relazione
    conclusiva 10/04/2026`` instead of ``📋 report_content:cbd2aa0f``.

    Citations whose lookup misses (deleted between cite-time and
    enrich-time, or kind without a registered query) keep their
    pre-enrich state — typically ``title=None`` — and the FE falls
    back to the UUID-short label gracefully.

    ``quote`` is preserved untouched: it was set at parse time from
    the inline ``"snippet"`` payload and is independent of the source
    row's metadata.
    """
    if not citations:
        return citations

    by_kind: dict[str, list[uuid.UUID]] = {}
    for c in citations:
        by_kind.setdefault(c.kind, []).append(c.ref_id)

    meta: dict[tuple[str, uuid.UUID], tuple[str | None, str | None]] = {}
    for kind, ids in by_kind.items():
        sql = _ENRICH_QUERIES.get(kind)
        if sql is None:
            continue
        rows = (await db.execute(sql_text(sql), {"ids": ids, "pid": patient_id})).all()
        for row in rows:
            row_id, title, date_val = row[0], row[1], row[2]
            date_iso = date_val.isoformat() if date_val is not None else None
            meta[(kind, row_id)] = (title, date_iso)

    enriched: list[Citation] = []
    for c in citations:
        key = (c.kind, c.ref_id)
        if key not in meta:
            enriched.append(c)
            continue
        title, date_iso = meta[key]
        enriched.append(
            Citation(
                kind=c.kind,
                ref_id=c.ref_id,
                title=title,
                date=date_iso,
                quote=c.quote,
            )
        )
    return enriched


def _strip_invalid_citation_markers(
    answer_md: str, *, valid_citations: list[Citation], lang: str = "it"
) -> str:
    """Replace ``[kind:UUID]`` markers whose UUID is NOT in ``valid_citations``
    with an inline "source unavailable" sentinel.

    Keeps the answer text coherent when the SQL validator drops a
    cited id (model hallucination / cross-patient guard hit). Without
    this, the FE would render a clickable chip that opens a 404, or
    the user would see an opaque ``[doc:abc...]`` token in the
    output. Both are confusing.
    """
    if not answer_md:
        return answer_md
    # Normalise to the long form so short (``doc``) and long-form
    # (``document``) emitters both line up against the canonical
    # ``citation.kind`` strings.
    valid_keys = {(c.kind, c.ref_id.hex) for c in valid_citations}
    sentinel = (
        "[fonte non disponibile]" if lang.lower().startswith("it") else "[source unavailable]"
    )

    def _sub(m: re.Match[str]) -> str:
        kind_long = _CITATION_KIND_LABEL.get(m.group("kind"), m.group("kind"))
        try:
            ref = uuid.UUID(m.group("uuid"))
        except ValueError:
            return sentinel
        if (kind_long, ref.hex) in valid_keys:
            return m.group(0)
        return sentinel

    return _CITATION_RE.sub(_sub, answer_md)


# ---------------------------------------------------------------------------
# Helpers reused by the API layer
# ---------------------------------------------------------------------------


def serialise_answer(result: AnswerResult) -> dict[str, Any]:
    """Return a JSON-friendly dict for the REST/MCP layer.

    Strips internal fields not meant for the wire (raw arg blobs in
    traces are reduced to the count + names; full traces stay in
    structured logs / provenance events instead).
    """
    return {
        "answer_md": result.answer_md,
        "citations": [
            {
                "kind": c.kind,
                "ref_id": str(c.ref_id),
                "title": c.title,
                "date": c.date,
                "quote": c.quote,
            }
            for c in result.citations
        ],
        "used_tools": result.used_tools,
        "iterations": result.iterations,
        "stop_reason": result.stop_reason,
        "tier": result.tier.value,
        "model_id": result.model_id,
        "usage": result.usage,
        "tool_calls": [
            {
                "name": tc.name,
                "duration_ms": tc.duration_ms,
                "is_error": tc.is_error,
                "result_chars": tc.result_chars,
            }
            for tc in result.tool_calls
        ],
    }


def safe_loads(json_str: str) -> Any:
    """Best-effort JSON decode used by the API layer when post-processing
    structured tool outputs that are not strictly the orchestrator's
    concern."""
    try:
        return json.loads(json_str)
    except (TypeError, ValueError):
        return None
