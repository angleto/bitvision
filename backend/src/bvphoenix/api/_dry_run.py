"""``?dry_run=true`` query convention for mutating endpoints.

Spec sezione 2.3: every mutating endpoint that supports preview takes
the ``dry_run`` query parameter. When set, the endpoint MUST:

* compute the same diff / outcome it would compute on a real apply;
* return the diff in the response body without committing;
* never emit audit-log writes, side effects, or events.

Mechanics
---------
This module exposes :func:`dry_run_flag` as a FastAPI dependency and
the :func:`is_dry_run` plain-function variant for places where a
dependency injection is awkward (background helpers, helpers reused by
the idempotency middleware which already has the request).

The flag participates in the idempotency hash (see ADR 0002 and
``middleware/idempotency.py``), so the same body with ``dry_run=true``
and ``dry_run=false`` is recorded as two distinct cache entries.
"""

from __future__ import annotations

from fastapi import Query
from starlette.requests import Request

_TRUTHY = frozenset({"1", "true", "yes", "y", "t", "on"})


def is_dry_run(request: Request) -> bool:
    """Read ``?dry_run=...`` from a raw :class:`Request`.

    Useful for non-endpoint helpers (middlewares, hash functions). For
    endpoints prefer :func:`dry_run_flag` so the parameter shows up in
    the OpenAPI schema.
    """
    raw = request.query_params.get("dry_run")
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def dry_run_flag(
    dry_run: bool = Query(
        False,
        description=(
            "When true, the endpoint computes the change and returns the "
            "diff without committing. Audit, side effects, and downstream "
            "events are skipped. Participates in the Idempotency-Key hash."
        ),
    ),
) -> bool:
    """FastAPI dependency: surface ``dry_run`` in the schema."""
    return dry_run


__all__ = ["dry_run_flag", "is_dry_run"]
