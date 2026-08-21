"""Auto-tag a study / series / instance as a background job.

The task loads the target's free-text descriptors directly from the
database (study/series descriptions, latest report text, annotation
payloads), runs a deterministic medical-lexicon regex pass, then
optionally augments the result with an LLM call when
``settings.autotag_use_llm`` is on and the text is long enough to be
worth the round-trip.

Tags are persisted with ``source='auto'`` and a per-rule confidence;
the ``INSERT ... ON CONFLICT DO NOTHING`` idiom means a re-run is a
no-op and a manual (``source='manual'``) tag for the same
``(target_kind, target_id, namespace, value)`` is never overwritten.

Rule table parity
-----------------
The lexicon below is intentionally a verbatim copy of the rules in
``backend/src/bvphoenix/services/autotag.py`` so workers can run
without a runtime dependency on the backend package (same pattern as
``prefetch_series._downsample_packed``). If you edit one list, mirror
the change in the other — CI does not yet cross-check them.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

import httpx
from bvphoenix.db.engine import make_async_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings


@dataclass(frozen=True, slots=True)
class TagCandidate:
    namespace: str
    value: str
    confidence: float


# Keep in sync with backend/src/bvphoenix/services/autotag.py::_RAW_RULES.
_RAW_RULES: list[tuple[str, str, str, float]] = [
    # --- modality ---
    (r"\b(ct|tc|computed[-\s]tomography|tomografia[-\s]computer\w*)\b", "modality", "CT", 0.95),
    (r"\b(mr|rm|mri|risonanza[-\s]magnetica|magnetic[-\s]resonance)\b", "modality", "MR", 0.95),
    (r"\b(xr|rx|x[-\s]?ray|radiograph\w*|radiografia)\b", "modality", "XR", 0.9),
    (r"\b(us|ultrasound|ecografia|ecograf\w+)\b", "modality", "US", 0.9),
    (r"\b(pet|pt[-\s]?scan|positron[-\s]emission)\b", "modality", "PT", 0.9),
    (r"\b(spect|scintigraf\w+|nuclear[-\s]medicine)\b", "modality", "NM", 0.85),
    (r"\b(mammograf\w+|mammography|mg[-\s]scan)\b", "modality", "MG", 0.9),
    # --- anatomy ---
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
    # --- body region ---
    (r"\b(thorax|chest|torace|toracico|toracica)\b", "body", "thorax", 0.85),
    (r"\b(abdomen|abdominal|addome|addominale)\b", "body", "abdomen", 0.85),
    (r"\b(head|cranio|cranium|skull|testa)\b", "body", "head", 0.85),
    (r"\b(spine|rachide|colonna[-\s]vertebrale)\b", "body", "spine", 0.85),
    (r"\b(pelvis|pelvic|pelvi|pelvico|pelvica|bacino)\b", "body", "pelvis", 0.85),
    (r"\b(extremity|estremità|arto|arti|upper[-\s]limb|lower[-\s]limb)\b", "body", "extremity", 0.8),
    (r"\b(neck|collo|cervical|cervicale)\b", "body", "neck", 0.85),
    # --- findings ---
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
    # --- pathology ---
    (r"\b(pneumonia|polmonite)\b", "pathology", "pneumonia", 0.9),
    (r"\b(cancer|cancro|tumor|tumore|malign\w+|carcinoma|carcinoma\w+)\b", "pathology", "cancer", 0.8),
    (r"\b(stroke|ictus|cerebrovascular[-\s]accident|cva)\b", "pathology", "stroke", 0.9),
    (r"\b(embolism|embolia|pulmonary[-\s]embolism|pe)\b", "pathology", "embolism", 0.85),
    (r"\b(aneurysm|aneurisma)\b", "pathology", "aneurysm", 0.9),
    (r"\b(covid|covid-19|sars-cov-2)\b", "pathology", "covid", 0.95),
    (r"\b(tubercul\w+|tbc|tb)\b", "pathology", "tuberculosis", 0.9),
    (r"\b(pneumothorax|pnx)\b", "pathology", "pneumothorax", 0.9),
    (r"\b(metastas\w+|mts)\b", "pathology", "metastasis", 0.85),
    (r"\b(osteoporos\w+)\b", "pathology", "osteoporosis", 0.9),
    # --- technique ---
    (r"\b(contrast|contrasto|mdc|mezzo[-\s]di[-\s]contrasto)\b", "technique", "contrast", 0.9),
    (r"\b(angio|angiograf\w+|ct[-\s]?angio)\b", "technique", "angio", 0.9),
    (r"\b(diffusion|dwi|diffusione)\b", "technique", "diffusion", 0.9),
    (r"\b(perfusion|perfusione)\b", "technique", "perfusion", 0.9),
    (r"\b(t1[-\s]weighted|t1w|t1)\b", "technique", "t1", 0.7),
    (r"\b(t2[-\s]weighted|t2w|t2)\b", "technique", "t2", 0.7),
    (r"\b(flair)\b", "technique", "flair", 0.9),
]

_RULES: list[tuple[re.Pattern[str], str, str, float]] = [
    (re.compile(pat, re.IGNORECASE), ns, val, conf) for (pat, ns, val, conf) in _RAW_RULES
]

_LLM_MIN_CHARS = 200
_LLM_SYSTEM = (
    "You are a clinical indexing assistant. Given the text below, extract "
    "relevant tags for a medical imaging archive. Reply with a strict JSON "
    "array of objects of the form {\"namespace\": \"<ns>\", \"value\": \"<slug>\"}. "
    "Use only these namespaces: modality, anatomy, body, finding, pathology, "
    "technique. Emit lowercase slugs. No prose, no trailing text, no code fences."
)


def _extract_lexicon(src: str) -> list[TagCandidate]:
    if not src:
        return []
    found: dict[tuple[str, str], TagCandidate] = {}
    for pattern, ns, val, conf in _RULES:
        if pattern.search(src):
            key = (ns, val)
            existing = found.get(key)
            if existing is None or conf > existing.confidence:
                found[key] = TagCandidate(ns, val, conf)
    return list(found.values())


def _parse_llm_tag_json(raw: str) -> list[TagCandidate]:
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
    allowed = {"modality", "anatomy", "body", "finding", "pathology", "technique"}
    out: list[TagCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ns = str(item.get("namespace", "")).strip().lower()
        val = str(item.get("value", "")).strip().lower()
        if ns in allowed and val:
            out.append(TagCandidate(ns, val[:255], 0.6))
    return out


async def _extract_via_llm(src: str, *, api_key: str, model_id: str) -> list[TagCandidate]:
    if not src or not api_key:
        return []
    body = {
        "model": model_id,
        "max_tokens": 512,
        "system": _LLM_SYSTEM,
        "messages": [{"role": "user", "content": src[:8000]}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                content=json.dumps(body),
            )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    raw = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()
    return _parse_llm_tag_json(raw)


async def _load_target_text(db: AsyncSession, target_kind: str, target_id: uuid.UUID) -> str:
    """Concatenate every free-text descriptor related to the target.

    Covers the obvious bases: study / series description, report body,
    annotation payload strings. Order irrelevant — the rule engine is
    idempotent on duplicates.
    """
    parts: list[str] = []
    if target_kind == "study":
        row = (
            await db.execute(
                text("SELECT study_description FROM studies WHERE id = :tid"),
                {"tid": target_id},
            )
        ).first()
        if row and row[0]:
            parts.append(row[0])
        # Descriptions on the child series round out the picture.
        series_rows = await db.execute(
            text(
                "SELECT series_description, body_part_examined, modality "
                "FROM series WHERE study_id = :tid"
            ),
            {"tid": target_id},
        )
        for desc, body, modality in series_rows.all():
            if desc:
                parts.append(desc)
            if body:
                parts.append(body)
            if modality:
                parts.append(modality)
        # Latest report.
        report = (
            await db.execute(
                text(
                    "SELECT text FROM reports WHERE study_id = :tid "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"tid": target_id},
            )
        ).first()
        if report and report[0]:
            parts.append(report[0])
    elif target_kind == "series":
        row = (
            await db.execute(
                text(
                    "SELECT series_description, body_part_examined, modality, study_id "
                    "FROM series WHERE id = :tid"
                ),
                {"tid": target_id},
            )
        ).first()
        if row:
            desc, body, modality, study_id = row
            if desc:
                parts.append(desc)
            if body:
                parts.append(body)
            if modality:
                parts.append(modality)
            # ImagingStudy-level description helps disambiguate.
            sdesc = (
                await db.execute(
                    text("SELECT study_description FROM studies WHERE id = :sid"),
                    {"sid": study_id},
                )
            ).first()
            if sdesc and sdesc[0]:
                parts.append(sdesc[0])
    elif target_kind == "instance":
        # Instances don't carry their own text — inherit from the series.
        row = (
            await db.execute(
                text(
                    "SELECT s.series_description, s.body_part_examined, s.modality "
                    "FROM series s JOIN instances i ON i.series_id = s.id "
                    "WHERE i.id = :tid"
                ),
                {"tid": target_id},
            )
        ).first()
        if row:
            for v in row:
                if v:
                    parts.append(v)

    return "\n".join(parts)


async def _persist_tags(
    db: AsyncSession,
    target_kind: str,
    target_id: uuid.UUID,
    candidates: list[TagCandidate],
) -> int:
    """Insert auto tags, skipping any conflicts. Returns inserted count.

    The unique constraint ``(target_kind, target_id, namespace, value)``
    is what makes ``ON CONFLICT DO NOTHING`` safe — a manual tag with
    the same key pre-empts the automated one (first writer wins).
    """
    if not candidates:
        return 0
    inserted = 0
    for cand in candidates:
        res = await db.execute(
            text(
                "INSERT INTO tags (target_kind, target_id, namespace, value, source, confidence) "
                "VALUES (:tk, :tid, :ns, :val, 'auto', :conf) "
                "ON CONFLICT (target_kind, target_id, namespace, value) DO NOTHING "
                "RETURNING id"
            ),
            {
                "tk": target_kind,
                "tid": target_id,
                "ns": cand.namespace,
                "val": cand.value,
                "conf": cand.confidence,
            },
        )
        if res.first():
            inserted += 1
    return inserted


async def autotag_target(ctx: dict, target_kind: str, target_id: str) -> dict:  # type: ignore[type-arg]
    """Arq task: load a target's text, extract tags, persist the new ones.

    Idempotent on reruns and on collisions with manual tags. Returns a
    small status dict for visibility in arq's job log.
    """
    if target_kind not in ("study", "series", "instance"):
        return {"status": "invalid_target_kind", "target_kind": target_kind}

    settings = get_settings()
    engine = make_async_engine(settings.database_url, pool_pre_ping=True)
    tid = uuid.UUID(target_id)

    async with AsyncSession(engine) as db:
        # Workers run as the service principal so RLS doesn't trim our view.
        await db.execute(text("SELECT set_config('app.current_subject_id', 'service', true)"))
        source_text = await _load_target_text(db, target_kind, tid)
        if not source_text.strip():
            await engine.dispose()
            return {
                "status": "no_text",
                "target_kind": target_kind,
                "target_id": target_id,
            }

        lexicon_tags = _extract_lexicon(source_text)

        llm_tags: list[TagCandidate] = []
        if settings.autotag_use_llm and len(source_text) >= _LLM_MIN_CHARS:
            llm_tags = await _extract_via_llm(
                source_text,
                api_key=settings.anthropic_api_key,
                model_id=settings.llm_default_model,
            )

        # Merge keeping the higher-confidence entry per (ns, value).
        merged: dict[tuple[str, str], TagCandidate] = {}
        for c in (*lexicon_tags, *llm_tags):
            k = (c.namespace, c.value)
            existing = merged.get(k)
            if existing is None or c.confidence > existing.confidence:
                merged[k] = c
        candidates = list(merged.values())

        inserted = await _persist_tags(db, target_kind, tid, candidates)
        await db.commit()

    await engine.dispose()
    return {
        "status": "ok",
        "target_kind": target_kind,
        "target_id": target_id,
        "candidates": len(candidates),
        "inserted": inserted,
        "llm_used": settings.autotag_use_llm and len(source_text) >= _LLM_MIN_CHARS,
    }
