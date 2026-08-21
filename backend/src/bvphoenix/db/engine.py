"""Single factory for every SQLAlchemy engine in the monorepo.

Why this module exists: SQLAlchemy's ``JSON``/``JSONB`` bind processor
falls back to stdlib :func:`json.dumps` unless the engine is built with
a ``json_serializer``. Without one, writing a ``date`` / ``datetime`` /
``UUID`` into *any* JSONB column raises ``TypeError`` at bind time and
surfaces as a bare 500: ``middleware/problem_details.py`` only registers
handlers for ``StarletteHTTPException`` and ``RequestValidationError``,
so nothing turns that ``TypeError`` into a structured response.

That is exactly how ``PATCH /api/clinical-events/{id}`` started
returning "500: Internal Server Error" whenever the payload moved a
date: the handler builds an audit ``diff`` out of raw model attributes
and hands it to ``provenance_events.diff`` (JSONB). Every writer that
remembered to ``.isoformat()`` by hand was fine; the one that forgot
took the endpoint down. Per-call-site discipline is the thing that
already failed, so the fix goes at the serialisation choke point and
``tests/test_db_engine_factory.py`` keeps it there.

The encoder is deliberately **fail-loud**: only the types we knowingly
persist are coerced. A stray ORM instance or file handle still raises,
because silently ``str()``-ing an unknown object into the audit chain
would trade a visible 500 for an unreadable audit record.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import enum
import json
import uuid
from typing import Any

from sqlalchemy import create_engine as _create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine


def json_default(obj: Any) -> Any:
    """``default=`` hook for :func:`json.dumps` on JSON/JSONB binds.

    Handles the value types the ORM layer legitimately hands to a JSONB
    column (temporal values in audit diffs and snapshots, ``UUID``
    identifiers, ``Decimal`` measurements, ``Enum`` members, ``set``
    membership lists) and raises for anything else.
    """
    if isinstance(obj, _dt.datetime | _dt.date | _dt.time):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        # JSON has no decimal type. Callers that need exactness (money,
        # dosages) must stringify before the write.
        return float(obj)
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, set | frozenset):
        return sorted(obj, key=repr)
    raise TypeError(
        f"{type(obj).__name__} is not JSON serialisable; convert it explicitly "
        "before writing it to a JSON/JSONB column"
    )


def dumps(obj: Any) -> str:
    """JSON encoder used for every JSON/JSONB bind parameter."""
    return json.dumps(obj, default=json_default, ensure_ascii=False)


_JSON_KWARGS: dict[str, Any] = {
    "json_serializer": dumps,
    "json_deserializer": json.loads,
}


def make_async_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """``create_async_engine`` with the JSON codecs already wired."""
    return _create_async_engine(url, **_JSON_KWARGS, **kwargs)


def make_sync_engine(url: str, **kwargs: Any) -> Engine:
    """``create_engine`` with the JSON codecs already wired."""
    kwargs.setdefault("future", True)
    return _create_engine(url, **_JSON_KWARGS, **kwargs)


__all__ = ["dumps", "json_default", "make_async_engine", "make_sync_engine"]
