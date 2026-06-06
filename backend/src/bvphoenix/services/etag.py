"""ETag helpers for optimistic concurrency on versioned resources.

Per ADR 0001, every versioned entity (consultation, document, ...) lives
on a dedicated branch in the patient DAG (``services/versioning.py``).
The HTTP ETag of the resource is the hex of the branch head's
``commit_hash``. This module provides:

* :func:`etag_for_branch` — async lookup of the current head, returns the
  hex string or ``None`` when the branch has no commit yet.
* :func:`format_etag` / :func:`parse_if_match` — strong ETag formatting
  per RFC 9110 §13.1.1 (quoted, optional weak ``W/`` prefix; we always
  emit strong).
* :func:`require_if_match` — FastAPI dependency-style helper that pulls
  the ``If-Match`` header from a :class:`Request` and compares it against
  the supplied current ETag, raising 412 Precondition Failed when they
  differ.

Sprint 1 intent: every mutating endpoint that wants optimistic
concurrency (PATCH document, PATCH consultation, ...) calls
``etag_for_branch`` once, then ``require_if_match`` against the request,
and finally returns the *new* ETag (post-commit) in the response
``ETag`` header.

ADR pointers
------------
* ADR 0001 — DAG-backed versioning (commit_hash is the ETag).
* ADR 0002 — Idempotency interaction (ETag is *separate* from
  Idempotency-Key; a 412 here is also recorded as a 412 in the
  idempotency cache, so the agent learns the same state both ways).
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def format_etag(commit_hash_hex: str) -> str:
    """Wrap a hex commit hash in a strong ETag literal.

    >>> format_etag("abc")
    '"abc"'
    """
    return f'"{commit_hash_hex}"'


def parse_if_match(header_value: str | None) -> str | None:
    """Strip surrounding quotes and any ``W/`` weak prefix.

    Returns the inner token, the literal ``"*"`` sentinel for the
    wildcard form (RFC 9110 §13.1.1: "If-Match: *" means "any current
    representation"), or ``None`` if the header is absent/blank.
    Callers that want to refuse the wildcard handle it explicitly;
    ``require_if_match`` accepts it because it matches the RFC
    semantics agents expect when they want to bypass optimistic
    concurrency on a deliberately idempotent mutation.
    """
    if header_value is None:
        return None
    raw = header_value.strip()
    if not raw:
        return None
    if raw == "*":
        return "*"
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1]
    return raw


async def etag_for_branch(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    ref_name: str,
) -> str | None:
    """Return the hex of the current head commit on ``(patient_id, ref_name)``.

    ``None`` means the branch has not been created yet (no commit was
    ever recorded for this entity). Callers decide whether that is a
    legitimate state (e.g. an entity created via a non-DAG path that
    will receive its first commit on this PATCH) or a 404.
    """
    row = (
        await db.execute(
            text(
                "SELECT encode(commit_hash, 'hex') FROM refs "
                "WHERE patient_id = :pid AND ref_name = :rn"
            ),
            {"pid": patient_id, "rn": ref_name},
        )
    ).first()
    if row is None:
        return None
    (etag_hex,) = row
    return etag_hex


def require_if_match(request: Request, current_etag_hex: str | None) -> None:
    """Enforce ``If-Match`` against ``current_etag_hex``.

    Raises:
        HTTPException 428 Precondition Required when the header is
        missing on a resource that has an ETag. Some clients may want
        to opt-out (server-only writes during seed); they can pass
        ``current_etag_hex=None`` to skip the check entirely.
        HTTPException 412 Precondition Failed when the header is
        present but does not match.
    """
    if current_etag_hex is None:
        # Branch not created yet — first commit will assign the ETag.
        # If the client sent If-Match anyway, that's a stale view.
        if request.headers.get("if-match"):
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail=("If-Match supplied but resource has no version yet"),
            )
        return

    presented = parse_if_match(request.headers.get("if-match"))
    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header is required for this mutation",
        )
    if presented == "*":
        # RFC 9110 §13.1.1: wildcard matches any current representation.
        # Caller has explicitly opted out of optimistic concurrency.
        return
    if presented != current_etag_hex:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=(f"If-Match {presented!r} does not match current ETag {current_etag_hex!r}"),
        )


def enforce_if_match_value(if_match: str | None, current_etag: str) -> None:
    """Variant of :func:`require_if_match` that takes the raw header
    value (or ``None``) instead of a :class:`Request`. Used by the
    FSM transition handlers in ``api/clinical_events`` and
    ``api/patient_tasks`` that read ``If-Match`` via FastAPI's
    ``Header(alias='If-Match')`` parameter rather than from the
    request headers map.

    Same contract: 428 when the header is missing, 412 when present
    but mismatched. The wildcard ``*`` is accepted (the caller has
    opted out of optimistic concurrency).
    """
    presented = parse_if_match(if_match)
    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header is required for this mutation",
        )
    if presented != "*" and presented != current_etag:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="If-Match does not match current ETag",
        )


def enforce_optional_if_match(
    if_match: str | None, current_etag: str, *, what: str = "resource"
) -> None:
    """Opt-in optimistic-concurrency guard (the Document / Marker / Finding
    pattern, as opposed to the mandatory :func:`enforce_if_match_value`).

    When the caller supplies ``If-Match`` a stale token is rejected with
    412; when absent the write proceeds (last-write-wins). Agents SHOULD
    pass the ETag they read so a concurrent edit cannot be silently
    clobbered; a single-editor first-party UI may omit it. ``*`` opts out.
    ``what`` is interpolated into the 412 detail for a clearer message.
    """
    presented = parse_if_match(if_match)
    if presented is not None and presented != "*" and presented != current_etag:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=f"If-Match {presented!r} does not match the current {what} etag",
        )


__all__ = [
    "enforce_if_match_value",
    "enforce_optional_if_match",
    "etag_for_branch",
    "format_etag",
    "parse_if_match",
    "require_if_match",
]
