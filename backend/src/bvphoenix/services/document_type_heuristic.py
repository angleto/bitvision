"""Heuristic classifier for uploaded patient-document file types.

The patient "fascicolo" (folder) can ingest loose PDFs, scans, phone
photos of paper forms, and hand-written notes. We do not want to force
the user to label each file manually, but we do want to route them into
the right bucket so search and permissions can differentiate (e.g. a
``personal_notebook`` image is treated more loosely than a formal
``discharge_letter``).

The approach is deliberately dumb and deterministic:

1. **Filename pass** — match the basename (stem + extension stripped and
   normalised) against an ordered list of regex rules. First hit wins.
2. **Text-preview pass** — when the filename is inconclusive and the
   caller provided the first few hundred characters of extracted text,
   re-run a parallel set of keyword rules tuned for document bodies
   (section headers like "Diagnosi", "Consenso informato", ...).
3. **Fallback by mime hint** — infer image vs text/PDF from the file
   extension: images default to ``personal_notebook`` (phone photo of a
   paper note), text/PDF defaults to ``clinical_note``.

Lexicon covers Italian and English because the user base is mostly
Italian but the paperwork increasingly mixes languages.
"""

from __future__ import annotations

import os
import re
from typing import Literal

# v3: ``document_type`` was retired in favour of the 3-axis taxonomy
# (``kind_id`` / ``provenance_id`` / ``authority_id``). The heuristic
# now produces a ``kind_id`` value drawn from the document_kinds catalog
# table (seeded in migration 0072). Validation is delegated to the
# DB-level FK; if the catalog grows beyond the seeded set, the YAML
# seed file is the source of truth and this Literal is updated
# alongside.
DocumentType = Literal[
    "consent",
    "discharge_summary",
    "prescription",
    "referral",
    "lab_result",
    "emergency_report",
    "radiology_report",
    "pathology_report",
    "surgical_report",
    "cardio_report",
    "endoscopy_report",
    "specialist_visit_note",
    "progress_note",
    "history_physical",
    "imaging_study_bundle",
    "personal_note",
    "unclassified",
]


# Image extensions that we treat as "phone photo of a paper note"
# rather than a structured document. Kept explicit (not mimetypes) so
# the decision stays obvious when reading a trace.
_IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic", ".heif"}
)

# Extensions that typically carry structured, parseable text. Anything
# outside the image or text buckets falls through to ``other``.
_TEXT_EXTS = frozenset({".pdf", ".txt", ".md", ".rtf", ".doc", ".docx", ".odt", ".html", ".htm"})


# Filename rules — ordered by priority. First match wins.
# Patterns are intentionally permissive (substring + word stems) because
# real-world filenames are noisy: "ricetta-bianca-dott-bianchi.pdf",
# "Lab Results 2024 (copy).pdf", "Discharge_Letter_Mario_Rossi.pdf", etc.
#
# Order matters: we check ER first (very explicit), then radiology /
# imaging (was previously misrouted to ``er_report``, now goes to
# ``imaging_report``), then specialist consultation reports, then the
# generic progress / H&P notes. Anything still unmatched falls through
# to the clinical_note / personal_notebook / other defaults.
_FILENAME_RULES: list[tuple[str, DocumentType]] = [
    (r"consen(s|t)|privacy", "consent"),
    (r"dimiss|discharge|letter[\s_-]*dimission", "discharge_summary"),
    (r"ricett|prescri(zione|ption)|terap", "prescription"),
    (r"rinvia|referral|richies|impegnativ", "referral"),
    (r"\blab\b|analisi|esame[\s_-]*labor|bloo?d|urine|sangue|emocrom", "lab_result"),
    (r"pronto[\s_-]*socc|\ber[\s_-]*(report|note)?|emergency|triage", "emergency_report"),
    # Imaging report — radiology/specialist diagnostic imaging. LOINC
    # 18748-4. Catches "Referto TAC torace.pdf", "MRI_brain_report.pdf",
    # "RX_torace_2024.pdf", "Eco_addome.pdf", and similar.
    (
        r"referto[\s_-]*(radiolog|imag|tc|tac|rm|rx|ct|mri|mr|pet|eco|ultrasou|us)|"
        r"radiolog|imaging[\s_-]*report|\b(tc|tac|rm|rx|ct|mri|pet|eco|ultraso|us)[\s_-]*(referto|report)?",
        "radiology_report",
    ),
    # Specialist consultation report — LOINC 11488-4. Oncology /
    # cardiology / neurology / dermatology / etc. visit summary letter.
    (
        r"visit[ae][\s_-]*specialist|specialist[\s_-]*(report|note|letter)|"
        r"oncolog|cardiolog|neurolog|dermatolog|pneumolog|urolog|ginecolog|gastroentr|"
        r"endocrinolog|reumatolog|psichiat|psicolog|otorin|ortope|nefrolog|"
        r"\bvisita[\s_-]+(?!special)\w+",
        "specialist_visit_note",
    ),
    # Progress / follow-up note — LOINC 11506-3.
    (
        r"progress[\s_-]*note|follow[\s_-]*up|follow-?up|controllo[\s_-]*(periodic|post)|decorso",
        "progress_note",
    ),
    # History and physical — LOINC 34117-2. Anamnesis + exam.
    (
        r"anamnes|history[\s_-]*and[\s_-]*physical|\bh&p\b|hp[\s_-]*note|esame[\s_-]*obiettiv",
        "history_physical",
    ),
]


# Text-preview rules — tuned for section headers and typical boilerplate
# found in the first ~500 characters of extracted PDF text. Same ordering
# semantics as filename rules.
_TEXT_PREVIEW_RULES: list[tuple[str, DocumentType]] = [
    (r"consenso[\s_-]*informato|informed[\s_-]*consent|privacy[\s_-]*policy", "consent"),
    (r"lettera[\s_-]*di[\s_-]*dimission|discharge[\s_-]*summary|dimission", "discharge_summary"),
    (
        r"prescrizione[\s_-]*medic|ricetta[\s_-]*medic|prescription|terapia[\s_-]*domic",
        "prescription",
    ),
    (r"richiesta[\s_-]*di[\s_-]*visit|impegnativa|referral|rinvia", "referral"),
    (
        r"referto[\s_-]*di[\s_-]*labora|analisi[\s_-]*clinic|lab(oratory)?[\s_-]*result|emocromo|urinocolt",
        "lab_result",
    ),
    (
        r"pronto[\s_-]*soccorso|emergency[\s_-]*(room|department)|triage[\s_-]*infermier",
        "emergency_report",
    ),
    # Imaging report bodies — section headers usually include "Tecnica",
    # "Reperti", "Conclusioni" plus the modality acronym.
    (
        r"referto[\s_-]*radiolog|radiology[\s_-]*report|diagnostic[\s_-]*imaging|"
        r"tomografia[\s_-]*compute|risonanza[\s_-]*magnetic|ecografi|"
        r"radiografi|tac[\s_-]*torace|rm[\s_-]*encefalo|pet[\s_-]*ct",
        "radiology_report",
    ),
    # Specialist visit reports — the salutation often spells out the
    # specialty in the first lines.
    (
        r"visita[\s_-]*specialist|specialist[\s_-]*consult|consultation[\s_-]*note|"
        r"visita[\s_-]*(oncolog|cardiolog|neurolog|dermatolog|pneumolog|urolog|"
        r"ginecolog|gastroenter|endocrinolog|reumatolog|psichiat|psicolog|"
        r"otorin|ortope|nefrolog|ematolog)",
        "specialist_visit_note",
    ),
    (
        r"progress[\s_-]*note|nota[\s_-]*di[\s_-]*decorso|follow[\s_-]*up|controllo[\s_-]*post",
        "progress_note",
    ),
    (
        r"anamnesi[\s_-]*(patologic|prossim|familiar)|history[\s_-]*and[\s_-]*physical|"
        r"esame[\s_-]*obiettiv",
        "history_physical",
    ),
    # "Diagnosi" on its own is a common header in both clinical notes and
    # ER reports — we leave it out of the early rules on purpose and let
    # the clinical_note / er_report fallback handle it.
]


_FILENAME_PATTERNS: list[tuple[re.Pattern[str], DocumentType]] = [
    (re.compile(pat, re.IGNORECASE), dtype) for pat, dtype in _FILENAME_RULES
]

_TEXT_PREVIEW_PATTERNS: list[tuple[re.Pattern[str], DocumentType]] = [
    (re.compile(pat, re.IGNORECASE), dtype) for pat, dtype in _TEXT_PREVIEW_RULES
]


def _match(
    patterns: list[tuple[re.Pattern[str], DocumentType]], haystack: str
) -> DocumentType | None:
    for pattern, dtype in patterns:
        if pattern.search(haystack):
            return dtype
    return None


def guess_document_type(
    filename: str,
    text_preview: str | None = None,
) -> DocumentType:
    """Return the best guess document type for an uploaded file.

    The filename is the primary signal; ``text_preview`` (typically the
    first ~500 characters of extracted PDF/text content) is consulted
    only when the filename is inconclusive. Defaults to
    ``clinical_note`` for PDF/text-like content, ``personal_notebook``
    for image uploads, and ``other`` for anything else.
    """
    # One splitext: drops directory prefix so paths like
    # ``"/tmp/xyz/Ricetta.pdf"`` still match the ``ricett`` rule, and
    # gives us the extension for the image/text fallback in one shot.
    base = os.path.basename(filename or "")
    stem, raw_ext = os.path.splitext(base)
    stem = stem.lower()
    ext = raw_ext.lower()

    # 1. Filename pass.
    hit = _match(_FILENAME_PATTERNS, stem)
    if hit is not None:
        return hit

    # 2. Text-preview pass — only if the caller gave us something useful.
    if text_preview:
        hit = _match(_TEXT_PREVIEW_PATTERNS, text_preview)
        if hit is not None:
            return hit

    # 3. Fallback by extension.
    if ext in _IMAGE_EXTS:
        return "personal_note"
    if ext in _TEXT_EXTS:
        return "progress_note"
    return "unclassified"
