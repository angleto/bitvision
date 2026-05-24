"""Offline reclassifier: scan ``imaging_report`` documents and propose
which ones should move to the new finer-grained kinds (Sprint 3,
ROADMAP "imaging_report → radiology_report"). The script is read-only
— it emits a JSON manifest the operator can feed into the Sprint 2
``bulk_update_documents`` endpoint after review.

Usage::

    uv run python scripts/propose_radiology_reclassification.py \
        --out /tmp/manifest.json [--patient-id <uuid>] [--limit 1000]

The heuristic is intentionally simple: title / inline-text keyword
match against per-kind cue lists. The output also carries a
``confidence`` field so the human can sort by uncertainty when
reviewing.

Read-only: the script never writes to the DB. The output manifest is
the unit of work the agent / human reviews; the apply step uses the
existing bulk-update endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Cue dictionaries — italian-leaning, since the corpus is italian.
# Lowercased substrings; whitespace tolerance handled at runtime.
_KIND_CUES: dict[str, tuple[str, ...]] = {
    "radiology_report": (
        "tc",
        "tac",
        "rmn",
        "rm encefalo",
        "ecografia",
        "rx torace",
        "ecodoppler",
        "scintigrafia",
        "pet",
        "mammografia",
        "radiografia",
        "referto rx",
        "referto tc",
        "referto rm",
        "referto ecografia",
        "doppler",
    ),
    "pathology_report": (
        "istologico",
        "anatomia patologica",
        "esame istologico",
        "biopsia",
        "esame citologico",
        "ago aspirato",
    ),
    "surgical_report": (
        "intervento chirurgico",
        "verbale operatorio",
        "operatoria",
        "post-operatorio",
    ),
    "cardio_report": (
        "ecocardiogramma",
        "ecg",
        "elettrocardiogramma",
        "holter",
        "stress test",
        "test da sforzo",
    ),
    "endoscopy_report": (
        "endoscopia",
        "egds",
        "colonscopia",
        "broncoscopia",
        "gastroscopia",
        "rettoscopia",
    ),
}


@dataclass(slots=True)
class Proposal:
    document_id: str
    patient_id: str
    current_type: str
    proposed_type: str
    confidence: float
    matched_cues: list[str]
    title: str | None


def _score(text: str, cues: tuple[str, ...]) -> tuple[float, list[str]]:
    """Return ``(confidence, matched_cues)``.

    Each cue hit contributes 1/len(cues); the score caps at 1. We pick
    the kind with the highest score; ties prefer the more specific
    kind (alphabetical order is fine because the cue dictionaries are
    disjoint enough in practice).
    """
    matches: list[str] = []
    haystack = text.lower()
    for cue in cues:
        # Word-boundary-ish match so "ecg" doesn't trigger on
        # "richiesta visita".
        if re.search(rf"\b{re.escape(cue)}\b", haystack):
            matches.append(cue)
    if not matches:
        return 0.0, []
    return min(1.0, len(matches) / max(1, len(cues))), matches


def _classify(text: str) -> tuple[str | None, float, list[str]]:
    best_kind: str | None = None
    best_score = 0.0
    best_matches: list[str] = []
    for kind, cues in _KIND_CUES.items():
        score, matches = _score(text, cues)
        if score > best_score:
            best_kind = kind
            best_score = score
            best_matches = matches
    return best_kind, best_score, best_matches


async def _walk(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID | None,
    limit: int,
) -> list[Proposal]:
    from bvphoenix.db.models import PatientDocument

    q = select(PatientDocument).where(
        PatientDocument.deleted_at.is_(None),
        PatientDocument.document_type.in_(("imaging_report", "specialist_report")),
    )
    if patient_id is not None:
        q = q.where(PatientDocument.patient_id == patient_id)
    rows = (await db.execute(q.limit(limit))).scalars().all()
    proposals: list[Proposal] = []
    for d in rows:
        haystack = "\n".join([d.title or "", d.text or ""])
        kind, score, matches = _classify(haystack)
        if kind is None or kind == d.document_type:
            continue
        proposals.append(
            Proposal(
                document_id=str(d.id),
                patient_id=str(d.patient_id),
                current_type=d.document_type,
                proposed_type=kind,
                confidence=round(score, 3),
                matched_cues=matches,
                title=d.title,
            )
        )
    proposals.sort(key=lambda p: (-p.confidence, p.document_id))
    return proposals


def _emit(proposals: list[Proposal]) -> dict[str, Any]:
    """Format the output manifest for ``bulk_update_documents``."""
    return {
        "_metadata": {
            "purpose": (
                "imaging_report -> finer-grained radiology / cardio / "
                "endoscopy / surgical / pathology kinds"
            ),
            "n_items": len(proposals),
            "schema": "bvphoenix.bulk_document_update v1",
        },
        "proposals": [asdict(p) for p in proposals],
        "items": [
            {
                "document_id": p.document_id,
                "document_type": p.proposed_type,
            }
            for p in proposals
        ],
    }


async def _amain(args: argparse.Namespace) -> int:
    from bvphoenix.db.session import SERVICE_SUBJECT, SessionFactory, set_current_subject

    session = SessionFactory()
    try:
        await set_current_subject(session, SERVICE_SUBJECT)
        proposals = await _walk(
            session,
            patient_id=uuid.UUID(args.patient_id) if args.patient_id else None,
            limit=int(args.limit),
        )
    finally:
        await session.close()

    output = _emit(proposals)
    out_path = Path(args.out).resolve()
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(proposals)} proposals to {out_path}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Output JSON path.")
    parser.add_argument(
        "--patient-id",
        default=None,
        help="Optional UUID to scope the scan to a single patient.",
    )
    parser.add_argument(
        "--limit",
        default=1000,
        help="Cap the number of documents to scan (default 1000).",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
