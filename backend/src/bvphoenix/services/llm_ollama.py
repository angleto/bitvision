"""Ollama-hosted local LLM provider.

Thin subclass of :class:`OpenAILLM` that targets a local Ollama server
exposing its OpenAI-compatible ``/v1`` API. Used for the
``privacy_mode='local'`` deployment path where data must not leave the
cluster and the operator runs models like ``medgemma:27b`` or
``gemma4:*`` on a dedicated GPU node pool.

Tool calling caveat: Ollama-hosted models do not always support the
OpenAI ``tools=`` function-calling extension natively. ``medgemma`` in
particular exposes only chat. For those models the agent loop falls
back to *prompt-based JSON tool calling*: the system prompt instructs
the model to reply with a strict JSON envelope, and the loop helper
parses the output into :class:`LLMToolUseBlock` instances.

The :class:`OllamaLLM` class itself does NOT implement the JSON
fallback — that lives in :mod:`bvphoenix.services.agent_loop` so the
behaviour can be enabled per-model regardless of the provider class.
This adapter just delegates to the SDK exactly like :class:`OpenAILLM`
does. Operators that point this class at a model with native function
calling (e.g. some Mistral variants Ollama can host) get the same
agentic surface as Scaleway and OpenAI.

Deployment note: the reference production cluster is ARM-only with
no GPU today. The local tier is therefore feature-flagged off until
the admin provisions a GPU node pool and deploys the Ollama
StatefulSet.
"""

from __future__ import annotations

from bvphoenix.services.llm_openai import OpenAILLM

DEFAULT_OLLAMA_BASE_URL = "http://ollama.bvphoenix.svc.cluster.local:11434/v1"

# Ollama disregards the API key — but the OpenAI SDK rejects empty
# strings, so we ship a stable placeholder. Override via env if the
# operator runs the Ollama server with an auth proxy in front.
PLACEHOLDER_API_KEY = "ollama-local"

# Default models per tier when local privacy mode is on.
DEFAULT_STANDARD_MODEL = "medgemma:4b"
DEFAULT_PREMIUM_MODEL = "medgemma:27b"


class OllamaLLM(OpenAILLM):
    """Local Ollama-hosted Chat Completions provider.

    Same agentic + legacy surface as :class:`OpenAILLM`. Construction
    differs only in the default ``base_url`` (cluster-local Ollama
    Service) and the advisory model defaults (medgemma family).
    """

    provider_id: str = "ollama"

    def __init__(
        self,
        *,
        api_key: str = PLACEHOLDER_API_KEY,
        model_id: str = DEFAULT_STANDARD_MODEL,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
    ) -> None:
        super().__init__(api_key=api_key, model_id=model_id, base_url=base_url)


__all__ = [
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_PREMIUM_MODEL",
    "DEFAULT_STANDARD_MODEL",
    "PLACEHOLDER_API_KEY",
    "OllamaLLM",
]
