"""Auto-tagging logic for DICOM studies / series / instances.

Tag-first search (DESIGN.md §5) is the primary strategy for keeping the
result set small without hitting a full-text index for every query. This
module extracts namespaced tags from free-text descriptors (study /
series description, report body, annotation payload) using two stages:

1. **Lexicon stage** — a hand-curated Italian + English medical keyword
   table drives deterministic regex matches. Fast, offline, predictable.
2. **LLM stage (optional)** — when ``settings.autotag_use_llm`` is set
   and the input is long enough to be worth the round-trip, call the
   configured LLM provider with a strict JSON schema prompt and merge
   any new tags in. Off by default so the worker stays cheap.

Every emitted tag is stamped with ``source='auto'`` + a per-rule
``confidence``. The persistence path keeps manual (``source='manual'``)
tags untouched — auto-tagging never clobbers a human decision.

Namespaces (see Unit S5 spec):
- ``modality:*``    CT, MR, XR, US, PT, NM, MG, ...
- ``anatomy:*``     lung, heart, liver, brain, bone, ... (with ``/`` sub-paths)
- ``body:*``        thorax, abdomen, head, spine, pelvis, extremity, ...
- ``finding:*``     nodule, mass, fracture, hemorrhage, edema, effusion, ...
- ``pathology:*``   pneumonia, cancer, stroke, embolism, ...
- ``technique:*``   contrast, angio, diffusion, perfusion, ...
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

# ---------------------------------------------------------------------------
# Lexicon — keyword → (namespace, value, confidence)
#
# Keys are case-insensitive regex alternations. Values are matched by
# word-boundary where reasonable so ``ct`` does not fire inside ``actual``.
# Confidence defaults to 0.85 for specific terms, lower for ambiguous
# ones (``mass``, ``edema``). Italian + English variants share the same
# tag value so downstream search is language-neutral.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TagCandidate:
    namespace: str
    value: str
    confidence: float


# Each rule: (pattern, namespace, value, confidence)
# Pattern compiled at module load. Kept as plain tuples to make it
# trivial to extend or move into a YAML file later without touching the
# extraction algorithm.
_RAW_RULES: list[tuple[str, str, str, float]] = [
    # --- modality -----------------------------------------------------------
    # DICOM codes are authoritative when present. Keep pattern tight so
    # "ct" inside a word does not match; the spaces / boundary handle it.
    (r"\b(ct|tc|computed[-\s]tomography|tomografia[-\s]computer\w*)\b", "modality", "CT", 0.95),
    (r"\b(mr|rm|mri|risonanza[-\s]magnetica|magnetic[-\s]resonance)\b", "modality", "MR", 0.95),
    (r"\b(xr|rx|x[-\s]?ray|radiograph\w*|radiografia)\b", "modality", "XR", 0.9),
    (r"\b(us|ultrasound|ecografia|ecograf\w+)\b", "modality", "US", 0.9),
    (r"\b(pet|pt[-\s]?scan|positron[-\s]emission)\b", "modality", "PT", 0.9),
    (r"\b(spect|scintigraf\w+|nuclear[-\s]medicine)\b", "modality", "NM", 0.85),
    (r"\b(mammograf\w+|mammography|mg[-\s]scan)\b", "modality", "MG", 0.9),
    # --- anatomy (top-level organ / structure) ------------------------------
    (r"\b(lung|polmon\w+|pulmonary|polmonare)\b", "anatomy", "lung", 0.9),
    (r"\b(upper[-\s]lobe|lobo[-\s]superiore)\b", "anatomy", "lung/upper-lobe", 0.85),
    (r"\b(lower[-\s]lobe|lobo[-\s]inferiore)\b", "anatomy", "lung/lower-lobe", 0.85),
    (r"\b(middle[-\s]lobe|lobo[-\s]medio)\b", "anatomy", "lung/middle-lobe", 0.85),
    (r"\b(heart|cuore|cardiac|cardiaco)\b", "anatomy", "heart", 0.9),
    (r"\b(left[-\s]ventricle|ventricolo[-\s]sinistro|lv)\b", "anatomy", "heart/ventricle", 0.75),
    (r"\b(right[-\s]ventricle|ventricolo[-\s]destro|rv)\b", "anatomy", "heart/ventricle", 0.75),
    (r"\b(liver|fegato|hepatic|epatico|epatica)\b", "anatomy", "liver", 0.9),
    (r"\b(brain|cervello|cerebral|cerebrale)\b", "anatomy", "brain", 0.9),
    (r"\b(kidney|rene|reni|renal|renale)\b", "anatomy", "kidney", 0.9),
    (r"\b(spleen|milza|splenic)\b", "anatomy", "spleen", 0.9),
    (r"\b(pancreas|pancreatico|pancreatic)\b", "anatomy", "pancreas", 0.9),
    (r"\b(bone|osseo|ossea|skelet\w+|scheletro|scheletrico)\b", "anatomy", "bone", 0.85),
    (r"\b(vertebra\w*|spinal[-\s]cord|midollo[-\s]spinale)\b", "anatomy", "spine", 0.85),
    (r"\b(aorta|aortic|aortico|aortica)\b", "anatomy", "aorta", 0.9),
    (r"\b(colon|colic|intestin\w+)\b", "anatomy", "colon", 0.85),
    (r"\b(bladder|vescic\w+)\b", "anatomy", "bladder", 0.85),
    (r"\b(prostate|prostatico|prostatica)\b", "anatomy", "prostate", 0.9),
    (r"\b(breast|mammell\w+|mammari\w+)\b", "anatomy", "breast", 0.9),
    # --- body region (coarser than anatomy) ---------------------------------
    (r"\b(thorax|chest|torace|toracico|toracica)\b", "body", "thorax", 0.85),
    (r"\b(abdomen|abdominal|addome|addominale)\b", "body", "abdomen", 0.85),
    (r"\b(head|cranio|cranium|skull|testa)\b", "body", "head", 0.85),
    (r"\b(spine|rachide|colonna[-\s]vertebrale)\b", "body", "spine", 0.85),
    (r"\b(pelvis|pelvic|pelvi|pelvico|pelvica|bacino)\b", "body", "pelvis", 0.85),
    (
        r"\b(extremity|estremità|arto|arti|upper[-\s]limb|lower[-\s]limb)\b",
        "body",
        "extremity",
        0.8,
    ),
    (r"\b(neck|collo|cervical|cervicale)\b", "body", "neck", 0.85),
    # --- findings (descriptive, less specific than pathology) ---------------
    (r"\b(nodule|nodulo|noduli|nodular\w*)\b", "finding", "nodule", 0.85),
    (r"\b(mass|massa|masse)\b", "finding", "mass", 0.7),
    (r"\b(fracture|frattur\w+)\b", "finding", "fracture", 0.9),
    (r"\b(hemorrhage|emorragia|bleeding|sanguinamento)\b", "finding", "hemorrhage", 0.85),
    (r"\b(edema|edem\w*)\b", "finding", "edema", 0.75),
    (r"\b(effusion|versamento|versamenti)\b", "finding", "effusion", 0.85),
    (r"\b(cyst|cisti|cystic)\b", "finding", "cyst", 0.85),
    (r"\b(lesion|lesion\w+)\b", "finding", "lesion", 0.7),
    (r"\b(calcification|calcificazion\w+)\b", "finding", "calcification", 0.85),
    (r"\b(opacity|opacit\w+)\b", "finding", "opacity", 0.75),
    (r"\b(consolidation|consolidament\w+|addensament\w+)\b", "finding", "consolidation", 0.85),
    (r"\b(atelectasis|atelettasia)\b", "finding", "atelectasis", 0.9),
    # --- pathology (diagnostic / disease entities) --------------------------
    (r"\b(pneumonia|polmonite)\b", "pathology", "pneumonia", 0.9),
    (
        r"\b(cancer|cancro|tumor|tumore|malign\w+|carcinoma|carcinoma\w+)\b",
        "pathology",
        "cancer",
        0.8,
    ),
    (r"\b(stroke|ictus|cerebrovascular[-\s]accident|cva)\b", "pathology", "stroke", 0.9),
    (r"\b(embolism|embolia|pulmonary[-\s]embolism|pe)\b", "pathology", "embolism", 0.85),
    (r"\b(aneurysm|aneurisma)\b", "pathology", "aneurysm", 0.9),
    (r"\b(covid|covid-19|sars-cov-2)\b", "pathology", "covid", 0.95),
    (r"\b(tubercul\w+|tbc|tb)\b", "pathology", "tuberculosis", 0.9),
    (r"\b(pneumothorax|pnx)\b", "pathology", "pneumothorax", 0.9),
    (r"\b(metastas\w+|mts)\b", "pathology", "metastasis", 0.85),
    (r"\b(osteoporos\w+)\b", "pathology", "osteoporosis", 0.9),
    # --- technique (acquisition / protocol hints) ---------------------------
    (r"\b(contrast|contrasto|mdc|mezzo[-\s]di[-\s]contrasto)\b", "technique", "contrast", 0.9),
    (r"\b(angio|angiograf\w+|ct[-\s]?angio)\b", "technique", "angio", 0.9),
    (r"\b(diffusion|dwi|diffusione)\b", "technique", "diffusion", 0.9),
    (r"\b(perfusion|perfusione)\b", "technique", "perfusion", 0.9),
    (r"\b(t1[-\s]weighted|t1w|t1)\b", "technique", "t1", 0.7),
    (r"\b(t2[-\s]weighted|t2w|t2)\b", "technique", "t2", 0.7),
    (r"\b(flair)\b", "technique", "flair", 0.9),
]


# Compile once. ``IGNORECASE`` is the only flag we need — the patterns
# use explicit word boundaries so DOTALL / MULTILINE would be wrong.
_RULES: list[tuple[re.Pattern[str], str, str, float]] = [
    (re.compile(pattern, re.IGNORECASE), ns, val, conf) for (pattern, ns, val, conf) in _RAW_RULES
]


# Minimum text length before we even bother calling the LLM — anything
# shorter is dominated by the lexicon stage.
_LLM_MIN_CHARS = 200


def extract_tags_from_text(text: str) -> list[TagCandidate]:
    """Run every lexicon rule against ``text`` and return deduplicated candidates.

    Later rules with higher confidence override earlier lower-confidence
    duplicates for the same ``(namespace, value)`` pair.
    """
    if not text:
        return []
    by_key: dict[tuple[str, str], TagCandidate] = {}
    for pattern, ns, val, conf in _RULES:
        if not pattern.search(text):
            continue
        key = (ns, val)
        existing = by_key.get(key)
        if existing is None or conf > existing.confidence:
            by_key[key] = TagCandidate(namespace=ns, value=val, confidence=conf)
    return list(by_key.values())


def extract_tags_from_payload(payload: object) -> list[TagCandidate]:
    """Walk an arbitrary JSON-like payload and feed every string leaf
    to :func:`extract_tags_from_text`. Lets us run the same lexicon
    across ``annotations.payload`` (free-shape JSONB) without each
    caller having to serialise it explicitly.
    """
    joined = _walk_strings(payload)
    if not joined:
        return []
    return extract_tags_from_text(joined)


def _walk_strings(node: object) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return " ".join(_walk_strings(v) for v in node.values())
    if isinstance(node, (list, tuple, set)):
        return " ".join(_walk_strings(v) for v in node)
    return ""


def merge_candidates(*groups: list[TagCandidate]) -> list[TagCandidate]:
    """Union of several candidate lists, keeping the max confidence per
    ``(namespace, value)``. Order of inputs does not matter."""
    by_key: dict[tuple[str, str], TagCandidate] = {}
    for group in groups:
        for cand in group:
            key = (cand.namespace, cand.value)
            existing = by_key.get(key)
            if existing is None or cand.confidence > existing.confidence:
                by_key[key] = cand
    return list(by_key.values())


# ---------------------------------------------------------------------------
# Optional LLM stage
# ---------------------------------------------------------------------------


_LLM_SYSTEM = (
    "You are a clinical indexing assistant. Given the text below, extract "
    "relevant tags for a medical imaging archive. Reply with a strict JSON "
    'array of objects of the form {"namespace": "<ns>", "value": "<slug>"}. '
    "Use only these namespaces: modality, anatomy, body, finding, pathology, "
    "technique. Emit lowercase slugs. No prose, no trailing text, no code fences."
)


async def extract_tags_via_llm(
    text: str,
    *,
    api_key: str,
    model_id: str,
    timeout: float = 20.0,
    http_client: httpx.AsyncClient | None = None,
) -> list[TagCandidate]:
    """Call the Anthropic Messages API with a tag-extraction prompt and
    parse the JSON array it returns. Silently returns ``[]`` on any
    failure — the lexicon stage is the ground truth; LLM is a bonus.

    Pass ``http_client`` to reuse a connection pool across invocations
    (the worker does this); otherwise a throwaway client is built.
    """
    if not text or not api_key:
        return []
    body = {
        "model": model_id,
        "max_tokens": 512,
        "system": _LLM_SYSTEM,
        "messages": [{"role": "user", "content": text[:8000]}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        if http_client is None:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    content=json.dumps(body),
                )
        else:
            resp = await http_client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                content=json.dumps(body),
                timeout=timeout,
            )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    raw = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()
    return _parse_llm_tag_json(raw)


def _parse_llm_tag_json(raw: str) -> list[TagCandidate]:
    """Parse the LLM's JSON array. Tolerates stray text around the array
    by scanning for the first ``[`` ... last ``]`` slice."""
    if not raw:
        return []
    lo = raw.find("[")
    hi = raw.rfind("]")
    if lo < 0 or hi <= lo:
        return []
    try:
        items = json.loads(raw[lo : hi + 1])
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    allowed_ns = {"modality", "anatomy", "body", "finding", "pathology", "technique"}
    out: list[TagCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ns = str(item.get("namespace", "")).strip().lower()
        val = str(item.get("value", "")).strip().lower()
        if ns not in allowed_ns or not val:
            continue
        # Confidence for LLM-emitted tags is conservative — lexicon rules
        # are explicitly audited, the LLM is not.
        out.append(TagCandidate(namespace=ns, value=val[:255], confidence=0.6))
    return out


def should_try_llm(text: str, enabled: bool) -> bool:
    """Gate the LLM stage on both the feature flag and a minimum input
    length. Exposed so the worker can log the decision without
    duplicating the heuristic."""
    return enabled and len(text) >= _LLM_MIN_CHARS
