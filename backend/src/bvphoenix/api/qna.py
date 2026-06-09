"""Q&A REST endpoint with SSE streaming + JSON fallback.

``POST /api/patients/{patient_id}/ask`` runs the orchestrator from
:mod:`bvphoenix.services.qna` and returns either:

* A Server-Sent-Events stream (``Accept: text/event-stream``) of:
  ``tool_call_start``, ``tool_call_end``, ``text_delta``,
  ``citation``, ``done``, ``error``.
* A single JSON payload (default) accumulated from the same internal
  events: ``{answer_md, citations[], used_tools[], iterations,
  stop_reason, tier, model_id, usage}``.

Both paths share the orchestrator. SSE gets enabled when the client
opts in via the ``Accept`` header; the JSON fallback exists so curl
demos and the MCP wrapper do not have to parse SSE.

Wallet gate (M12):
    1. Resolve tier for the live user.
    2. If tier requires a paid call, estimate the upper-bound cost
       from the orchestrator's per-tier turn budget × model rate.
    3. If the user's wallet balance is below the estimate, attempt an
       auto-downgrade to a cheaper tier whose estimate fits the
       balance. Free tier always fits (zero estimate).
    4. After the loop completes, debit the actual cost via the
       existing :func:`bvphoenix.services.billing.debit_llm_call`
       helper. The debit is idempotent on
       ``qna:{patient}:{request_id}``.

Audit / provenance hooks (M9):
    Every ``/ask`` writes a ``qna_request`` provenance event with
    ``author_kind='agent'``, the resolved tier, the tool plan, and
    the cumulative usage. Cross-patient leakage tests gate on the
    citation list referencing only ids of the active patient.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user
from bvphoenix.db.models import ProvenanceEvent, User
from bvphoenix.db.session import get_db
from bvphoenix.services import billing, credits, sponsorship
from bvphoenix.services.ai_tiers import (
    AiTier,
    config_for_tier,
    resolve_tier_for_user,
)
from bvphoenix.services.llm import LLMUsage
from bvphoenix.services.llm_cost import (
    UnknownModelError,
    billed_cents,
    wholesale_usd,
)
from bvphoenix.services.permissions import get_patient_or_404
from bvphoenix.services.qna import (
    AnswerResult,
    Citation,
    answer_question,
    serialise_answer,
)
from bvphoenix.services.rate_limit import LLM_LIMIT, limiter

logger = logging.getLogger(__name__)
router = APIRouter(tags=["qna"])


# Conservative upper-bound budget per call, used by the wallet gate.
# 8k input + 2k output × tier-aware iteration cap. The post-flight
# debit reconciles to the actual usage.
_ESTIMATE_INPUT_TOKENS = 8_000
_ESTIMATE_OUTPUT_TOKENS = 2_000
_ESTIMATE_ITER_BY_TIER: dict[AiTier, int] = {
    AiTier.FREE: 0,
    AiTier.STANDARD: 6,
    AiTier.PREMIUM: 8,
}

# Auto-downgrade order: try premium → standard → free until the
# wallet balance covers the estimate.
_DOWNGRADE_ORDER: list[AiTier] = [AiTier.PREMIUM, AiTier.STANDARD, AiTier.FREE]

# SSE keepalive: emit a comment every N seconds so reverse proxies
# (Traefik/Nginx, Cloudflare) do not close the connection during long
# orchestrator runs. Comment lines start with ``:`` and are ignored
# by SSE parsers per the spec.
_SSE_KEEPALIVE_SECONDS = 15.0


class AskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    lang: Literal["it", "en"] = "it"
    model_override: str | None = Field(default=None, max_length=128)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def _estimate_max_cost_cents(tier: AiTier) -> int:
    """Conservative upper bound on the wallet debit for one call at ``tier``.

    Reads the tier's model from :func:`config_for_tier` and the rate
    table to produce a cents figure. Returns ``0`` for the free tier.
    """
    if tier is AiTier.FREE:
        return 0
    cfg = config_for_tier(tier)
    iters = _ESTIMATE_ITER_BY_TIER.get(tier, 6)
    if iters <= 0:
        return 0
    usage = LLMUsage(
        prompt=_ESTIMATE_INPUT_TOKENS * iters,
        completion=_ESTIMATE_OUTPUT_TOKENS * iters,
    )
    try:
        return billed_cents(usage, model_id=cfg.llm_model_id, is_byok=False)
    except UnknownModelError:
        # Fall back to the wholesale figure when the rate table is
        # silent — better to over-estimate than to skip the gate.
        try:
            dollars = wholesale_usd(usage, model_id=cfg.llm_model_id)
        except UnknownModelError:
            dollars = 0.0
        return max(1, math.ceil(dollars * 100))


async def _resolve_effective_tier(
    db: AsyncSession,
    *,
    user: User | None,
) -> tuple[AiTier, int, int, bool]:
    """Resolve the user's tier and apply auto-downgrade if needed.

    Returns ``(effective_tier, estimated_cost_cents, balance_cents,
    downgraded)``. When ``user`` is ``None`` we always run free; no
    wallet check possible without a subject id.
    """
    if user is None:
        return AiTier.FREE, 0, 0, False

    requested = await resolve_tier_for_user(db, user_subject_id=user.subject_id)
    balance = await credits.get_balance_cents(db, user_subject_id=user.subject_id)
    estimate = _estimate_max_cost_cents(requested)
    if balance >= estimate:
        return requested, estimate, balance, False

    # Auto-downgrade through the chain.
    for candidate in _DOWNGRADE_ORDER:
        if candidate is requested:
            continue
        cand_estimate = _estimate_max_cost_cents(candidate)
        if balance >= cand_estimate:
            return candidate, cand_estimate, balance, True

    # Should never happen (free always costs 0), but be defensive.
    return AiTier.FREE, 0, balance, True


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/patients/{patient_id}/ask")
@limiter.limit(LLM_LIMIT)
async def ask(
    patient_id: uuid.UUID,
    request: Request,
    payload: AskRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> Any:
    """Run the Q&A orchestrator for ``patient_id``.

    Content-negotiation:
        ``Accept: text/event-stream`` → SSE stream.
        Otherwise → block-and-return JSON.
    """
    accept = (request.headers.get("accept") or "").lower()
    wants_stream = "text/event-stream" in accept

    # Patient access gate. Layered:
    #   1. enforce_agent_patient_scope refuses agent tokens whose
    #      assistant has not been granted this patient.
    #   2. can_patient refuses humans who do not have READ_METADATA on
    #      this fascicolo.
    # Both surface as 404 (unknown agent-scope produces 403; we keep
    # the layered ordering so a leaked token cannot enumerate patient
    # ids via timing). Runs before any LLM / wallet work.
    await get_patient_or_404(db, patient_id=patient_id, user=user, request=request)

    # Wallet gate.
    effective_tier, estimate_cents, balance_cents, downgraded = await _resolve_effective_tier(
        db, user=user
    )
    if user is not None and effective_tier is AiTier.FREE and estimate_cents == 0:
        # Reached free via auto-downgrade: balance is too low even for
        # the cheapest paid tier. Surface a 402 so the client can show
        # a top-up prompt instead of silently dropping to free.
        if downgraded and balance_cents < _estimate_max_cost_cents(AiTier.STANDARD):
            return JSONResponse(
                status_code=402,
                content={
                    "detail": "insufficient_credits",
                    "balance_cents": balance_cents,
                    "estimated_max_cost_cents": _estimate_max_cost_cents(AiTier.STANDARD),
                    "fallback_available": "free",
                    "top_up_url": "/settings/billing",
                },
            )

    # Run the orchestrator. ``answer_question`` honours the override.
    request_id = uuid.uuid4()

    if wants_stream:
        return StreamingResponse(
            _stream_answer(
                db,
                patient_id=patient_id,
                payload=payload,
                user=user,
                tier=effective_tier,
                downgraded=downgraded,
                request_id=request_id,
                request=request,
            ),
            media_type="text/event-stream",
        )

    # Block-and-return JSON path.
    try:
        result = await answer_question(
            db,
            patient_id=patient_id,
            query=payload.query,
            lang=payload.lang,
            user_subject_id=(user.subject_id if user else None),
            user=user,
            request=request,
            tier_override=effective_tier,
            model_override=payload.model_override,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("qna orchestrator failed")
        raise HTTPException(status_code=500, detail="qna_orchestrator_failed") from None

    await _post_flight_debit(
        db, user=user, result=result, request_id=request_id, patient_id=patient_id
    )
    await _record_qna_provenance(
        db,
        request=request,
        patient_id=patient_id,
        user=user,
        result=result,
        request_id=request_id,
    )

    body = serialise_answer(result)
    body["effective_tier"] = effective_tier.value
    body["downgraded"] = downgraded
    body["balance_cents"] = balance_cents
    body["request_id"] = str(request_id)
    return body


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


async def _stream_answer(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    payload: AskRequest,
    user: User | None,
    tier: AiTier,
    downgraded: bool,
    request_id: uuid.UUID,
    request: Request,
) -> AsyncIterator[bytes]:
    """SSE generator.

    v1 strategy: run ``answer_question`` to completion, then emit the
    full response as a single ``done`` event. This keeps the surface
    simple while we ship; per-token streaming + tool_call_start
    interleaving will land in v1.5 when ``stream_with_tools`` is wired
    end-to-end (the orchestrator already supports it via
    :meth:`LLMProvider.stream_with_tools`).
    """
    try:
        # Announce the resolved tier so the FE can show the badge
        # immediately instead of waiting for the ``done`` event.
        yield _sse(
            "tier",
            {
                "tier": tier.value,
                "downgraded": downgraded,
                "request_id": str(request_id),
            },
        )

        # Run the orchestrator on a background task and emit a
        # ``: keepalive`` SSE comment every _SSE_KEEPALIVE_SECONDS
        # while it runs. Reverse proxies (Traefik, Nginx, Cloudflare)
        # default to 60 s idle timeouts; without a heartbeat, premium
        # tier calls (8 turns, ~30-60 s) get their connection killed
        # mid-flight and the user sees a 502.
        task: asyncio.Task[AnswerResult] = asyncio.create_task(
            answer_question(
                db,
                patient_id=patient_id,
                query=payload.query,
                lang=payload.lang,
                user_subject_id=(user.subject_id if user else None),
                user=user,
                request=request,
                tier_override=tier,
                model_override=payload.model_override,
            )
        )
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_SSE_KEEPALIVE_SECONDS)
            except TimeoutError:
                yield b": keepalive\n\n"
        result = await task

        # Emit each citation as a discrete event (the FE renders them
        # as chips alongside the answer markdown).
        for cit in result.citations:
            yield _sse("citation", _citation_dict(cit))

        # The full markdown comes through as one delta. v1.5 will
        # split this into incremental deltas via ``stream_with_tools``.
        if result.answer_md:
            yield _sse("text_delta", {"delta": result.answer_md})

        await _post_flight_debit(
            db, user=user, result=result, request_id=request_id, patient_id=patient_id
        )
        await _record_qna_provenance(
            db,
            request=request,
            patient_id=patient_id,
            user=user,
            result=result,
            request_id=request_id,
        )

        yield _sse(
            "done",
            {
                "stop_reason": result.stop_reason,
                "model_id": result.model_id,
                "usage": result.usage,
                "iterations": result.iterations,
                "used_tools": result.used_tools,
                "request_id": str(request_id),
            },
        )
    except Exception as exc:
        logger.exception("qna SSE stream failed")
        yield _sse("error", {"code": "internal_error", "message": str(exc)})


def _sse(event: str, data: dict[str, Any]) -> bytes:
    """Encode one Server-Sent-Event frame."""
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _citation_dict(c: Citation) -> dict[str, Any]:
    """Serialise a :class:`Citation` for the SSE ``citation`` event.

    The FE consumes ``title`` and ``date`` to render a human-readable
    chip; ``quote`` to highlight the matched passage in the preview.
    All three are nullable on the wire — the FE falls back to the
    UUID short when ``title`` is absent and skips the highlight when
    ``quote`` is absent.
    """
    return {
        "kind": c.kind,
        "ref_id": str(c.ref_id),
        "title": c.title,
        "date": c.date,
        "quote": c.quote,
    }


# ---------------------------------------------------------------------------
# Post-flight debit
# ---------------------------------------------------------------------------


async def _post_flight_debit(
    db: AsyncSession,
    *,
    user: User | None,
    result: AnswerResult,
    request_id: uuid.UUID,
    patient_id: uuid.UUID,
) -> None:
    """Book the cost of one Q&A call against the user's wallet.

    Free tier is a no-op (zero usage). Idempotency key includes the
    request id so a stream that reconnects mid-flight cannot double
    charge.

    ``scopes`` carries ``patient`` and ``global`` so the sponsorship
    resolver can pick the most specific active sponsorship: the patient
    owner's wallet (typical "consult" flow) takes precedence over a
    global pool, while a self-pay caller with no matching sponsorship
    keeps the legacy behaviour.
    """
    if user is None or result.tier is AiTier.FREE:
        return
    if not result.usage or not result.model_id:
        return
    scopes = [
        sponsorship.ScopeMatch(scope_kind="patient", scope_id=patient_id),
        sponsorship.ScopeMatch(scope_kind="global", scope_id=None),
    ]
    try:
        await billing.debit_llm_call(
            db,
            user_subject_id=user.subject_id,
            model_id=result.model_id,
            token_usage=result.usage,
            is_byok=False,
            reference_kind="qna",
            reference_id=request_id,
            idempotency_key=f"qna:{request_id}",
            scopes=scopes,
        )
    except credits.InsufficientCreditsError:
        # The pre-flight gate should have prevented this, but the
        # estimate is conservative and could over-shoot. Log and let
        # the user keep the response — calling the LLM was already
        # paid by the platform.
        logger.warning(
            "qna debit failed (insufficient credits) for user=%s request=%s",
            user.subject_id,
            request_id,
        )


# ---------------------------------------------------------------------------
# Provenance audit (M9)
# ---------------------------------------------------------------------------


async def _record_qna_provenance(
    db: AsyncSession,
    *,
    request: Request,
    patient_id: uuid.UUID,
    user: User | None,
    result: AnswerResult,
    request_id: uuid.UUID,
) -> None:
    """Append a ``provenance_events`` row for one Q&A request.

    Required by the platform-wide "AI provenance must be visible"
    invariant: every server-side LLM action against a patient leaves
    an append-only audit trail with ``agent_kind='agent'`` and the
    triggering subject id (when known). The row's metadata captures
    the full tool plan, used model, cumulative usage, and the
    request id used as the wallet idempotency key — operators can
    cross-reference a complaint against the ledger and see exactly
    what the orchestrator did.

    Best-effort: any failure is swallowed and logged. We never want
    to break a successful answer because the audit log refused.
    """
    try:
        agent_kind = "agent"
        agent_token_id = getattr(request.state, "agent_token_id", None)
        agent_assistant_id = getattr(request.state, "agent_assistant_id", None)
        # The check constraint requires either token or assistant id
        # for ``agent_kind='agent'``; when neither is present (human
        # acting through the UI), fall back to the human identity.
        if agent_token_id is None and agent_assistant_id is None:
            agent_kind = "human"
        agent_subject_id = user.subject_id if user else None

        diff = {
            "tier": result.tier.value,
            "model_id": result.model_id,
            "iterations": result.iterations,
            "stop_reason": result.stop_reason,
            "used_tools": result.used_tools,
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
            "citations": [{"kind": c.kind, "ref_id": str(c.ref_id)} for c in result.citations],
        }

        ev = ProvenanceEvent(
            id=uuid.uuid4(),
            recorded_at=datetime.now(UTC),
            target_kind="patient",
            target_id=patient_id,
            activity="extract",
            agent_kind=agent_kind,
            agent_subject_id=agent_subject_id,
            agent_token_id=agent_token_id,
            agent_assistant_id=agent_assistant_id,
            source_kind=None,
            source_id=None,
            diff=diff,
            event_metadata={"qna_request_id": str(request_id)},
        )
        db.add(ev)
        await db.commit()
    except Exception:
        logger.exception("qna provenance write failed for request=%s", request_id)


__all__ = ["router"]
