"""Embedding-side cost calculation, sibling of :mod:`llm_cost`.

Embedding providers bill per token of input only — there is no
``output``, no ``cache_read``, no ``cache_creation`` axis. The shape
is therefore much smaller than :class:`ModelRate`.

We track:

* ``minilm-multi-v1`` — local sentence-transformers MiniLM, free for
  the platform. Entry exists so the wallet path can be uniform on the
  free tier (zero debit).
* Scaleway embedding catalog — qwen3-embedding-8b and
  bge-multilingual-gemma2, both at €0.10/M tokens.

Tier-aware markup mirrors :mod:`llm_cost`: ``standard`` 20%,
``premium`` 30%, ``free`` 0%. The user's monetization decision was to
keep variable markups across both LLM and embedding planes for parity.

Token counting note: when the operator routes embedding calls through
Scaleway, the SDK returns the exact ``prompt_tokens`` count on the
response. When the local MiniLM path runs, no token count is charged
back; the helper rounds up ``len(text) / 4`` as a conservative
estimate so any future "free → paid" upgrade does not retroactively
under-charge. For zero-rated entries the multiplier zeroes the
estimate out, so the estimate is harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from bvphoenix.services.llm_cost import (
    EUR_TO_USD,
    ModelTier,
    UnknownModelError,
    markup_for_tier,
)


@dataclass(frozen=True, slots=True)
class EmbeddingRate:
    """Per-1M-token rate for an embedding model, in USD."""

    input_usd_per_mtok: float


_EMBEDDING_TIERS: dict[str, ModelTier] = {
    "minilm-multi-v1": ModelTier.FREE,
    "qwen3-embedding-8b": ModelTier.PREMIUM,
    "bge-multilingual-gemma2": ModelTier.PREMIUM,
    # OpenAI illustrative entries — operator may extend.
    "text-embedding-3-small": ModelTier.STANDARD,
    "text-embedding-3-large": ModelTier.PREMIUM,
}


_EMBEDDING_RATES: dict[str, EmbeddingRate] = {
    # Local — no provider cost. Stays here so the lookup never throws
    # on a free-tier path.
    "minilm-multi-v1": EmbeddingRate(input_usd_per_mtok=0.0),
    # Scaleway (€0.10/M each, converted via the shared EUR_TO_USD).
    "qwen3-embedding-8b": EmbeddingRate(input_usd_per_mtok=0.10 * EUR_TO_USD),
    "bge-multilingual-gemma2": EmbeddingRate(input_usd_per_mtok=0.10 * EUR_TO_USD),
    # OpenAI proper.
    "text-embedding-3-small": EmbeddingRate(input_usd_per_mtok=0.02),
    "text-embedding-3-large": EmbeddingRate(input_usd_per_mtok=0.13),
}


def estimate_input_tokens(text: str) -> int:
    """Conservative ``len/4`` token estimate.

    Used when the upstream SDK does not return an exact prompt-token
    count (local providers). 4 char/token rounds up so we never
    under-charge on a paid path. Negative or zero text returns 0.
    """
    if not text:
        return 0
    return ceil(len(text) / 4)


def wholesale_embedding_usd(input_tokens: int, *, model_id: str) -> float:
    """Wholesale provider cost in USD for embedding ``input_tokens``."""
    if input_tokens <= 0:
        return 0.0
    try:
        rate = _EMBEDDING_RATES[model_id]
    except KeyError as exc:
        raise UnknownModelError(
            f"No embedding-rate entry for model_id={model_id!r}. "
            "Update bvphoenix.services.embedding_cost._EMBEDDING_RATES."
        ) from exc
    return input_tokens * rate.input_usd_per_mtok / 1_000_000


def billed_embedding_usd(input_tokens: int, *, model_id: str, is_byok: bool) -> float:
    """USD figure to debit from the user's wallet for an embedding call.

    BYOK waives the markup; platform-key calls apply the tier-resolved
    multiplier (mirrors :func:`llm_cost.billed_usd`).
    """
    raw = wholesale_embedding_usd(input_tokens, model_id=model_id)
    if is_byok:
        return raw
    tier = _EMBEDDING_TIERS.get(model_id, ModelTier.STANDARD)
    return raw * (1.0 + markup_for_tier(tier))


def billed_embedding_cents(input_tokens: int, *, model_id: str, is_byok: bool) -> int:
    """Round-up cents form of :func:`billed_embedding_usd`.

    Returns ``0`` for zero-rated models so the wallet path can be
    blindly invoked on the free tier without producing spurious
    one-cent debits.
    """
    dollars = billed_embedding_usd(input_tokens, model_id=model_id, is_byok=is_byok)
    if dollars <= 0:
        return 0
    return ceil(dollars * 100)


def embedding_tier_for_model(model_id: str) -> ModelTier:
    """Tier resolution for an embedding model, defaulting to standard."""
    return _EMBEDDING_TIERS.get(model_id, ModelTier.STANDARD)


__all__ = [
    "EmbeddingRate",
    "billed_embedding_cents",
    "billed_embedding_usd",
    "embedding_tier_for_model",
    "estimate_input_tokens",
    "wholesale_embedding_usd",
]
