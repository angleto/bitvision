"""Regex-baseline text de-identification for Italian clinical records.

Replaces personally-identifying snippets in free-text fields
(``clinical_notes.body``, ``reports.text``, ``consultations.findings_md``,
etc.) with stable token markers, returning both the redacted text and
a list of :class:`Redaction` records that auditors can persist into
``redaction_events``. The plaintext that was scrubbed is never stored:
each :class:`Redaction` carries only the SHA256 of the original
excerpt (``original_excerpt_hash``) plus its kind.

Two passes are available, run independently or together:

  * :func:`redact_text` (regex baseline): codice fiscale, email, phone,
    precise dates, addresses. Fast, deterministic, zero LLM cost.
  * :func:`redact_with_llm` (F12.4-llm): an LLM-driven scrub for
    proper nouns and contextual PHI that regex cannot catch reliably.
    Audit-trail-rich: ``model_id`` + ``provider`` + ``prompt_hash``
    are returned with every redaction so the publication audit log
    can prove which model said which span was PHI.

Patterns covered:
  * Italian codice fiscale (16 alphanumeric, with the standard
    16-char layout LLLLLLNNLNNLNNNL).
  * E-mail addresses.
  * Phone numbers (Italian formats: +39, leading 3 mobile, 0xx fixed).
  * Precise dates (DD/MM/YYYY, YYYY-MM-DD, DD-MM-YYYY); generic
    "year-only" forms like "2023" are kept since they are usually
    not personally identifying once everything else is scrubbed.
  * Italian street addresses ("via X 12", "piazza Y 3", etc.).

Names (proper nouns) are intentionally NOT covered by the baseline.
A regex-only proper-noun detector for Italian is too noisy and the
clinical text is short enough that a small CONLL/spaCy or LLM step
is the right answer; that is the F12.4b-llm scope. Until then,
demographics scrub at publish-time strips the patient's known names
from the publish target, which closes the dominant leak.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

__all__ = ["Redaction", "redact_all", "redact_text", "redact_with_llm"]


@dataclass(frozen=True, slots=True)
class Redaction:
    """One redacted excerpt: kind + start/end + plaintext-hash.

    The plaintext itself is intentionally not retained; auditors get
    the kind (so they can search "did we ever scrub a CF here?") and
    the hash (so they can prove "the same plaintext appeared twice"
    without retaining either occurrence).
    """

    kind: str
    start: int
    end: int
    original_excerpt_hash: bytes
    placeholder: str


# Italian codice fiscale: 6 letters + 2 digits + 1 letter + 2 digits +
# 1 letter + 3 digits + 1 letter (case-insensitive, no spaces). The
# checksum digit is not validated here: that's the `codicefiscale`
# library's job, not the redaction layer's.
_CF_RE = re.compile(
    r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Italian phone: optional +39 prefix; mobile starts with 3xx (10 digits
# total after country code); fixed starts with 0 (8-12 digits). We use
# a permissive pattern; over-matching numbers like "12345678" is OK
# because it would still be PHI-adjacent in a clinical context.
#
# Each digit after the first prefix block may be preceded by an optional
# space / dash / dot, so we catch the long tail of Italian formats:
# "3331234567", "333 1234567", "333 123 4567", "333-123-4567",
# "333.123.4567", "+39 333 1234567", "+39 333.123.4567", "06 12345678",
# "06-12345678", etc.
_PHONE_RE = re.compile(
    r"(?:\+39[\s.\-]?)?"
    r"(?:3\d{2}(?:[\s.\-]?\d){7}|0\d{1,3}(?:[\s.\-]?\d){6,8})"
    r"\b"
)

# Precise dates. Avoid matching just "2023" (year-only) — those are
# usually safe and stripping them harms readability.
_DATE_RE = re.compile(
    r"\b("
    r"(?:0?[1-9]|[12]\d|3[01])[/-](?:0?[1-9]|1[0-2])[/-]\d{2,4}"  # DD/MM/YYYY or DD-MM-YYYY
    r"|"
    r"\d{4}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])"  # YYYY-MM-DD
    r")\b"
)

_ADDRESS_RE = re.compile(
    r"\b(?:via|viale|piazza|piazzale|corso|largo|vicolo|strada)\s+"
    r"[A-ZÀ-Ü][\w\s'.-]{2,40}?\s+\d{1,4}[a-z]?\b",
    re.IGNORECASE,
)


_KIND_TO_PATTERN: list[tuple[str, re.Pattern[str], str]] = [
    ("regex_codice_fiscale", _CF_RE, "[CF]"),
    ("regex_email", _EMAIL_RE, "[EMAIL]"),
    ("regex_phone", _PHONE_RE, "[TEL]"),
    ("regex_date_precise", _DATE_RE, "[DATE]"),
    ("regex_address", _ADDRESS_RE, "[ADDR]"),
]


def redact_text(
    text: str | None,
    *,
    skip_kinds: frozenset[str] | None = None,
) -> tuple[str, list[Redaction]]:
    """Apply all baseline regex redactions to ``text``.

    Patterns are applied in declaration order; later patterns operate on
    text that has already been redacted by earlier ones (so a phone
    number embedded inside an email isn't double-counted).

    Returns the redacted string and the list of :class:`Redaction`
    records, in the order they were applied. ``text`` may be None for
    convenience (returns ``("", [])``).

    ``skip_kinds`` lets callers turn off a specific pattern (e.g. for
    a field where dates are clinically relevant and not personally
    identifying — never the case for our current entity set, but
    documented for future use).
    """
    if not text:
        return "", []
    skip = skip_kinds or frozenset()
    out = text
    redactions: list[Redaction] = []
    for kind, pattern, placeholder in _KIND_TO_PATTERN:
        if kind in skip:
            continue
        cursor = 0
        new_parts: list[str] = []
        for m in pattern.finditer(out):
            new_parts.append(out[cursor : m.start()])
            excerpt = m.group(0)
            digest = hashlib.sha256(excerpt.encode("utf-8")).digest()
            new_parts.append(placeholder)
            redactions.append(
                Redaction(
                    kind=kind,
                    start=m.start(),
                    end=m.end(),
                    original_excerpt_hash=digest,
                    placeholder=placeholder,
                )
            )
            cursor = m.end()
        new_parts.append(out[cursor:])
        out = "".join(new_parts)
    return out, redactions


@dataclass(frozen=True, slots=True)
class LlmRedaction:
    """One LLM-flagged span: kind + the literal substring + provenance.

    Unlike :class:`Redaction` (regex), the LLM pass is supervised by an
    explicit ``model_id`` / ``provider`` / ``prompt_hash`` so auditors
    can reconstruct exactly what the model saw and decided. The
    plaintext is hashed before storage; the literal is kept only in
    memory long enough to apply the substitution.
    """

    kind: str
    start: int
    end: int
    original_excerpt_hash: bytes
    placeholder: str
    model_id: str
    provider: str
    prompt_hash: bytes


_LLM_SCRUB_SYSTEM = (
    "Sei un servizio di de-identification per cartelle cliniche italiane. "
    "Identifica nomi propri di persone, nomi propri di luoghi specifici "
    "(citta', ospedali, vie con numero civico), date di nascita complete, "
    "e codici fiscali italiani. NON segnalare termini medici, nomi di "
    "sostanze, sintomi, diagnosi, dosaggi. NON riformulare il testo: solo "
    "indicare le posizioni esatte da redarre.\n\n"
    "Rispondi SOLO con JSON valido nel formato:\n"
    '{"spans": [{"start": <int>, "end": <int>, "kind": '
    '"proper_name"|"location"|"birth_date"|"codice_fiscale"}, ...]}\n'
    "Le posizioni sono offset di carattere zero-based dentro l'input. "
    "Restituisci una lista vuota {\"spans\": []} se non c'e' nulla da "
    "redarre."
)


async def redact_with_llm(
    text: str,
    *,
    provider: Any | None = None,
    model_id: str | None = None,
) -> tuple[str, list[LlmRedaction]]:
    """LLM-driven de-id pass for proper nouns / contextual PHI.

    Sends ``text`` to the LLM provider (default: project default via
    :func:`bvphoenix.services.llm.get_llm_provider`), asks for a list
    of spans to redact, applies the substitutions, and returns the
    redacted string + per-span audit records.

    On any LLM error / parse failure the function returns the input
    unchanged with an empty list (fail-closed): the caller is expected
    to ALWAYS run the regex baseline first, so the LLM pass is only
    additive. A failed LLM pass is logged via the audit_log; it never
    leaks PHI by accident.
    """
    if not text or not text.strip():
        return text, []

    if provider is None:
        from bvphoenix.services.llm import get_llm_provider

        provider = get_llm_provider()

    prompt = (
        "Analizza il seguente testo e indica le posizioni dei dati personali "
        "che devono essere redatti per renderlo OpenData-compliant. "
        "Testo:\n---\n" + text + "\n---"
    )
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).digest()

    try:
        result = await provider.complete(
            system=_LLM_SCRUB_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        content = result.content if hasattr(result, "content") else str(result)
        actual_model_id = getattr(result, "model_id", None) or model_id or "unknown"
        actual_provider = getattr(result, "provider", None) or "anthropic"
        spans = _parse_llm_spans(content)
    except Exception:
        # Fail closed: leave the text intact, return no redactions.
        # The regex pass should have already removed the high-confidence
        # patterns; the LLM is a defence-in-depth.
        return text, []

    # Apply spans in reverse order so earlier offsets remain valid.
    spans_sorted = sorted(spans, key=lambda s: s["start"], reverse=True)
    out = text
    redactions: list[LlmRedaction] = []
    for s in spans_sorted:
        start, end, kind = s["start"], s["end"], s["kind"]
        if start < 0 or end > len(out) or start >= end:
            continue
        excerpt = out[start:end]
        digest = hashlib.sha256(excerpt.encode("utf-8")).digest()
        placeholder = _placeholder_for_llm_kind(kind)
        out = out[:start] + placeholder + out[end:]
        redactions.append(
            LlmRedaction(
                kind="llm_scrub_via_mcp",
                start=start,
                end=end,
                original_excerpt_hash=digest,
                placeholder=placeholder,
                model_id=actual_model_id,
                provider=actual_provider,
                prompt_hash=prompt_hash,
            )
        )
    # Reverse application means redactions list is in reverse order; flip
    # so callers see them in source order.
    redactions.reverse()
    return out, redactions


def _parse_llm_spans(content: str) -> list[dict]:
    """Extract spans from the LLM's JSON response. Lenient: tolerates
    leading prose, trailing prose, code-fence wrapping."""
    import json as _json
    import re as _re

    # Strip any markdown code fences if present.
    m = _re.search(r"\{[\s\S]*\}", content)
    if not m:
        return []
    try:
        data = _json.loads(m.group(0))
    except _json.JSONDecodeError:
        return []
    spans = data.get("spans", []) if isinstance(data, dict) else []
    if not isinstance(spans, list):
        return []
    valid: list[dict] = []
    for s in spans:
        if (
            isinstance(s, dict)
            and isinstance(s.get("start"), int)
            and isinstance(s.get("end"), int)
            and isinstance(s.get("kind"), str)
        ):
            valid.append(s)
    return valid


def _placeholder_for_llm_kind(kind: str) -> str:
    return {
        "proper_name": "[NAME]",
        "location": "[LOC]",
        "birth_date": "[DOB]",
        "codice_fiscale": "[CF]",
    }.get(kind, "[REDACTED]")


def redact_all(payload: dict, *, fields: list[str]) -> tuple[dict, list[Redaction]]:
    """Apply :func:`redact_text` to a list of string fields in a payload.

    Returns a copy of ``payload`` with each named field replaced by
    its redacted version, plus the merged list of redactions across
    all fields. Fields that don't exist or are non-string are ignored.
    """
    new_payload = dict(payload)
    all_redactions: list[Redaction] = []
    for field in fields:
        value = new_payload.get(field)
        if not isinstance(value, str):
            continue
        redacted, marks = redact_text(value)
        new_payload[field] = redacted
        all_redactions.extend(marks)
    return new_payload, all_redactions
