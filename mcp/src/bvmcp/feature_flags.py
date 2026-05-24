"""Backend feature-flag probe for dynamic MCP tool registration.

The MCP toolkit is filtered at ``list_tools()`` time against the
flags returned by ``GET /api/system/features``. When the backend
reports a feature as off, every tool tagged as depending on that
feature is hidden from the response — both ``ALL_TOOLS`` and the
per-call dispatch entry are filtered, so an LLM client that lists
the toolkit and immediately invokes a tool by name only sees the
ones the backend can actually serve.

Flags surfaced by the backend right now:

* ``llm_classifier`` — true when ``BVP_ANTHROPIC_API_KEY`` is non-
  empty and ``BVP_LLM_PROVIDER`` is one of ``"anthropic"`` / ``"auto"``.
  Gates ``propose_care_phases`` and ``apply_phase_proposal``: in BYO
  mode (the default — no server-side LLM provisioned) the agent
  classifies in its own LLM and uses ``create_care_phase`` +
  ``assign_event_to_phase`` directly. Surfacing the proposal /
  apply tools without a real classifier just produces 502s.

The probe runs once per process: the first ``list_tools()`` call
fetches and caches the result. Backend config changes propagate on
the next MCP pod restart, which is acceptable: feature toggles are
operator-grade events, not per-request churn.

If the probe fails (backend unreachable / 5xx), the module defaults
to ``False`` for every flag — surface the smaller toolkit rather
than risk advertising tools that would 502 at call time.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from bvmcp.config import get_settings

_log = logging.getLogger("bvmcp.feature_flags")


@dataclass(frozen=True, slots=True)
class FeatureFlags:
    llm_classifier: bool

    @classmethod
    def safe_default(cls) -> FeatureFlags:
        """Returned when the backend probe fails. Conservative: every
        feature off, the MCP exposes only the BYO-friendly subset."""
        return cls(llm_classifier=False)


_FLAGS: FeatureFlags | None = None
_LOCK = asyncio.Lock()


async def get_feature_flags() -> FeatureFlags:
    """Return the cached flag snapshot, fetching it on first call.

    Thread-safe via :class:`asyncio.Lock`; the http call only fires
    once even under concurrent ``list_tools()`` traffic during
    startup. Subsequent calls are O(1).
    """
    global _FLAGS
    if _FLAGS is not None:
        return _FLAGS
    async with _LOCK:
        if _FLAGS is not None:
            return _FLAGS
        _FLAGS = await _probe_backend()
    return _FLAGS


async def _probe_backend() -> FeatureFlags:
    settings = get_settings()
    url = f"{settings.backend_base_url}/api/system/features"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _log.warning("feature flag probe failed; defaulting to all-off (%s)", exc)
        return FeatureFlags.safe_default()
    return FeatureFlags(
        llm_classifier=bool(payload.get("llm_classifier", False)),
    )


# Tool name → required feature flag attribute. A tool whose required
# flag is ``False`` is filtered out of ``list_tools()`` and from the
# dispatch table. Tools NOT in this map are unconditionally exposed.
TOOL_FEATURE_DEPENDENCY: dict[str, str] = {
    "propose_care_phases": "llm_classifier",
    "apply_phase_proposal": "llm_classifier",
}


def is_tool_available(tool_name: str, flags: FeatureFlags) -> bool:
    """``True`` when ``tool_name`` has no feature dependency, or its
    required flag is on. Used by the server transports to decide
    whether to surface the tool."""
    feature = TOOL_FEATURE_DEPENDENCY.get(tool_name)
    if feature is None:
        return True
    return bool(getattr(flags, feature, False))


__all__ = [
    "TOOL_FEATURE_DEPENDENCY",
    "FeatureFlags",
    "get_feature_flags",
    "is_tool_available",
]
