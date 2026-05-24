"""LLM token-to-USD cost calculation (F7.2 + Scaleway extension).

Converts an :class:`LLMUsage` shard into a dollar figure using a rate
table keyed on the model id. Two flavours of cost come back:

* **wholesale_usd** — what the upstream provider (Anthropic, Scaleway,
  OpenAI, …) actually charges for the call. Always computed. Used for
  internal accounting and for BYOK calls where we bill the user
  exactly what their key was billed (no platform cut).
* **billed_usd** — what we charge the user's wallet. Equals
  ``wholesale_usd * (1 + tier_markup)`` on platform-key calls, and
  ``wholesale_usd`` on BYOK calls (no markup — the user already pays
  the provider directly).

Tier markups:
* ``standard`` (mistral-small Scaleway, Anthropic Sonnet/Haiku, gpt-4o-mini): 20%.
* ``premium`` (qwen3-235b/qwen3.5-397b Scaleway, Anthropic Opus): 30%.
* ``free`` (StubLLM, no LLM tier): 0% — should not produce billable
  calls in the first place but the entry is there for completeness.

The 20% legacy default applies to any model not yet classified; that
keeps existing summarizer / care-phase classifier paths fully
backwards-compatible until each call site opts in to a tier.

Currency conversion: Scaleway prices are listed in EUR; we convert at
:data:`EUR_TO_USD` (refreshed alongside the rate table). The
conversion is documented per-entry so a rates refresh is a one-line
change.

Notation: all costs are in USD. We keep float math because single
calls are in the millicent-to-cent range; the ledger rounds to an
integer cent at debit time so we don't accumulate rounding error
across many calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bvphoenix.services.llm import LLMUsage


class CostTier(StrEnum):
    WHOLESALE = "wholesale"  # what the upstream provider charges
    BILLED = "billed"  # what we charge the user (wholesale * markup)


class ModelTier(StrEnum):
    """Pricing tier — selects the markup applied to a billed call."""

    FREE = "free"
    STANDARD = "standard"
    PREMIUM = "premium"


# EUR→USD conversion used to express Scaleway prices in the same USD
# unit as the existing Anthropic rates. Fixed at table-author time so
# pricing is deterministic; refresh together with the model rates.
# Source: 2026-05 ECB reference, rounded.
EUR_TO_USD: float = 1.10

# Platform markup on platform-key calls. Variable by tier per the
# user's monetization decision; BYOK is always 1.0.
PLATFORM_MARKUP: float = 0.20  # legacy default for unclassified models
MARKUP_BY_TIER: dict[ModelTier, float] = {
    ModelTier.FREE: 0.0,
    ModelTier.STANDARD: 0.20,
    ModelTier.PREMIUM: 0.30,
}


@dataclass(frozen=True, slots=True)
class ModelRate:
    """Per-1M-token rates, all in USD.

    ``cache_read_usd_per_mtok`` is typically ~10% of ``input``; the
    cache write (``cache_creation_usd_per_mtok``) is ~125% of input
    (Anthropic charges a premium on the call that populates the cache
    because it also produces regular output).
    """

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    cache_creation_usd_per_mtok: float


# Tier classification per model. Anything not listed here is treated
# as ``standard`` for billing.
_MODEL_TIERS: dict[str, ModelTier] = {
    # Anthropic
    "claude-opus-4-7": ModelTier.PREMIUM,
    "claude-opus-4-6": ModelTier.PREMIUM,
    "claude-sonnet-4-6": ModelTier.STANDARD,
    "claude-sonnet-4-5": ModelTier.STANDARD,
    "claude-haiku-4-5-20251001": ModelTier.STANDARD,
    # Scaleway — chat
    "mistral-small-3.2-24b-instruct-2506": ModelTier.STANDARD,
    "gpt-oss-120b": ModelTier.STANDARD,
    "gemma-4-26b-a4b-it": ModelTier.STANDARD,
    "llama-3.3-70b-instruct": ModelTier.STANDARD,
    "qwen3-coder-30b-a3b-instruct": ModelTier.STANDARD,
    "pixtral-12b-2409": ModelTier.STANDARD,
    "voxtral-small-24b-2507": ModelTier.STANDARD,
    "gemma-3-27b-it": ModelTier.STANDARD,
    "holo2-30b-a3b": ModelTier.STANDARD,
    "qwen3-235b-a22b-instruct-2507": ModelTier.PREMIUM,
    "qwen3.5-397b-a17b": ModelTier.PREMIUM,
    "devstral-2-123b-instruct-2512": ModelTier.PREMIUM,
    # Local (Ollama). No per-token cost; OPEX recouped via a
    # subscription path the orchestrator chooses; treated as ``free``
    # for the per-call wallet path and ``premium`` only when a
    # paid-subscription user runs them.
    "medgemma:4b": ModelTier.FREE,
    "medgemma:27b": ModelTier.FREE,
    "medgemma1.5:4b": ModelTier.FREE,
    "gemma4:4b": ModelTier.FREE,
    "gemma4:27b": ModelTier.FREE,
    # OpenAI generic — only added when the operator points at it.
    "gpt-4o-mini": ModelTier.STANDARD,
    "gpt-4o": ModelTier.PREMIUM,
    # Stub
    "stub-v0": ModelTier.FREE,
}


def _eur(eur_per_mtok: float) -> float:
    """Convert EUR/Mtok price to USD/Mtok using the fixed conversion.

    Inlined as a tiny helper so each Scaleway entry visibly states its
    EUR origin — refreshing the rate table is then a search-and-replace
    on the EUR figure rather than a math exercise.
    """
    return eur_per_mtok * EUR_TO_USD


# Public rate table. Keys are the model-id strings the upstream SDK
# advertises; callers with a non-exact string should normalise before
# looking up. Anthropic prices reflect 2026-04 public pricing; Scaleway
# prices reflect the 2026-05 Scaleway pricing page (in EUR, converted
# via :data:`EUR_TO_USD`). Update in one place when any vendor refreshes
# rates.
_MODEL_RATES: dict[str, ModelRate] = {
    # ------------------------------------------------------------------
    # Anthropic Messages API (USD-native)
    # ------------------------------------------------------------------
    "claude-opus-4-7": ModelRate(
        input_usd_per_mtok=15.0,
        output_usd_per_mtok=75.0,
        cache_read_usd_per_mtok=1.50,
        cache_creation_usd_per_mtok=18.75,
    ),
    "claude-opus-4-6": ModelRate(
        input_usd_per_mtok=15.0,
        output_usd_per_mtok=75.0,
        cache_read_usd_per_mtok=1.50,
        cache_creation_usd_per_mtok=18.75,
    ),
    "claude-sonnet-4-6": ModelRate(
        input_usd_per_mtok=3.0,
        output_usd_per_mtok=15.0,
        cache_read_usd_per_mtok=0.30,
        cache_creation_usd_per_mtok=3.75,
    ),
    "claude-sonnet-4-5": ModelRate(
        input_usd_per_mtok=3.0,
        output_usd_per_mtok=15.0,
        cache_read_usd_per_mtok=0.30,
        cache_creation_usd_per_mtok=3.75,
    ),
    "claude-haiku-4-5-20251001": ModelRate(
        input_usd_per_mtok=1.0,
        output_usd_per_mtok=5.0,
        cache_read_usd_per_mtok=0.10,
        cache_creation_usd_per_mtok=1.25,
    ),
    # ------------------------------------------------------------------
    # Scaleway Generative API (EUR-native, converted via EUR_TO_USD)
    # ------------------------------------------------------------------
    "mistral-small-3.2-24b-instruct-2506": ModelRate(
        input_usd_per_mtok=_eur(0.15),
        output_usd_per_mtok=_eur(0.35),
        cache_read_usd_per_mtok=0.0,  # Scaleway has no prompt cache today
        cache_creation_usd_per_mtok=0.0,
    ),
    "gpt-oss-120b": ModelRate(
        input_usd_per_mtok=_eur(0.15),
        output_usd_per_mtok=_eur(0.60),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "gemma-4-26b-a4b-it": ModelRate(
        input_usd_per_mtok=_eur(0.25),
        output_usd_per_mtok=_eur(0.50),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "llama-3.3-70b-instruct": ModelRate(
        input_usd_per_mtok=_eur(0.90),
        output_usd_per_mtok=_eur(0.90),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "qwen3-coder-30b-a3b-instruct": ModelRate(
        input_usd_per_mtok=_eur(0.20),
        output_usd_per_mtok=_eur(0.80),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "pixtral-12b-2409": ModelRate(
        input_usd_per_mtok=_eur(0.20),
        output_usd_per_mtok=_eur(0.20),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "voxtral-small-24b-2507": ModelRate(
        input_usd_per_mtok=_eur(0.15),
        output_usd_per_mtok=_eur(0.35),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "gemma-3-27b-it": ModelRate(
        input_usd_per_mtok=_eur(0.25),
        output_usd_per_mtok=_eur(0.50),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "holo2-30b-a3b": ModelRate(
        input_usd_per_mtok=_eur(0.30),
        output_usd_per_mtok=_eur(0.70),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "qwen3-235b-a22b-instruct-2507": ModelRate(
        input_usd_per_mtok=_eur(0.75),
        output_usd_per_mtok=_eur(2.25),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "qwen3.5-397b-a17b": ModelRate(
        input_usd_per_mtok=_eur(0.60),
        output_usd_per_mtok=_eur(3.60),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    "devstral-2-123b-instruct-2512": ModelRate(
        input_usd_per_mtok=_eur(0.40),
        output_usd_per_mtok=_eur(2.00),
        cache_read_usd_per_mtok=0.0,
        cache_creation_usd_per_mtok=0.0,
    ),
    # ------------------------------------------------------------------
    # OpenAI proper — illustrative entries (operator may extend)
    # ------------------------------------------------------------------
    "gpt-4o-mini": ModelRate(
        input_usd_per_mtok=0.15,
        output_usd_per_mtok=0.60,
        cache_read_usd_per_mtok=0.075,
        cache_creation_usd_per_mtok=0.0,
    ),
    "gpt-4o": ModelRate(
        input_usd_per_mtok=2.50,
        output_usd_per_mtok=10.00,
        cache_read_usd_per_mtok=1.25,
        cache_creation_usd_per_mtok=0.0,
    ),
    # ------------------------------------------------------------------
    # Local (Ollama) — no per-token cost. Entry exists so cost lookup
    # never throws on local-tier traffic; subscription billing is owned
    # by services/credits.py, not by this rate table.
    # ------------------------------------------------------------------
    "medgemma:4b": ModelRate(0.0, 0.0, 0.0, 0.0),
    "medgemma:27b": ModelRate(0.0, 0.0, 0.0, 0.0),
    "medgemma1.5:4b": ModelRate(0.0, 0.0, 0.0, 0.0),
    "gemma4:4b": ModelRate(0.0, 0.0, 0.0, 0.0),
    "gemma4:27b": ModelRate(0.0, 0.0, 0.0, 0.0),
    "stub-v0": ModelRate(0.0, 0.0, 0.0, 0.0),
}


def tier_for_model(model_id: str) -> ModelTier:
    """Resolve the pricing tier for a model id, defaulting to standard."""
    if model_id in _DYNAMIC_TIERS:
        try:
            return ModelTier(_DYNAMIC_TIERS[model_id])
        except ValueError:
            pass
    return _MODEL_TIERS.get(model_id, ModelTier.STANDARD)


def markup_for_tier(tier: ModelTier) -> float:
    """Return the markup multiplier for a tier (e.g. 0.20, 0.30)."""
    return MARKUP_BY_TIER.get(tier, PLATFORM_MARKUP)


def markup_for_model(model_id: str) -> float:
    """Per-model markup (overrides tier default when set in DB).

    Returns a multiplier (0.20, 0.30, ...). When the DB row has
    ``markup_pct = NULL``, falls back to the tier default. This is what
    ``billed_usd`` actually consults; ``markup_for_tier`` is kept as the
    callable-level fallback for unmapped models.
    """
    override = _DYNAMIC_MARKUP_PCT.get(model_id)
    if override is not None:
        return override / 100.0
    return markup_for_tier(tier_for_model(model_id))


class UnknownModelError(KeyError):
    """Raised when the usage cannot be priced because the model_id is
    not in the rate table. Signals an ops task: refresh ``_MODEL_RATES``
    or guard the call path until support is added."""


# Dynamic overrides loaded from ``llm_rate_cards`` on app startup and
# refreshed after each admin PATCH. None of these are populated when
# the DB is unreachable, in which case we fall back to the static
# ``_MODEL_RATES`` / ``_MODEL_TIERS`` dicts above; this keeps the unit
# tests (which monkey-patch the dicts directly) and the bootstrap path
# working without a live DB. Kept as module-level dicts so reads from
# hot paths stay synchronous.
_DYNAMIC_RATES: dict[str, ModelRate] = {}
_DYNAMIC_TIERS: dict[str, str] = {}
_DYNAMIC_MARKUP_PCT: dict[str, float] = {}


def set_dynamic_rate(
    model_id: str,
    *,
    rate: ModelRate,
    tier_hint: str = "standard",
    markup_pct: float | None = None,
) -> None:
    """Inject a rate at runtime. Used by ``refresh_rate_cards`` after a
    DB read; tests can also call this directly to override a model's
    rate for the duration of a test."""
    _DYNAMIC_RATES[model_id] = rate
    _DYNAMIC_TIERS[model_id] = tier_hint
    if markup_pct is None:
        _DYNAMIC_MARKUP_PCT.pop(model_id, None)
    else:
        _DYNAMIC_MARKUP_PCT[model_id] = float(markup_pct)


def clear_dynamic_rate(model_id: str) -> None:
    """Remove a model from the dynamic overrides. Falls back to the
    static dict for the next lookup."""
    _DYNAMIC_RATES.pop(model_id, None)
    _DYNAMIC_TIERS.pop(model_id, None)
    _DYNAMIC_MARKUP_PCT.pop(model_id, None)


def reset_dynamic_rates() -> None:
    """Drop every override (used at the start of ``refresh_rate_cards``
    so deactivated rows disappear from the runtime view)."""
    _DYNAMIC_RATES.clear()
    _DYNAMIC_TIERS.clear()
    _DYNAMIC_MARKUP_PCT.clear()


def _rate_for(model_id: str) -> ModelRate:
    if model_id in _DYNAMIC_RATES:
        return _DYNAMIC_RATES[model_id]
    try:
        return _MODEL_RATES[model_id]
    except KeyError as exc:
        raise UnknownModelError(
            f"No rate table entry for model_id={model_id!r}. "
            "Add a row to ``llm_rate_cards`` (admin UI: /admin/llm-rates)."
        ) from exc


def wholesale_usd(usage: LLMUsage, *, model_id: str) -> float:
    """Compute the raw Anthropic-side cost of a call in USD."""
    rate = _rate_for(model_id)
    # Each counter is "number of tokens"; the rate is per 1M tokens.
    return (
        usage.prompt * rate.input_usd_per_mtok
        + usage.completion * rate.output_usd_per_mtok
        + usage.cache_read_tokens * rate.cache_read_usd_per_mtok
        + usage.cache_creation_tokens * rate.cache_creation_usd_per_mtok
    ) / 1_000_000


def billed_usd(usage: LLMUsage, *, model_id: str, is_byok: bool) -> float:
    """What we should debit from the user's wallet for this call.

    BYOK: wholesale (markup waived — the user pays the provider
    directly, no platform cut). Platform key: wholesale * (1 + markup),
    where the markup is the per-model override when set, otherwise the
    tier default.
    """
    raw = wholesale_usd(usage, model_id=model_id)
    if is_byok:
        return raw
    return raw * (1.0 + markup_for_model(model_id))


def billed_cents(usage: LLMUsage, *, model_id: str, is_byok: bool) -> int:
    """Same as :func:`billed_usd` but rounded up to whole cents so the
    ledger never persists a fractional charge. Upward rounding means
    the platform never *under*-charges on a single call; an honest
    operator can still discount retroactively, but we do not accumulate
    rounding-down losses across millions of short calls."""
    from math import ceil

    dollars = billed_usd(usage, model_id=model_id, is_byok=is_byok)
    return ceil(dollars * 100)


__all__ = [
    "EUR_TO_USD",
    "MARKUP_BY_TIER",
    "PLATFORM_MARKUP",
    "CostTier",
    "ModelRate",
    "ModelTier",
    "UnknownModelError",
    "billed_cents",
    "billed_usd",
    "clear_dynamic_rate",
    "markup_for_model",
    "markup_for_tier",
    "reset_dynamic_rates",
    "set_dynamic_rate",
    "tier_for_model",
    "wholesale_usd",
]
