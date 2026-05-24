"""Scaleway Generative API LLM provider.

Thin subclass of :class:`OpenAILLM` that pre-configures the
``base_url`` for Scaleway's OpenAI-compatible Generative API and
records that this is a Scaleway-hosted call so downstream cost
attribution can apply the right rate table.

Default model is ``mistral-small-3.2-24b-instruct-2506`` — the
cost-sensitive Italian-friendly tier-`standard` choice from the plan.
The admin can override per-tier by setting
``BVP_SCALEWAY_MODEL_QNA`` (used for the standard Q&A tier) or
``BVP_SCALEWAY_MODEL_PREMIUM`` (used by the premium tier when the
admin opts users in).

Pricing source: ``services/llm_cost.py`` carries the per-1M-token
rates converted EUR→USD; the account that ships M11 ensures every
model id used here has a matching :class:`ModelRate` entry.

Deployment note: co-locating the cluster in the same region as the
provider's Generative API endpoint keeps latency single-digit
milliseconds region-internal. Operators should scope the provider
API key to a dedicated project for isolated billing.
"""

from __future__ import annotations

from bvphoenix.services.llm_openai import OpenAILLM

SCALEWAY_BASE_URL = "https://api.scaleway.ai/v1"

# Default models per tier. The plan binds tier_standard → mistral-small,
# tier_premium → qwen3-235b. Both are mistral/Qwen instruction-tuned
# variants that Scaleway hosts in fr-par.
DEFAULT_STANDARD_MODEL = "mistral-small-3.2-24b-instruct-2506"
DEFAULT_PREMIUM_MODEL = "qwen3-235b-a22b-instruct-2507"


class ScalewayLLM(OpenAILLM):
    """Scaleway-hosted Chat Completions provider.

    Inherits the full agentic + legacy surface from :class:`OpenAILLM`.
    Construction differs only in the default ``base_url`` and the
    advisory model defaults; everything else (tool calling, streaming,
    usage parsing, message translation) is shared.
    """

    provider_id: str = "scaleway"

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str = DEFAULT_STANDARD_MODEL,
        base_url: str = SCALEWAY_BASE_URL,
    ) -> None:
        super().__init__(api_key=api_key, model_id=model_id, base_url=base_url)


__all__ = [
    "DEFAULT_PREMIUM_MODEL",
    "DEFAULT_STANDARD_MODEL",
    "SCALEWAY_BASE_URL",
    "ScalewayLLM",
]
