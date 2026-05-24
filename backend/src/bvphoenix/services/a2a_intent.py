"""LLM-driven intent router for the A2A protocol.

Parses a natural-language message into a skill id + structured params.
Uses the configured LLM provider when one is available; falls back to a
keyword heuristic when the provider is in stub mode or if the LLM call
errors out. The keyword path keeps the A2A endpoint useful in CI and
development without external API keys.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx
from pydantic import BaseModel, Field

from bvphoenix.config import get_settings

SKILL_IDS = (
    "dicom-search",
    "similarity-search",
    "image-analysis",
    "patient-fascicolo",
    "radiology-consultation",
    "fascicolo-executive-summary",
)


class IntentResult(BaseModel):
    skill_id: str = Field(..., description="One of the registered A2A skill ids.")
    params: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def _extract_uuid(text: str) -> str | None:
    m = _UUID_RE.search(text)
    if not m:
        return None
    try:
        return str(uuid.UUID(m.group(0)))
    except ValueError:
        return None


def _heuristic_parse(text: str) -> IntentResult:
    """Keyword-based fallback — mirrors the legacy router."""
    lower = text.lower()
    target_uuid = _extract_uuid(text)

    # Match before the patient branch so the summary verb wins.
    if any(
        kw in lower
        for kw in (
            "riassumi il fascicolo",
            "riassunto del fascicolo",
            "executive summary",
            "3-bullet summary",
            "3 bullet summary",
            "executive overview",
            "bullet summary",
        )
    ):
        params: dict[str, Any] = {}
        if target_uuid:
            params["patient_id"] = target_uuid
        if "english" in lower or "in english" in lower:
            params["lang"] = "en"
        elif "italiano" in lower or "in italiano" in lower:
            params["lang"] = "it"
        return IntentResult(skill_id="fascicolo-executive-summary", params=params, confidence=0.6)

    # consultation first — most specific phrasing
    if any(kw in lower for kw in ["consult", "consulenza", "consultation", "help me", "aiutami"]):
        params = {"query": text}
        if target_uuid:
            params["study_id"] = target_uuid
        return IntentResult(skill_id="radiology-consultation", params=params, confidence=0.4)

    if any(kw in lower for kw in ["similar", "simile", "like this", "come questo"]):
        params = {}
        if target_uuid:
            params["target_id"] = target_uuid
        return IntentResult(skill_id="similarity-search", params=params, confidence=0.5)

    if any(kw in lower for kw in ["describe", "analyze", "analisi", "descrivi", "findings"]):
        params = {}
        if target_uuid:
            params["series_id"] = target_uuid
        return IntentResult(skill_id="image-analysis", params=params, confidence=0.5)

    if any(kw in lower for kw in ["patient", "paziente", "fascicolo", "record", "timeline"]):
        params = {}
        if target_uuid:
            params["patient_id"] = target_uuid
        return IntentResult(skill_id="patient-fascicolo", params=params, confidence=0.5)

    if any(kw in lower for kw in ["search", "find", "look for", "cerca", "studi"]):
        return IntentResult(skill_id="dicom-search", params={"query": text}, confidence=0.5)

    # Default to search — least destructive, gives the caller a list back.
    return IntentResult(skill_id="dicom-search", params={"query": text}, confidence=0.2)


_LLM_SYSTEM_PROMPT = """You are an intent router for a medical imaging platform.
Classify the user's message into exactly one of these skill ids and extract parameters:

- dicom-search: metadata / full-text search over DICOM studies. params: {"query": str}
- similarity-search: find visually similar cases. params: {"target_id": uuid, "k": int?, "modality": str?}
- image-analysis: generate an LLM description for a series. params: {"series_id": uuid, "hint": str?}
- patient-fascicolo: patient radiology record. params: {"patient_id": uuid, "section": str?}
- radiology-consultation: multi-step consultation. params: {"study_id": uuid?, "query": str}
- fascicolo-executive-summary: 3-5 bullet executive summary of a patient's fascicolo. params: {"patient_id": uuid, "lang": "it"|"en"?}

Reply with strict JSON only, no prose:
{"skill_id": "...", "params": {...}, "confidence": 0.0-1.0}
"""


async def _anthropic_parse(text: str, *, api_key: str, model_id: str) -> IntentResult | None:
    """Call Anthropic Messages API for structured intent extraction."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            content=json.dumps(
                {
                    "model": model_id,
                    "max_tokens": 400,
                    "system": _LLM_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": text}],
                }
            ),
        )
    resp.raise_for_status()
    data = resp.json()
    raw = "".join(
        block["text"] for block in data.get("content", []) if block.get("type") == "text"
    ).strip()
    # Strip any markdown code fences the model may emit.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    parsed = json.loads(raw)
    skill_id = parsed.get("skill_id")
    if skill_id not in SKILL_IDS:
        return None
    return IntentResult(
        skill_id=skill_id,
        params=parsed.get("params") or {},
        confidence=float(parsed.get("confidence", 0.7)),
    )


async def parse_intent(text: str) -> IntentResult:
    """Return a structured intent for ``text``.

    Strategy:
    1. If the LLM provider is configured (non-stub) try the LLM once.
    2. On any error, or in stub mode, fall back to keyword heuristics.
    """
    text = text.strip()
    if not text:
        return IntentResult(skill_id="dicom-search", params={}, confidence=0.0)

    settings = get_settings()
    if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
        try:
            llm_result = await _anthropic_parse(
                text,
                api_key=settings.anthropic_api_key,
                model_id=settings.llm_default_model,
            )
            if llm_result is not None:
                return llm_result
        except Exception:
            # Deliberate silent fallback — the router should never 500
            # because the LLM misbehaved.
            pass

    return _heuristic_parse(text)
