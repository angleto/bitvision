"""LLM wallet debit wiring (F7.3 follow-up).

Bridges :mod:`bvphoenix.services.llm_cost` and
:mod:`bvphoenix.services.credits` so every call path that completes an
LLM request can book its cost against the caller's wallet with a single
helper call. Keeps the LLM endpoints free of knowledge about cents,
markup, or idempotency keys.

Scope today: platform-key calls only. BYOK (``is_byok=True``) short-
circuits because the user is paying the provider directly, so the
platform has nothing to bill. Future work is to route the call site
through ``services.byok`` first, set ``is_byok`` accordingly, and let
this helper keep the same shape.

Failure modes:

* Unknown model id (``llm_cost._MODEL_RATES`` missing an entry) logs a
  warning and returns ``None`` — we would rather under-bill than
  refuse the response after the LLM has already run.
* Zero-token usage (stub provider, cache-only hit) returns ``None``.
* :class:`credits.InsufficientCreditsError` propagates — the caller
  chooses whether to swallow it (keep the response) or surface it
  (e.g. stream endpoints can refund downstream).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.services import credits, sponsorship
from bvphoenix.services.llm import LLMUsage
from bvphoenix.services.llm_cost import UnknownModelError, billed_cents
from bvphoenix.services.sponsorship import (
    BillingTarget,
    CapExceededError,
    ScopeMatch,
    SponsorshipError,
)

logger = logging.getLogger(__name__)


def _usage_from_dict(token_usage: dict | None) -> LLMUsage | None:
    """Reconstruct an :class:`LLMUsage` from the serialised ``token_usage``
    dicts stored on ``Summary`` / ``Consultation`` rows.

    Accepts the canonical keys emitted by :meth:`LLMUsage.as_dict`
    (``prompt`` / ``completion`` / ``cache_read_tokens`` /
    ``cache_creation_tokens``). Also tolerates the Anthropic-API names
    (``input_tokens`` / ``output_tokens``) in case an older call path
    stored the raw provider payload, so we can still bill those rows.

    Returns ``None`` when the dict does not carry usable counters.
    """
    if not isinstance(token_usage, dict):
        return None

    def _int(*keys: str) -> int:
        for k in keys:
            v = token_usage.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, float):
                return int(v)
        return 0

    prompt = _int("prompt", "input_tokens", "input")
    completion = _int("completion", "output_tokens", "output")
    cache_read = _int("cache_read_tokens", "cache_read_input_tokens")
    cache_creation = _int("cache_creation_tokens", "cache_creation_input_tokens")
    if prompt + completion + cache_read + cache_creation == 0:
        return None
    return LLMUsage(
        prompt=prompt,
        completion=completion,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    )


async def debit_llm_call(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID | None,
    model_id: str | None,
    token_usage: dict | None,
    is_byok: bool,
    reference_kind: str,
    reference_id: uuid.UUID,
    idempotency_key: str | None = None,
    scopes: list[ScopeMatch] | None = None,
) -> credits.LedgerMovement | None:
    """Book the cost of one LLM call against the appropriate wallet.

    ``user_subject_id`` is the *caller* (who originated the spend).
    When ``scopes`` is provided, the resolver in
    :mod:`bvphoenix.services.sponsorship` looks for an active
    sponsorship that matches one of the scopes; the matched sponsor's
    wallet is debited instead of the caller's, and the sponsorship's
    cap_cents counter is advanced atomically.

    Returns the :class:`LedgerMovement` when a row was written, ``None``
    when billing was skipped (no user, BYOK, zero tokens, unknown
    model).

    Propagates :class:`credits.InsufficientCreditsError` so callers can
    decide their own policy. When a sponsorship is matched but its cap
    would be exceeded, falls back to charging the caller's wallet (the
    sponsorship is consumed up to its cap, the residual goes on the
    caller). This is the trade-off chosen to avoid losing track of a
    call already billed upstream by the LLM provider; an alternative
    "refuse the debit and surface 402 mid-flight" can be wired by
    flipping ``CapExceededError`` from caught to re-raised below.

    The idempotency key defaults to ``"{reference_kind}:{reference_id}"``
    which makes retries of the same consultation / summary / annotation
    land on the same ledger row instead of double-charging.
    """
    if user_subject_id is None or is_byok or not model_id:
        return None

    usage = _usage_from_dict(token_usage)
    if usage is None:
        return None

    try:
        cents = billed_cents(usage, model_id=model_id, is_byok=False)
    except UnknownModelError:
        logger.warning(
            "llm debit skipped: model_id=%r not in rate table (%s:%s)",
            model_id,
            reference_kind,
            reference_id,
        )
        return None
    if cents <= 0:
        return None

    target: BillingTarget
    if scopes:
        target = await sponsorship.resolve_billing(
            db,
            caller_subject_id=user_subject_id,
            scopes=scopes,
            estimated_cents=cents,
        )
    else:
        target = BillingTarget(
            billed_subject_id=user_subject_id,
            caller_subject_id=user_subject_id,
            sponsorship=None,
        )

    key = idempotency_key or f"{reference_kind}:{reference_id}"
    notes = {
        "model_id": model_id,
        "usage": usage.as_dict(),
    }
    if target.is_sponsored and target.sponsorship is not None:
        notes["sponsorship_id"] = str(target.sponsorship.id)
        try:
            await sponsorship.consume_sponsorship(
                db,
                sponsorship_id=target.sponsorship.id,
                amount_cents=cents,
            )
        except (CapExceededError, SponsorshipError) as exc:
            logger.warning(
                "sponsorship consume failed, falling back to caller wallet",
                extra={
                    "caller_subject_id": str(user_subject_id),
                    "sponsorship_id": str(target.sponsorship.id),
                    "reason": str(exc),
                },
            )
            target = BillingTarget(
                billed_subject_id=user_subject_id,
                caller_subject_id=user_subject_id,
                sponsorship=None,
            )
            notes.pop("sponsorship_id", None)

    return await credits.debit(
        db,
        user_subject_id=target.billed_subject_id,
        amount_cents=cents,
        idempotency_key=key,
        reference_kind=reference_kind,
        reference_id=reference_id,
        notes=notes,
        caller_subject_id=target.caller_subject_id,
        sponsorship_id=(target.sponsorship.id if target.sponsorship else None),
    )


__all__ = ["debit_llm_call"]
