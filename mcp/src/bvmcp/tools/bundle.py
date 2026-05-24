"""MCP tool that aggregates the full patient fascicolo into a single bundle.

The bundle is returned as two ``TextContent`` items:

1. A markdown rendering, ready to drop into an LLM prompt.
2. A JSON appendix with the full structured payloads for programmatic
   retrieval and follow-up tool calls.

The goal is to remove the need for the LLM to orchestrate five separate
MCP calls just to reason over a patient record — this tool calls the
backend REST endpoints concurrently, stitches the results together, and
applies a simple token budget so the rendered markdown never blows past
the model's context window.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
from mcp.types import TextContent, Tool

from bvmcp.tools.client import api_get

DEFAULT_SECTIONS = [
    "demographics",
    "studies",
    "reports",
    "documents",
    "annotations",
    "timeline",
]


TOOLS = [
    Tool(
        name="get_fascicolo_bundle",
        description=(
            "Aggregate a patient's entire fascicolo (radiology record) into a single "
            "response with two parts: a markdown summary ready for LLM prompting and "
            "a JSON appendix with the raw structured payloads. Fetches demographics, "
            "fascicolo index, timeline, documents, and inferred studies/reports/"
            "annotations in parallel so the caller does not need to orchestrate "
            "multiple tool calls. Honours a token budget by trimming older items."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "UUID of the patient",
                },
                "include": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": DEFAULT_SECTIONS,
                    },
                    "description": (
                        "Sections to include in the bundle. Defaults to all: "
                        "demographics, studies, reports, documents, annotations, timeline."
                    ),
                },
                "max_tokens": {
                    "type": "integer",
                    "description": (
                        "Approximate token budget for the markdown rendering. "
                        "Older items are trimmed first when the budget is exceeded. "
                        "Default 20000."
                    ),
                    "default": 20000,
                },
                "lang": {
                    "type": "string",
                    "description": "Language for section labels. 'it' or 'en'. Default 'it'.",
                    "default": "it",
                },
            },
            "required": ["patient_id"],
        },
    ),
]


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

_LABELS = {
    "it": {
        "header": "Fascicolo Paziente",
        "demographics": "Dati Anagrafici",
        "studies": "Studi Diagnostici",
        "reports": "Referti",
        "documents": "Documenti Clinici",
        "annotations": "Annotazioni",
        "timeline": "Timeline",
        "overview": "Panoramica Fascicolo",
        "older_omitted": "voci più vecchie omesse per vincoli di spazio",
        "none": "Nessun elemento.",
        "field_name": "Nome",
        "field_birth": "Data di nascita",
        "field_sex": "Sesso",
        "field_tax_id": "Codice fiscale",
        "field_phone": "Telefono",
        "field_email": "Email",
        "field_blood": "Gruppo sanguigno",
        "field_allergies": "Allergie",
        "field_notes": "Note cliniche",
        "field_address": "Indirizzo",
        "field_external": "ID esterno",
        "col_date": "Data",
        "col_type": "Tipo",
        "col_title": "Titolo",
        "col_description": "Descrizione",
        "col_modalities": "Modalità",
        "col_version": "Versione",
        "col_text": "Testo",
        "col_source": "Origine",
        "col_kind": "Tipo",
        "col_count": "N.",
        "col_last": "Ultimo",
        "col_breakdown": "Dettaglio",
        "col_section": "Sezione",
    },
    "en": {
        "header": "Patient Record",
        "demographics": "Demographics",
        "studies": "Studies",
        "reports": "Reports",
        "documents": "Documents",
        "annotations": "Annotations",
        "timeline": "Timeline",
        "overview": "Record Overview",
        "older_omitted": "older items omitted due to size limits",
        "none": "No items.",
        "field_name": "Name",
        "field_birth": "Birth date",
        "field_sex": "Sex",
        "field_tax_id": "Tax ID",
        "field_phone": "Phone",
        "field_email": "Email",
        "field_blood": "Blood type",
        "field_allergies": "Allergies",
        "field_notes": "Clinical notes",
        "field_address": "Address",
        "field_external": "External ID",
        "col_date": "Date",
        "col_type": "Type",
        "col_title": "Title",
        "col_description": "Description",
        "col_modalities": "Modalities",
        "col_version": "Version",
        "col_text": "Text",
        "col_source": "Source",
        "col_kind": "Kind",
        "col_count": "Count",
        "col_last": "Last",
        "col_breakdown": "Breakdown",
        "col_section": "Section",
    },
}


def _labels(lang: str) -> dict[str, str]:
    return _LABELS.get(lang, _LABELS["it"])


# --------------------------------------------------------------------------- #
# Token budget — rough chars/4 estimate is fine: we only need to avoid
# blowing the context window by orders of magnitude, not be exact.
# --------------------------------------------------------------------------- #


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Data fetching — tolerate per-endpoint failures so one missing piece does
# not kill the whole bundle (e.g. annotations returning a 4xx on a scope the
# caller cannot see still leaves demographics + studies useful).
# --------------------------------------------------------------------------- #


async def _safe_get(path: str, params: dict | None = None) -> Any:
    """GET returning None on HTTP error so partial bundles still render."""
    try:
        return await api_get(path, params=params)
    except httpx.HTTPError:
        return None


async def _fanout_by_study(
    study_ids: list[str],
    path_fn: Callable[[str], str],
    params_fn: Callable[[str], dict | None] = lambda _sid: None,
) -> dict[str, list[dict]]:
    """Fan out a per-study GET concurrently and collect list-valued results."""
    if not study_ids:
        return {}
    tasks = [_safe_get(path_fn(sid), params=params_fn(sid)) for sid in study_ids]
    results = await asyncio.gather(*tasks)
    return {sid: res for sid, res in zip(study_ids, results, strict=False) if isinstance(res, list)}


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #


def _render_demographics(patient: dict | None, lbl: dict[str, str]) -> str:
    if not patient:
        return f"## {lbl['demographics']}\n\n{lbl['none']}\n"
    rows: list[tuple[str, Any]] = [
        (lbl["field_name"], patient.get("display_name")),
        (lbl["field_birth"], patient.get("birth_date")),
        (lbl["field_sex"], patient.get("sex")),
        (lbl["field_tax_id"], patient.get("tax_id")),
        (lbl["field_phone"], patient.get("phone")),
        (lbl["field_email"], patient.get("email")),
        (lbl["field_address"], patient.get("address")),
        (lbl["field_blood"], patient.get("blood_type")),
        (lbl["field_allergies"], patient.get("allergies")),
        (lbl["field_external"], patient.get("external_id")),
        (lbl["field_notes"], patient.get("notes")),
    ]
    lines = [f"## {lbl['demographics']}", ""]
    for label, value in rows:
        if value:
            lines.append(f"- **{label}**: {value}")
    lines.append("")
    return "\n".join(lines)


def _render_overview(index: dict | None, lbl: dict[str, str]) -> str:
    if not index or not index.get("sections"):
        return ""
    lines = [f"## {lbl['overview']}", ""]
    lines.append(
        f"| {lbl['col_section']} | {lbl['col_count']} | {lbl['col_last']} | {lbl['col_breakdown']} |"
    )
    lines.append("|---|---|---|---|")
    for sec in index.get("sections", []):
        breakdown = sec.get("breakdown") or {}
        bd_txt = ", ".join(f"{k}:{v}" for k, v in breakdown.items()) if breakdown else "—"
        lines.append(
            f"| {sec.get('label', sec.get('key'))} "
            f"| {sec.get('count', 0)} "
            f"| {sec.get('last_date') or '—'} "
            f"| {bd_txt} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_studies(
    studies: list[dict], lbl: dict[str, str], budget_tokens: int
) -> tuple[str, int]:
    """Return (markdown, tokens_spent). Trims older studies beyond budget."""
    header = f"## {lbl['studies']} ({len(studies)})\n\n"
    if not studies:
        body = f"{lbl['none']}\n"
        return header + body, _estimate_tokens(header + body)

    table_head = (
        f"| {lbl['col_date']} | {lbl['col_description']} | {lbl['col_modalities']} | ID |\n"
        "|---|---|---|---|\n"
    )
    rendered: list[str] = []
    spent = _estimate_tokens(header + table_head)
    omitted = 0
    for s in studies:
        mods = ", ".join(s.get("modalities") or []) or "—"
        row = (
            f"| {s.get('study_date') or '—'} "
            f"| {(s.get('study_description') or '—')[:120]} "
            f"| {mods} "
            f"| `{s.get('id', '')}` |\n"
        )
        row_cost = _estimate_tokens(row)
        if spent + row_cost > budget_tokens:
            omitted = len(studies) - len(rendered)
            break
        rendered.append(row)
        spent += row_cost

    footer = ""
    if omitted:
        footer = f"\n> _{omitted} {lbl['older_omitted']}_\n"
        spent += _estimate_tokens(footer)
    md = header + table_head + "".join(rendered) + footer + "\n"
    return md, spent


def _render_reports(
    reports_by_study: dict[str, list[dict]],
    studies: list[dict],
    lbl: dict[str, str],
    budget_tokens: int,
) -> tuple[str, int]:
    all_reports: list[tuple[dict, dict]] = []  # (study, report)
    study_by_id = {s.get("id"): s for s in studies}
    for sid, reports in reports_by_study.items():
        study = study_by_id.get(sid, {"id": sid})
        for r in reports:
            all_reports.append((study, r))
    all_reports.sort(key=lambda sr: sr[1].get("created_at") or "", reverse=True)

    header = f"## {lbl['reports']} ({len(all_reports)})\n\n"
    if not all_reports:
        body = f"{lbl['none']}\n"
        return header + body, _estimate_tokens(header + body)

    spent = _estimate_tokens(header)
    rendered: list[str] = []
    omitted = 0
    for study, report in all_reports:
        text = (report.get("text") or "").strip()
        # Clip each report body to stay predictable; caller has full JSON.
        snippet = text[:800] + ("…" if len(text) > 800 else "")
        block = (
            f"### {study.get('study_description') or study.get('id')} "
            f"— v{report.get('version', '?')} "
            f"({report.get('created_at', '—')[:10]})\n\n"
            f"{snippet or '_(empty)_'}\n\n"
        )
        cost = _estimate_tokens(block)
        if spent + cost > budget_tokens:
            omitted = len(all_reports) - len(rendered)
            break
        rendered.append(block)
        spent += cost

    footer = ""
    if omitted:
        footer = f"> _{omitted} {lbl['older_omitted']}_\n"
        spent += _estimate_tokens(footer)
    return header + "".join(rendered) + footer, spent


def _render_documents(
    documents: list[dict], lbl: dict[str, str], budget_tokens: int
) -> tuple[str, int]:
    header = f"## {lbl['documents']} ({len(documents)})\n\n"
    if not documents:
        body = f"{lbl['none']}\n"
        return header + body, _estimate_tokens(header + body)

    table_head = (
        f"| {lbl['col_date']} | {lbl['col_type']} | {lbl['col_title']} | {lbl['col_text']} |\n"
        "|---|---|---|---|\n"
    )
    rendered: list[str] = []
    spent = _estimate_tokens(header + table_head)
    omitted = 0
    for d in documents:
        text = (d.get("text") or "").strip().replace("\n", " ")
        snippet = text[:160] + ("…" if len(text) > 160 else "")
        row = (
            f"| {d.get('document_date') or d.get('created_at', '—')[:10]} "
            f"| {d.get('document_type', '—')} "
            f"| {(d.get('title') or '—')[:80]} "
            f"| {snippet or '—'} |\n"
        )
        cost = _estimate_tokens(row)
        if spent + cost > budget_tokens:
            omitted = len(documents) - len(rendered)
            break
        rendered.append(row)
        spent += cost

    footer = ""
    if omitted:
        footer = f"\n> _{omitted} {lbl['older_omitted']}_\n"
        spent += _estimate_tokens(footer)
    return header + table_head + "".join(rendered) + footer + "\n", spent


def _render_annotations(
    annotations_by_study: dict[str, list[dict]],
    lbl: dict[str, str],
    budget_tokens: int,
) -> tuple[str, int]:
    flat: list[dict] = [a for anns in annotations_by_study.values() for a in anns]
    flat.sort(key=lambda a: a.get("created_at") or "", reverse=True)

    header = f"## {lbl['annotations']} ({len(flat)})\n\n"
    if not flat:
        body = f"{lbl['none']}\n"
        return header + body, _estimate_tokens(header + body)

    table_head = (
        f"| {lbl['col_date']} | {lbl['col_source']} | {lbl['col_kind']} | ID |\n|---|---|---|---|\n"
    )
    rendered: list[str] = []
    spent = _estimate_tokens(header + table_head)
    omitted = 0
    for a in flat:
        row = (
            f"| {(a.get('created_at') or '—')[:10]} "
            f"| {a.get('source', '—')} "
            f"| {a.get('kind', '—')} "
            f"| `{a.get('id', '')}` |\n"
        )
        cost = _estimate_tokens(row)
        if spent + cost > budget_tokens:
            omitted = len(flat) - len(rendered)
            break
        rendered.append(row)
        spent += cost
    footer = ""
    if omitted:
        footer = f"\n> _{omitted} {lbl['older_omitted']}_\n"
        spent += _estimate_tokens(footer)
    return header + table_head + "".join(rendered) + footer + "\n", spent


def _render_timeline(
    timeline: list[dict], lbl: dict[str, str], budget_tokens: int
) -> tuple[str, int]:
    header = f"## {lbl['timeline']} ({len(timeline)})\n\n"
    if not timeline:
        body = f"{lbl['none']}\n"
        return header + body, _estimate_tokens(header + body)

    table_head = (
        f"| {lbl['col_date']} | {lbl['col_type']} | {lbl['col_description']} |\n|---|---|---|\n"
    )
    rendered: list[str] = []
    spent = _estimate_tokens(header + table_head)
    omitted = 0
    for item in timeline:
        data = item.get("data") or {}
        desc = (
            data.get("study_description")
            or data.get("title")
            or data.get("text")
            or data.get("kind")
            or "—"
        )
        desc = str(desc).replace("\n", " ")[:100]
        row = f"| {(item.get('date') or '—')[:10]} | {item.get('type', '—')} | {desc} |\n"
        cost = _estimate_tokens(row)
        if spent + cost > budget_tokens:
            omitted = len(timeline) - len(rendered)
            break
        rendered.append(row)
        spent += cost

    footer = ""
    if omitted:
        footer = f"\n> _{omitted} {lbl['older_omitted']}_\n"
        spent += _estimate_tokens(footer)
    return header + table_head + "".join(rendered) + footer + "\n", spent


# --------------------------------------------------------------------------- #
# Top-level bundler
# --------------------------------------------------------------------------- #


def _studies_from_timeline(timeline: list[dict]) -> list[dict]:
    """Extract study dicts from timeline items of type 'study'."""
    out: list[dict] = []
    for item in timeline or []:
        if item.get("type") == "study":
            d = item.get("data") or {}
            out.append(
                {
                    "id": d.get("id"),
                    "study_description": d.get("study_description"),
                    "modalities": d.get("modalities") or [],
                    "study_date": d.get("study_date"),
                }
            )
    return out


async def build_bundle(
    patient_id: str,
    include: list[str] | None = None,
    max_tokens: int = 20000,
    lang: str = "it",
) -> list[TextContent]:
    sections = set(include or DEFAULT_SECTIONS)
    lbl = _labels(lang)

    demographics, index, timeline, documents = await asyncio.gather(
        _safe_get(f"/api/patients/{patient_id}"),
        _safe_get(f"/api/patients/{patient_id}/index"),
        _safe_get(f"/api/patients/{patient_id}/timeline", params={"limit": 50}),
        _safe_get(f"/api/patients/{patient_id}/documents"),
    )
    timeline = timeline or []
    documents = documents or []

    studies = _studies_from_timeline(timeline)
    study_ids = [s["id"] for s in studies if s.get("id")]

    reports_by_study: dict[str, list[dict]] = {}
    annotations_by_study: dict[str, list[dict]] = {}
    fanout_tasks = []
    if "reports" in sections:
        fanout_tasks.append(
            ("reports", _fanout_by_study(study_ids, lambda sid: f"/api/studies/{sid}/reports"))
        )
    if "annotations" in sections:
        fanout_tasks.append(
            (
                "annotations",
                _fanout_by_study(
                    study_ids,
                    lambda _sid: "/api/annotations",
                    lambda sid: {"target_kind": "study", "target_id": sid},
                ),
            )
        )
    if fanout_tasks:
        results = await asyncio.gather(*(t for _, t in fanout_tasks))
        for (kind, _), res in zip(fanout_tasks, results, strict=True):
            if kind == "reports":
                reports_by_study = res
            else:
                annotations_by_study = res

    h1 = f"# {lbl['header']}\n\n"
    overview_md = _render_overview(index, lbl) if index else ""

    # Split the remaining budget evenly across sections that have content.
    dynamic_sections = [
        s for s in ("studies", "reports", "documents", "annotations", "timeline") if s in sections
    ]
    budget_remaining = max(500, max_tokens - _estimate_tokens(h1) - _estimate_tokens(overview_md))
    per_section = (
        budget_remaining // len(dynamic_sections) if dynamic_sections else budget_remaining
    )

    parts: list[str] = [h1]
    if "demographics" in sections:
        parts.append(_render_demographics(demographics, lbl))
    if overview_md:
        parts.append(overview_md)
    if "studies" in sections:
        md, _ = _render_studies(studies, lbl, per_section)
        parts.append(md)
    if "reports" in sections:
        md, _ = _render_reports(reports_by_study, studies, lbl, per_section)
        parts.append(md)
    if "documents" in sections:
        md, _ = _render_documents(documents, lbl, per_section)
        parts.append(md)
    if "annotations" in sections:
        md, _ = _render_annotations(annotations_by_study, lbl, per_section)
        parts.append(md)
    if "timeline" in sections:
        md, _ = _render_timeline(timeline, lbl, per_section)
        parts.append(md)

    markdown = "\n".join(p for p in parts if p)

    # JSON appendix keeps the original shapes so tools downstream can
    # pick fields without re-parsing markdown.
    appendix = {
        "patient_id": patient_id,
        "demographics": demographics,
        "index": index,
        "studies": studies,
        "reports_by_study": reports_by_study,
        "documents": documents,
        "annotations_by_study": annotations_by_study,
        "timeline": timeline,
        "included_sections": sorted(sections),
        "language": lang,
    }
    appendix_json = json.dumps(appendix, indent=2, ensure_ascii=False, default=str)

    return [
        TextContent(type="text", text=markdown),
        TextContent(type="text", text=appendix_json),
    ]


async def handle(name: str, arguments: dict) -> list[TextContent]:
    if name == "get_fascicolo_bundle":
        return await build_bundle(
            patient_id=arguments["patient_id"],
            include=arguments.get("include"),
            max_tokens=int(arguments.get("max_tokens", 20000)),
            lang=arguments.get("lang", "it"),
        )
    raise ValueError(f"unknown tool: {name}")
