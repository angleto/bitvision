"""Idempotency-Key middleware (ADR 0002).

Endpoints opt in via the :func:`idempotent` dependency. The dependency
inspects the inbound request, computes a deterministic hash over
``(method, path, body, dry_run)`` and looks it up in
``idempotency_records``. On hit, the captured response is replayed; on
miss, the handler runs and the response is captured for 24h.

Conflict semantics
------------------
* same key + same hash + still warm → cache replay (200/201/etc).
* same key + different hash → 422 ``idempotency_conflict``.
* different key + same hash → cache miss, handler runs (intentional —
  the agent chose to mint a fresh key).
* missing key → cache miss; the dependency does not enforce
  idempotency, the handler runs and no row is written. Endpoints that
  *require* the header use :func:`require_idempotency_key` instead of
  :func:`idempotent`.

Why a dependency, not a wholesale ASGI middleware?
--------------------------------------------------
A wholesale middleware would intercept every request, even reads, and
need to buffer the response body to capture it. With a dependency we
keep the cost paid by opt-in mutating endpoints only, and the response
capture is delegated to the endpoint via ``IdempotencyContext.capture``
called once the body is assembled.

Wire shape
----------
The endpoint code reads:

    idem: IdempotencyContext = Depends(idempotent)
    if idem.replay is not None:
        return idem.replay
    ...
    return idem.capture(payload, status_code=201, extra_headers={...})

``capture`` returns a :class:`fastapi.Response` ready for the framework
to serialise; it also schedules the DB insert via the same session.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models.idempotency import IdempotencyRecord
from bvphoenix.db.session import get_db
from bvphoenix.middleware.problem_details import problem

_HEADER: Final[str] = "Idempotency-Key"
_DEFAULT_TTL_SECONDS: Final[int] = 24 * 60 * 60
_MAX_KEY_LEN: Final[int] = 255

# Headers we replay on cache hit (RFC: idempotency must look identical).
_REPLAY_HEADERS: Final[frozenset[str]] = frozenset({"etag", "location", "x-job-id", "content-type"})


def _canonical_body(body_bytes: bytes) -> str:
    """Return a canonical JSON form of ``body_bytes``.

    Empty bodies map to ``''`` so empty PATCH/POST never collide with a
    JSON ``null`` payload. Non-JSON bodies fall back to a SHA-256 hex of
    the raw bytes prefixed with ``raw:`` — agent traffic is JSON-only,
    but multipart uploads still need a stable hash.
    """
    if not body_bytes:
        return ""
    try:
        decoded = json.loads(body_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "raw:" + hashlib.sha256(body_bytes).hexdigest()
    return json.dumps(decoded, sort_keys=True, separators=(",", ":"))


def compute_request_hash(
    method: str,
    path: str,
    body_bytes: bytes,
    dry_run: bool,
) -> str:
    """SHA-256 hex of the canonical request envelope.

    ``dry_run`` participates in the hash so a preview and its real-apply
    are recorded as two distinct cache entries — see ADR 0002.
    """
    canonical = _canonical_body(body_bytes)
    payload = f"{method.upper()}|{path}|{canonical}|dry={dry_run!s}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class IdempotencyContext:
    """Per-request idempotency state passed to the handler.

    The handler should:

    1. Check ``replay`` — if not ``None``, return it immediately.
    2. Otherwise execute its business logic.
    3. Wrap the response via :meth:`capture` so the row is written.

    ``key`` is ``None`` when the client did not opt in. In that case
    ``capture`` is a passthrough and writes nothing.
    """

    request: Request
    db: AsyncSession
    key: str | None
    request_hash: str | None
    method: str
    path: str
    actor_subject_id: uuid.UUID | None
    replay: JSONResponse | None = None
    extra_replay_headers: dict[str, str] = field(default_factory=dict)

    def capture(
        self,
        payload: Any,
        *,
        status_code: int = status.HTTP_200_OK,
        extra_headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        """Wrap ``payload`` into a :class:`JSONResponse`.

        When the request carries an ``Idempotency-Key`` we also record
        the response so a future replay returns the same body, status,
        and the curated subset of headers (``ETag``, ``Location``,
        ``X-Job-Id``, ``Content-Type``).
        """
        headers = {**(extra_headers or {})}
        response = JSONResponse(content=payload, status_code=status_code, headers=headers)
        if self.key is None or self.request_hash is None:
            return response

        # Capture the body and the curated headers for replay.
        replay_headers: dict[str, str] = {}
        for name, value in response.headers.items():
            lname = name.lower()
            if lname in _REPLAY_HEADERS:
                replay_headers[name] = value
        if extra_headers:
            for name, value in extra_headers.items():
                if name.lower() in _REPLAY_HEADERS:
                    replay_headers[name] = value

        # We schedule the DB insert via the request's session. Failure
        # to write the cache MUST NOT break the user-visible response.
        async def _persist() -> None:
            try:
                stmt = (
                    pg_insert(IdempotencyRecord)
                    .values(
                        idempotency_key=self.key,
                        request_hash=self.request_hash,
                        actor_subject_id=self.actor_subject_id,
                        method=self.method,
                        path=self.path,
                        response_status=status_code,
                        response_body=payload
                        if isinstance(payload, (dict, list))
                        else {"data": payload},
                        response_headers=replay_headers,
                        expires_at=datetime.now(UTC) + timedelta(seconds=_DEFAULT_TTL_SECONDS),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            IdempotencyRecord.idempotency_key,
                            IdempotencyRecord.request_hash,
                        ]
                    )
                )
                await self.db.execute(stmt)
                await self.db.commit()
            except IntegrityError:
                # Race with another worker that wrote the same entry —
                # benign because both writers see the same payload.
                await self.db.rollback()
            except Exception:  # pragma: no cover — defensive
                await self.db.rollback()

        # Use Starlette's BackgroundTask so the insert runs after the
        # response is flushed; failure does not affect the user.
        from starlette.background import BackgroundTask

        response.background = BackgroundTask(_persist)
        return response


def _extract_actor_subject_id(request: Request) -> uuid.UUID | None:
    """Best-effort lookup of the actor on the current request.

    The auth deps stash the resolved User on ``request.state``; we try
    a few attribute names and fall back to ``None``.
    """
    user = getattr(request.state, "user", None)
    if user is not None and hasattr(user, "subject_id"):
        sid = user.subject_id
        if isinstance(sid, uuid.UUID):
            return sid
    return None


async def idempotent(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IdempotencyContext:
    """FastAPI dependency: build an :class:`IdempotencyContext`.

    No-op when the client did not send the ``Idempotency-Key`` header —
    the handler runs normally and nothing is written.
    """
    raw_key = request.headers.get(_HEADER)
    key = raw_key.strip() if raw_key else None
    if key is not None and (len(key) == 0 or len(key) > _MAX_KEY_LEN):
        raise problem(
            status.HTTP_400_BAD_REQUEST,
            "invalid_idempotency_key",
            f"{_HEADER} must be 1..{_MAX_KEY_LEN} chars",
        )

    method = request.method.upper()
    path = request.url.path

    body_bytes = b""
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        body_bytes = await request.body()
        # Re-attach for downstream handlers — Starlette buffers the
        # body once read, so subsequent ``await request.body()`` calls
        # return the same bytes. We don't need explicit caching.

    dry_run = _parse_dry_run(request)

    request_hash = compute_request_hash(method, path, body_bytes, dry_run) if key else None

    ctx = IdempotencyContext(
        request=request,
        db=db,
        key=key,
        request_hash=request_hash,
        method=method,
        path=path,
        actor_subject_id=_extract_actor_subject_id(request),
    )

    if key is None or request_hash is None:
        return ctx

    # Try replay.
    now = datetime.now(UTC)
    row = (
        await db.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.idempotency_key == key,
                IdempotencyRecord.request_hash == request_hash,
                IdempotencyRecord.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        ctx.replay = JSONResponse(
            content=row.response_body,
            status_code=row.response_status,
            headers=row.response_headers or {},
        )
        return ctx

    # Conflict check: same key, different hash, still warm → 422.
    conflict = (
        await db.execute(
            select(IdempotencyRecord.request_hash).where(
                IdempotencyRecord.idempotency_key == key,
                IdempotencyRecord.expires_at > now,
            )
        )
    ).first()
    if conflict is not None and conflict[0] != request_hash:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "idempotency_conflict",
            (
                f"Idempotency-Key {key!r} was previously used with a different "
                "request body or query; mint a new key to apply this change."
            ),
            extra={"idempotency_key": key},
        )

    return ctx


async def require_idempotency_key(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IdempotencyContext:
    """Variant of :func:`idempotent` that 400s when the header is absent."""
    if not request.headers.get(_HEADER):
        raise problem(
            status.HTTP_400_BAD_REQUEST,
            "missing_idempotency_key",
            f"{_HEADER} header is required for this operation",
        )
    return await idempotent(request, db)


def _parse_dry_run(request: Request) -> bool:
    """Extract ``?dry_run=true`` from the query string.

    Mirrors :mod:`bvphoenix.api._dry_run` semantics so the hash is
    consistent regardless of whether the endpoint actually opted in.
    """
    raw = request.query_params.get("dry_run")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "y"}


__all__ = [
    "IdempotencyContext",
    "compute_request_hash",
    "idempotent",
    "require_idempotency_key",
]
