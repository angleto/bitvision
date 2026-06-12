"""Capability codes for inbox addresses — Crockford base32, ~80 bits.

The code is the *whole* secret: knowing it lets anyone deliver mail
into the patient's review queue (never directly into the fascicolo —
the queue is the moat). Requirements:

* high entropy (default 80 bits ⇒ 16 chars) so online enumeration
  through the MTA is hopeless even before rate limits;
* an alphabet that survives being read aloud over the phone and typed
  from paper — Crockford base32 excludes ``I L O U`` and is decoded
  case-insensitively with ``i→1 l→1 o→0`` confusions normalised;
* lowercase canonical form, because the local-part of an address is
  conventionally rendered lowercase and SMTP servers vary on case
  preservation.

No check symbol: a mistyped code simply bounces at RCPT (550), and the
capability is long enough that a typo never lands on a *different*
valid address by accident.
"""

from __future__ import annotations

import secrets

# Crockford base32 alphabet (lowercase canonical form).
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_DECODE_NORMALISE = str.maketrans(
    {
        "i": "1",
        "l": "1",
        "o": "0",
        "-": None,  # paper-friendly grouping dashes are ignored
    }
)


def generate_code(bits: int = 80) -> str:
    """Return a fresh capability code with at least ``bits`` of entropy."""
    if bits < 40:
        raise ValueError("capability codes below 40 bits are not acceptable")
    n_chars = -(-bits // 5)  # ceil(bits / 5): each char carries 5 bits
    return "".join(_ALPHABET[secrets.randbelow(32)] for _ in range(n_chars))


def normalize_code(raw: str) -> str | None:
    """Canonicalise a code as received in an SMTP envelope.

    Returns the canonical lowercase form, or ``None`` when the string
    cannot be a code (illegal characters, absurd length) — the caller
    answers 550 without a DB round-trip.
    """
    candidate = raw.strip().lower().translate(_DECODE_NORMALISE)
    if not 8 <= len(candidate) <= 32:
        return None
    if any(c not in _ALPHABET for c in candidate):
        return None
    return candidate


def split_local_part(local_part: str) -> tuple[str, str] | None:
    """Split ``code+tag`` (RCPT local part) into ``(code, tag)``.

    Routing is on the envelope recipient, never on To/Cc headers. The
    tag is everything after the *first* ``+`` (sub-addressing servers
    differ on how they treat later pluses; we keep them in the tag).
    Returns ``None`` when there is no tag or the code part does not
    normalise.
    """
    stem, sep, tag = local_part.partition("+")
    if not sep or not tag:
        return None
    code = normalize_code(stem)
    if code is None:
        return None
    return code, tag.strip().lower()


__all__ = ["generate_code", "normalize_code", "split_local_part"]
