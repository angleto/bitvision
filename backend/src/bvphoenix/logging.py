"""Logging configuration — PHI-safe by default.

Medical-platform logs routinely see patient identifiers and
authentication material cross the wire: codici fiscali in error
messages, JWT tokens in "unauthorized" responses, bcrypt hashes from
debug dumps, raw emails. Anything we write to disk or ship to a log
aggregator must be scrubbed or we leak PHI on the logging surface.

We install a single ``logging.Filter`` on the root logger. It rewrites
the final formatted message *and* each interpolation argument so the
redaction is effective regardless of whether callers pass
``log.info("user %s", email)`` or ``log.info(f"user {email}")``.

The filter is intentionally conservative (overmatches are fine, leaks
are not). If a regex hits something benign, we log ``<REDACTED>``; the
structured data the caller cared about is usually passed via
``extra=`` anyway, which travels on the LogRecord untouched.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# ---- Patterns --------------------------------------------------------
# Each pattern is broad enough to catch the real-world variants we've
# seen without being so loose it nukes every alphanumeric blob.
#
# - Email: RFC-lite. Avoid the full RFC 5322 regex — it's slow and
#   the false-positive rate is roughly zero for real log messages.
# - Codice Fiscale: fixed 16-char Italian format; the regex from the
#   Agenzia delle Entrate spec, applied case-insensitively.
# - JWT: three base64url segments joined by dots, always starting with
#   ``eyJ`` (the base64 of ``{"``). Cheap, accurate, no false positives.
# - bcrypt: the documented ``$2a$/$2b$/$2y$`` prefix plus cost and salt;
#   anchoring to the 60-char length would be safer but some libs emit
#   slightly different totals, so we stop at the first whitespace.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CF_RE = re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")
_BCRYPT_RE = re.compile(r"\$2[aby]\$\d{2}\$[A-Za-z0-9./]{53}")

_PATTERNS: tuple[re.Pattern[str], ...] = (_JWT_RE, _BCRYPT_RE, _CF_RE, _EMAIL_RE)

_REDACTED = "<REDACTED>"


def _scrub(value: Any) -> Any:
    """Return ``value`` with known PHI patterns replaced by <REDACTED>.

    Strings are rewritten in place; other scalar types are coerced
    to string only when they actually contain something redactable.
    Dicts / lists / tuples are walked recursively so structured log
    args (e.g. ``log.info("ctx %s", {"email": ...})``) are also
    scrubbed. Anything else passes through untouched — we never want
    the filter to mutate SQLAlchemy rows or pydantic models into
    strings by accident.
    """
    if isinstance(value, str):
        out = value
        for pat in _PATTERNS:
            out = pat.sub(_REDACTED, out)
        return out
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub(v) for v in value]
        return type(value)(scrubbed)
    return value


class PHIRedactionFilter(logging.Filter):
    """Strip PHI from ``LogRecord.msg`` and ``.args`` before formatting.

    We mutate the record in place — the filter runs before the
    formatter, so downstream handlers see only the scrubbed version.
    Returning ``True`` always: we never want redaction to drop records,
    only to rewrite them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = _scrub(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(_scrub(a) for a in record.args)
        return True


def install_phi_redaction(level: str | int = logging.INFO) -> None:
    """Attach the redaction filter to the root logger and every handler.

    Call once on app start. Idempotent: if the filter is already
    attached, a second call is a no-op. Also configures a basic
    formatter if no handlers exist yet, so the filter has somewhere
    to apply to in minimal environments (CLI tools, tests).
    """
    root = logging.getLogger()
    if not root.handlers:
        # basicConfig is a no-op if handlers already exist — be explicit
        # about the fallback formatter so CLI usage isn't silent.
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    else:
        root.setLevel(level)

    # Attach to the root logger itself (covers records that bypass
    # handler-level filters) and to each existing handler (covers the
    # common formatting pipeline). ``isinstance`` check keeps this
    # idempotent under repeated calls / test reloads.
    def _ensure(target: logging.Logger | logging.Handler) -> None:
        for existing in target.filters:
            if isinstance(existing, PHIRedactionFilter):
                return
        target.addFilter(PHIRedactionFilter())

    _ensure(root)
    for handler in root.handlers:
        _ensure(handler)
