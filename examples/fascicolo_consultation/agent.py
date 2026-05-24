"""End-to-end multimodal fascicolo consultation agent for bitvision phoenix.

Pulls a patient bundle (profile + index + timeline + studies) from the REST
API, fetches a JPEG thumbnail per series, composes an Anthropic multimodal
prompt, invokes Claude with a radiology-consultant system prompt, parses
the JSON response, and POSTs /api/consultations. Simulates the planned
M1 (get_series_thumbnail) and M3 (get_fascicolo_bundle) MCP tools
client-side. See README.md for usage.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from typing import Any

import httpx
from anthropic import Anthropic

DEFAULT_MODEL = "claude-opus-4-7-1m"
MAX_TOKENS = 4096
SYSTEM_PROMPT = (
    "Sei un consulente radiologo AI che analizza il fascicolo sanitario di un paziente. "
    "Ti vengono forniti: profilo paziente, indice fascicolo, timeline cronologica, "
    "studi DICOM con thumbnail delle serie, referti e documenti clinici. "
    "Produci una consulenza strutturata in JSON con i campi: "
    '{"title": "...", "findings": "...", "recommendations": "...", '
    '"citations": [{"kind": "study"|"report"|"document", "id": "uuid", "note": "..."}]}. '
    "Findings e recommendations sono testo Markdown conciso. "
    "Citazioni: referenzia solo gli ID effettivamente presenti nel bundle. "
    "Rispondi esclusivamente con il JSON, senza testo introduttivo."
)


def _env_or_arg(args: argparse.Namespace, attr: str, env: str, required: bool = True) -> str:
    value = getattr(args, attr, None) or os.environ.get(env, "")
    if not value and required:
        print(f"error: missing {env} (or --{attr.replace('_', '-')})", file=sys.stderr)
        sys.exit(2)
    return value


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default=None, help="bitvision backend base URL")
    p.add_argument("--agent-token", default=None, help="bitvision agent JWT")
    p.add_argument("--anthropic-api-key", default=None, help="Anthropic API key")
    p.add_argument("--patient-id", default=None, help="Patient UUID")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default: {DEFAULT_MODEL})")
    p.add_argument("--max-thumbs-per-study", type=int, default=3,
                   help="Cap on series thumbnails per study to keep prompt size bounded")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip the final POST /api/consultations")
    return p.parse_args()


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _get_json(client: httpx.Client, url: str, token: str, **params: Any) -> Any:
    resp = client.get(url, headers=_auth_headers(token), params=params or None)
    if resp.status_code == 401:
        raise SystemExit("error: 401 — agent token expired or invalid")
    resp.raise_for_status()
    return resp.json()


def fetch_bundle(
    client: httpx.Client, base: str, token: str, patient_id: str
) -> dict[str, Any]:
    """Simulate the planned M3 `get_fascicolo_bundle` MCP tool client-side."""
    print("[1/6] Fetching patient profile…")
    patient = _get_json(client, f"{base}/api/patients/{patient_id}", token)
    print("[2/6] Fetching fascicolo index + timeline…")
    index = _get_json(client, f"{base}/api/patients/{patient_id}/index", token)
    timeline = _get_json(client, f"{base}/api/patients/{patient_id}/timeline", token)
    studies = _get_json(client, f"{base}/api/studies", token, patient_id=patient_id)
    if isinstance(studies, dict) and "items" in studies:
        studies = studies["items"]
    print(f"[3/6] Fetched {len(studies)} studies.")
    return {"patient": patient, "index": index, "timeline": timeline, "studies": studies}


def fetch_thumbnail(
    client: httpx.Client, base: str, token: str, series_id: str
) -> tuple[str, str] | None:
    """Return (media_type, base64) or None if the series has no pixel data."""
    resp = client.get(
        f"{base}/api/series/{series_id}/thumbnail",
        headers=_auth_headers(token),
        params={"wc_delta": 0, "ww_delta": 0},
    )
    # 422 = no pixel data (structured report, key object); 404 = gone or de-authed
    if resp.status_code in (404, 422):
        return None
    resp.raise_for_status()
    media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    return media_type, base64.standard_b64encode(resp.content).decode("ascii")


def build_prompt_blocks(
    client: httpx.Client, base: str, token: str, bundle: dict[str, Any], max_thumbs: int
) -> list[dict[str, Any]]:
    """Interleave text (bundle JSON) and image blocks for Anthropic Messages."""
    blocks: list[dict[str, Any]] = []
    summary = {
        "patient": bundle["patient"],
        "index": bundle["index"],
        "timeline_head": bundle["timeline"][:20],  # bound prompt size
        "study_count": len(bundle["studies"]),
    }
    blocks.append({
        "type": "text",
        "text": "## Fascicolo paziente (metadati)\n```json\n"
                + json.dumps(summary, ensure_ascii=False, default=str, indent=2)
                + "\n```",
    })

    print("[4/6] Fetching thumbnails…")
    for study in bundle["studies"]:
        series_list = study.get("series") or []
        study_id = study.get("id", "?")
        modality = study.get("modality") or study.get("modalities", "?")
        blocks.append({
            "type": "text",
            "text": f"### Study `{study_id}` ({modality}) — {len(series_list)} series",
        })
        for s in series_list[:max_thumbs]:
            sid = s.get("id")
            if not sid:
                continue
            thumb = fetch_thumbnail(client, base, token, sid)
            if thumb is None:
                print(f"       · series {sid[:8]}… skipped (no pixel data)")
                continue
            media_type, b64 = thumb
            print(f"       ✓ series {sid[:8]}… ({len(b64) * 3 // 4 // 1024} KB)")
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
            blocks.append({
                "type": "text",
                "text": f"Serie `{sid}` — descrizione: {s.get('description', '—')}",
            })
    return blocks


def invoke_claude(
    api_key: str, model: str, blocks: list[dict[str, Any]]
) -> str:
    print(f"[5/6] Invoking Claude ({model}, {len(blocks)} content blocks)…")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": blocks}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def parse_response(raw: str, patient_id: str) -> dict[str, Any]:
    """Best-effort JSON extraction — tolerates ```json fences or prose wrapping."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidate = fenced.group(1) if fenced else raw.strip()
    try:
        parsed = json.loads(candidate)
        if not isinstance(parsed, dict):
            raise ValueError("not a JSON object")
    except (json.JSONDecodeError, ValueError):
        parsed = {
            "title": "Consulto fascicolo (raw)",
            "findings": raw,
            "recommendations": "",
            "citations": [],
        }
    parsed.setdefault("title", "Consulto fascicolo")
    parsed.setdefault("findings", "")
    parsed.setdefault("recommendations", "")
    parsed.setdefault("citations", [])
    parsed["patient_id"] = patient_id
    return parsed


def post_consultation(
    client: httpx.Client, base: str, token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    print("[6/6] POST /api/consultations…")
    resp = client.post(
        f"{base}/api/consultations",
        headers={**_auth_headers(token), "Content-Type": "application/json"},
        json=payload,
    )
    if resp.status_code == 404:
        raise SystemExit(
            "error: POST /api/consultations returned 404 — endpoint not yet landed. "
            "Re-run with --dry-run to preview the payload."
        )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    args = _parse_args()
    base = _env_or_arg(args, "base_url", "BVP_BASE_URL").rstrip("/")
    token = _env_or_arg(args, "agent_token", "BVP_AGENT_TOKEN")
    api_key = _env_or_arg(args, "anthropic_api_key", "ANTHROPIC_API_KEY")
    patient_id = _env_or_arg(args, "patient_id", "PATIENT_ID")

    with httpx.Client(timeout=60.0) as http:
        bundle = fetch_bundle(http, base, token, patient_id)
        blocks = build_prompt_blocks(http, base, token, bundle, args.max_thumbs_per_study)
        raw = invoke_claude(api_key, args.model, blocks)
        payload = parse_response(raw, patient_id)

        if args.dry_run:
            print("\n--- DRY RUN: would POST /api/consultations with ---")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        created = post_consultation(http, base, token, payload)
        consult_id = created.get("id", "?")
        print(f"\nConsultation created: {base}/consultations/{consult_id}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPError as e:
        print(f"network error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        sys.exit(2)
