"""De-identification helpers for DICOM bytes and free-text payloads.

The DICOM path delegates to the in-house PS3.15 engine in
``bvphoenix.services.deid`` — a table-driven Basic Application Confidentiality
Profile with options (consistent salted UID remap, per-patient date shift,
private-tag removal, coded ``DeidentificationMethodCodeSequence`` provenance,
and a fail-closed verification pass). This module keeps the stable facade
(``deidentify_dicom_bytes`` / ``should_deidentify``) so call sites are
unchanged, plus the free-text helpers used by the consultation / publish
pipelines.

Pixel data is NOT touched here — PHI burned into the image is handled by
``bvphoenix.services.pixel_deid`` (risk gate today; redaction + human review in
M1/M4).

The text helpers (``deidentify_text``, ``deidentify_markdown``) support the
consultation pipeline: every consultation defaults to ``deidentify=True`` so LLM
prompts and outbound summaries never carry directly-identifying patient data
unless the clinician explicitly opts out. Consent is snapshotted alongside (see
``consent_snapshot.py``).

Usage:

    from bvphoenix.services.deidentify import deidentify_dicom_bytes, should_deidentify

    if should_deidentify(grant, study):
        dcm = deidentify_dicom_bytes(dcm)

The DICOM function is pure (input bytes -> output bytes) so it runs safely from
an ``asyncio.to_thread`` wrapper in request handlers.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from bvphoenix.services.deid.engine import scrub_dicom_bytes

if TYPE_CHECKING:
    from bvphoenix.db.models import Patient


def deidentify_dicom_bytes(src: bytes) -> bytes:
    """Return a new DICOM byte string with PHI removed / pseudonymised.

    Delegates to the in-house PS3.15 engine
    (:func:`bvphoenix.services.deid.engine.scrub_dicom_bytes`). Raises
    ``pydicom.errors.InvalidDicomError`` on unparseable input,
    ``DeidVerificationError`` if the post-scrub verification finds residual PHI,
    or ``RequiresReview`` for SR / encapsulated objects. Callers treat any
    exception as "withhold this instance" — we never hand back un-verified
    bytes.
    """
    return scrub_dicom_bytes(src)


# ---- Text / Markdown de-identification ----
#
# The patterns below cover the identifier shapes we have actually seen in
# clinical notes, reports, and consultation transcripts. They are
# deliberately conservative: better to over-redact a user-entered email
# than to leak one into an LLM context window. The CF and email regexes
# match the ones in ``bvphoenix.logging`` / ``bvphoenix.services.audit``
# — keep them in sync if you tune any of them.

# Italian codice fiscale: 6 letters + 2 digits + letter + 2 digits +
# letter + 3 digits + letter. Case-insensitive because user-entered text
# is frequently lowercased.
_TAX_ID_RE = re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Optional ``+`` country prefix, then 6+ digits with space / dash / dot /
# parens tolerated between groups. ``(?<!\d)`` / ``(?!\d)`` prevent
# nibbling into longer digit runs (accession numbers, study UIDs).
_PHONE_RE = re.compile(r"(?<!\d)\+?\d(?:[\d\s().\-]{5,}\d)(?!\d)")

# Full ISO date → bare year: preserves relative timing for clinical
# context while dropping the exact day, a direct quasi-identifier under
# HIPAA Safe Harbor.
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b")

_NAME_TOKEN_SPLIT_RE = re.compile(r"[\s,;/]+")


def _name_tokens(name: str) -> list[str]:
    """Return name tokens of length >= 2.

    Single-letter middle initials are dropped: replacing every lone "M"
    in the surrounding text would destroy the document.
    """
    return [t for t in _NAME_TOKEN_SPLIT_RE.split(name.strip()) if len(t) >= 2]


def deidentify_text(
    text: str,
    patient_name: str | None = None,
    patient_tax_id: str | None = None,
    *,
    patient_email: str | None = None,
    patient_phone: str | None = None,
    extra_names: tuple[str, ...] | None = None,
) -> str:
    """Return ``text`` with direct patient identifiers redacted.

    Substitutes names → ``[PATIENT]``, codice-fiscale-shaped strings →
    ``[TAX_ID]``, emails → ``[EMAIL]``, phones → ``[PHONE]``, and
    collapses full ISO dates (``YYYY-MM-DD``) to the year alone.

    Pure and deterministic: identical input produces identical output,
    so the result is safe to cache or snapshot.
    """
    if not text:
        return text

    # Dedupe names (longest first) so "Mario Rossi" is replaced before
    # the token pass rewrites "Mario" → "[PATIENT]" and leaves "Rossi"
    # dangling.
    candidates = [patient_name, *(extra_names or ())]
    seen: set[str] = set()
    ordered_names: list[str] = []
    for n in sorted((c for c in candidates if c), key=len, reverse=True):
        if n not in seen:
            seen.add(n)
            ordered_names.append(n)

    out = text
    for full in ordered_names:
        out = re.sub(re.escape(full), "[PATIENT]", out, flags=re.IGNORECASE)

    # Token pass catches "Rossi, Mario" / "Dr. Rossi" / surname-only
    # references.
    tokens: set[str] = set()
    for full in ordered_names:
        tokens.update(_name_tokens(full))
    for token in sorted(tokens, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(token)}\b", "[PATIENT]", out, flags=re.IGNORECASE)

    if patient_tax_id:
        out = re.sub(re.escape(patient_tax_id), "[TAX_ID]", out, flags=re.IGNORECASE)
    out = _TAX_ID_RE.sub("[TAX_ID]", out)

    if patient_email:
        out = re.sub(re.escape(patient_email), "[EMAIL]", out, flags=re.IGNORECASE)
    out = _EMAIL_RE.sub("[EMAIL]", out)

    if patient_phone:
        out = re.sub(re.escape(patient_phone), "[PHONE]", out)
    out = _PHONE_RE.sub("[PHONE]", out)

    # Date pass runs last so a tax ID / phone fragment that happens to
    # share a digit run with YYYY-MM-DD cannot be misinterpreted.
    return _ISO_DATE_RE.sub(r"\1", out)


def deidentify_markdown(md: str, patient: Patient) -> str:
    """De-identify a markdown document against a Patient ORM row.

    Thin wrapper over :func:`deidentify_text` that pulls the sensitive
    fields off a ``Patient`` model. Markdown structure (headings, code
    fences, tables) is preserved verbatim — we operate on the raw string
    because the identifiers we scrub are never meaningful markdown
    syntax.
    """
    if not md:
        return md
    return deidentify_text(
        md,
        patient_name=getattr(patient, "display_name", None),
        patient_tax_id=getattr(patient, "tax_id", None),
        patient_email=getattr(patient, "email", None),
        patient_phone=getattr(patient, "phone", None),
    )


def should_deidentify(share_grant: Any, study: Any = None) -> bool:
    """Return True if a study should be served with PHI scrubbed.

    Two independent triggers:

    * ``share_grant.deidentify`` — the long-standing share-link flag
      (docs/security-encryption-deidentify-cors.md §2).
    * ``study.contribution_tier == 't3'`` — F6 opt-in to the training
      pool. T3 means "anonymised"; even a non-grant read path must
      scrub PHI. T4 (public CC) is left to the uploader's
      responsibility — by choosing T4 the owner has asserted the
      content is safe to publish verbatim. T1 / T2 only scrub when a
      grant explicitly requests it.

    Accepts a ``Grant`` ORM object (preferred path — checks the column),
    a ``ShareLink`` (unwraps via ``grant`` attr if present), or a plain
    mapping. Anything else → False (fail closed toward no-scrub: the call
    site is expected to default to a conservative mode if the grant isn't
    resolvable, and producing PHI by accident is worse than returning an
    obviously unshielded file during dev).
    """
    if study is not None:
        tier = getattr(study, "contribution_tier", None)
        if tier == "t3":
            return True
        # Contributed public studies (our contribution flow: is_public with no
        # external source_collection) are ALWAYS served de-identified — never
        # trust the uploader's tier choice (T4 historically meant "serve
        # verbatim"). Externally-imported public datasets (TCIA etc.) carry a
        # source_collection and are already de-identified upstream, so they are
        # served as-is (re-scrubbing would corrupt their citable UIDs/dates).
        if getattr(study, "is_public", False) and not getattr(study, "source_collection", None):
            return True
    if share_grant is None:
        return False
    flag = getattr(share_grant, "deidentify", None)
    if flag is None and isinstance(share_grant, dict):
        flag = share_grant.get("deidentify")
    return bool(flag)
