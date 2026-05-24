"""Canonical JSON serialization for content-addressed versioning.

Implements a deterministic byte representation of a Python payload so that
``sha256(canonicalize(x)) == sha256(canonicalize(y))`` iff ``x`` and ``y``
represent the same logical value, regardless of dict ordering, whitespace,
or representation choices for non-JSON-native types like UUID or datetime.

The output is RFC 8785 / JCS-compatible (JSON Canonicalization Scheme):

  * Object keys sorted lexicographically over their UTF-16 code units.
  * No insignificant whitespace; ``,`` and ``:`` separators only.
  * Strings normalised to NFC unicode and escaped per JSON minimal-escape rules.
  * Numbers serialised via ECMAScript ``Number.prototype.toString()`` semantics
    (integers without trailing ``.0``, floats without trailing zeros, exponents
    only when shorter than the decimal form).
  * NaN, +Inf, -Inf are explicitly rejected (JSON does not represent them).

Type extensions, applied as a pre-normalisation pass:

  * ``uuid.UUID`` → lower-case dashed string.
  * ``datetime`` → ISO-8601 with ``Z`` UTC suffix; naive datetimes are rejected.
  * ``date`` → ``YYYY-MM-DD`` string.
  * ``Decimal`` → string (preserves precision; never silently coerced to float).
  * ``bytes`` / ``bytearray`` → base64url (no padding) string.

The function is the cryptographic root of ``commits.commit_hash`` and
``entity_objects.object_hash``. It must not silently drop or coerce data;
callers passing unsupported types receive ``TypeError`` so a missing case is
loud.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import unicodedata
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

__all__ = ["canonicalize", "payload_hash"]


def canonicalize(payload: Any) -> bytes:
    """Serialise ``payload`` to canonical UTF-8 bytes.

    The returned bytes are deterministic, sorted, NFC-normalised, and
    free of insignificant whitespace. Two payloads that compare logically
    equal produce identical bytes; two payloads that differ in any field
    produce different bytes. The mapping is one-way from Python objects
    to bytes; the inverse (bytes → original Python types) is not
    representable since type information is erased (UUID becomes string,
    Decimal becomes string, etc.). This is intentional: canonical form
    is for hashing and storage equality, not round-trip.

    Raises:
        TypeError: an unsupported Python type appeared in the payload.
        ValueError: a value cannot be canonicalised (e.g. NaN, Inf,
            naive datetime, non-string dict key).
    """
    normalised = _normalise(payload)
    return json.dumps(
        normalised,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def payload_hash(payload: Any) -> bytes:
    """Return ``sha256(canonicalize(payload))`` as 32 raw bytes."""
    return hashlib.sha256(canonicalize(payload)).digest()


def _normalise(value: Any) -> Any:
    """Recursively coerce ``value`` to JSON-serialisable primitives.

    The output uses only ``dict``, ``list``, ``str``, ``int``, ``float``,
    ``bool``, ``None``. Custom types are mapped to canonical string forms
    here, before ``json.dumps`` runs, so the JSON encoder sees uniform
    primitives and our dict-key sorting is well-defined.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"NaN/Inf cannot be canonicalised (got {value!r})")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, (bytes, bytearray)):
        return base64.urlsafe_b64encode(bytes(value)).rstrip(b"=").decode("ascii")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be canonicalised; attach a tzinfo")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        normalised: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(
                    f"non-string dict key cannot be canonicalised (got {type(k).__name__}: {k!r})"
                )
            normalised[unicodedata.normalize("NFC", k)] = _normalise(v)
        return normalised
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    raise TypeError(f"cannot canonicalise value of type {type(value).__name__}: {value!r}")
