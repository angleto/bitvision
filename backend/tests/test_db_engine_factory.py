"""The JSON/JSONB bind codec is a choke point, and it must stay one.

Background: ``PATCH /api/clinical-events/{id}`` returned a bare 500
whenever the payload moved a date. The handler builds an audit ``diff``
out of raw model attributes and writes it to ``provenance_events.diff``
(JSONB); SQLAlchemy's JSON bind processor falls back to stdlib
:func:`json.dumps` unless the engine was built with a
``json_serializer``, and stdlib ``json`` cannot encode a
:class:`datetime.date`. The ``TypeError`` escaped every handler in
``middleware/problem_details.py`` and surfaced as "Internal Server
Error".

The fix lives in :mod:`bvphoenix.db.engine`, which is the only module
allowed to build an engine. This file is the guard around that
decision, in three parts:

1. the app engine really carries the serializer (a refactor that
   reverts ``session.py`` to ``create_async_engine`` fails here);
2. the encoder handles exactly the value types the ORM legitimately
   hands to a JSONB column, and still raises ``TypeError`` for
   anything else — the fail-loud policy is asserted so nobody
   "repairs" a future crash with ``default=str`` and turns a visible
   500 into an unreadable audit record;
3. no other module in the monorepo builds its own engine, because a
   second factory is a second place to forget the codec, which is the
   discipline that already failed once.

Deliberately DB-free: it inspects the engine object and the dialect's
bind processor without opening a connection, so it runs in the plain
``backend-test`` CI job.
"""

from __future__ import annotations

import ast
import datetime
import decimal
import enum
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.dialects.postgresql import JSONB

from bvphoenix.db.engine import dumps, json_default, make_sync_engine
from bvphoenix.db.session import engine as app_engine

# ---------------------------------------------------------------------------
# 1. The engine carries the codec
# ---------------------------------------------------------------------------


def test_app_engine_has_json_serializer_wired() -> None:
    """``bvphoenix.db.session.engine`` must be built by the factory.

    The dialect stores the ``json_serializer`` kwarg as
    ``_json_serializer``; when the engine is built with plain
    ``create_async_engine`` the attribute is ``None`` and every JSONB
    bind silently falls back to stdlib ``json.dumps``.
    """
    assert app_engine.dialect._json_serializer is dumps
    assert app_engine.dialect._json_deserializer is json.loads


def test_sync_factory_also_wires_the_codec() -> None:
    """The sync factory (alembic-adjacent scripts, CLI backfills) must
    not be the hole in the fence. Built against a URL that is never
    connected to — engine construction is lazy."""
    eng = make_sync_engine("postgresql+psycopg://u:p@127.0.0.1:1/none")
    try:
        assert eng.dialect._json_serializer is dumps
        assert eng.dialect._json_deserializer is json.loads
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# 2. What the codec accepts, and what it refuses
# ---------------------------------------------------------------------------


class _Colour(enum.Enum):
    RED = "red"


def _bind(value: object) -> str:
    """Serialise ``value`` exactly the way a JSONB column bind does.

    Going through ``JSONB().bind_processor(dialect)`` rather than
    calling ``dumps`` directly is the point: it proves the wiring, not
    just the encoder.
    """
    processor = JSONB().bind_processor(app_engine.dialect)
    assert processor is not None, "JSONB bind processor missing on the app dialect"
    return processor(value)


def test_jsonb_bind_serialises_a_date_as_iso() -> None:
    """The literal shape that produced the 500: a raw ``date`` inside an
    audit diff. ``YYYY-MM-DD`` is asserted explicitly because the
    frontend and the ICS export both parse it as a calendar date."""
    payload = {"event_date": {"from": datetime.date(2026, 3, 1), "to": datetime.date(2026, 3, 2)}}
    assert json.loads(_bind(payload)) == {"event_date": {"from": "2026-03-01", "to": "2026-03-02"}}


def test_jsonb_bind_serialises_the_other_orm_native_types() -> None:
    """The remaining types a model attribute can legitimately carry into
    a JSONB column: tz-aware datetime, UUID, Decimal, Enum, set."""
    ident = uuid.uuid4()
    payload = {
        "when": datetime.datetime(2026, 3, 1, 23, 30, tzinfo=datetime.UTC),
        "at": datetime.time(23, 30),
        "id": ident,
        "volume_ml": decimal.Decimal("12.5"),
        "colour": _Colour.RED,
        "tags": {"b", "a"},
    }
    decoded = json.loads(_bind(payload))
    assert decoded["when"] == "2026-03-01T23:30:00+00:00"
    assert decoded["at"] == "23:30:00"
    assert decoded["id"] == str(ident)
    assert decoded["volume_ml"] == 12.5
    assert decoded["colour"] == "red"
    # ``set`` has no JSON counterpart; sorted for a stable audit diff.
    assert decoded["tags"] == ["a", "b"]


def test_jsonb_bind_still_raises_for_an_unknown_object() -> None:
    """Fail-loud policy, pinned.

    Widening this to ``default=str`` would stop the crash and start
    writing ``<bvphoenix.db.models.ClinicalEvent object at 0x...>``
    into the audit chain — a silent corruption of the record the diff
    exists to preserve. If a new type genuinely needs persisting, add
    it to :func:`json_default` with an explicit encoding.
    """

    class _NotSerialisable:
        pass

    with pytest.raises(TypeError, match="not JSON serialisable"):
        _bind({"oops": _NotSerialisable()})

    # Same contract at the encoder level, so a caller that reaches for
    # ``dumps`` directly gets the identical guarantee.
    with pytest.raises(TypeError, match="not JSON serialisable"):
        dumps({"oops": _NotSerialisable()})
    with pytest.raises(TypeError):
        json_default(_NotSerialisable())


def test_dumps_keeps_non_ascii_readable() -> None:
    """Audit diffs are read by humans; ``\\u00e0`` escapes make an
    Italian narrative unreadable in psql."""
    assert dumps({"t": "città"}) == '{"t": "città"}'


# ---------------------------------------------------------------------------
# 3. Nobody else builds an engine
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every package that talks to Postgres. A path that does not exist is
# skipped rather than failing the test: the monorepo layout is allowed
# to change, this guard is not the place to assert it.
_SCAN_ROOTS = (
    "backend/src",
    "backend/scripts",
    # ``backend/alembic`` is scanned so the ``env.py`` exemption below is
    # a real carve-out rather than a comment: a data migration that
    # builds its own engine to backfill a JSONB column would otherwise
    # never be seen by this guard, and would bind through stdlib
    # ``json.dumps`` — the failure mode the factory exists to remove.
    "backend/alembic",
    "workers/src",
    "crawler/src",
    "mcp/src",
)

_FORBIDDEN_FACTORIES = frozenset(
    {
        "create_engine",
        "create_async_engine",
        "engine_from_config",
        "async_engine_from_config",
    }
)

# The two modules that are ALLOWED to call SQLAlchemy's factories:
# the single app factory, and alembic's entry point (which cannot
# import through the app factory because it builds the engine from the
# alembic ini section, so it wires the same kwargs by hand).
_ALLOWED = frozenset(
    {
        "backend/src/bvphoenix/db/engine.py",
        "backend/alembic/env.py",
    }
)


def _local_names_bound_to_factories(tree: ast.AST) -> set[str]:
    """Names in this module that resolve to a forbidden factory.

    Import aliases matter: ``bvphoenix/db/engine.py`` itself imports
    ``create_engine as _create_engine``, so a walk that only matched
    the literal attribute name would miss a call site that renamed its
    import — exactly what somebody working around this test would do.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _FORBIDDEN_FACTORIES:
                    names.add(alias.asname or alias.name)
    return names


def _calls_a_factory(tree: ast.AST, local_names: set[str]) -> list[int]:
    """Line numbers of calls to a forbidden factory, whether reached
    through an imported name (``create_engine(...)``, possibly aliased)
    or through an attribute (``sa.create_engine(...)``)."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id in local_names) or (
            isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_FACTORIES
        ):
            hits.append(node.lineno)
    return hits


def _python_files() -> list[Path]:
    files: list[Path] = []
    for rel in _SCAN_ROOTS:
        root = _REPO_ROOT / rel
        if not root.exists():
            continue
        files.extend(sorted(root.rglob("*.py")))
    return files


def test_only_the_factory_module_builds_engines() -> None:
    """One factory, one place to wire the codec.

    A second ``create_async_engine`` call anywhere is a second engine
    whose JSONB binds fall back to stdlib ``json.dumps`` — i.e. the
    500 comes back on whatever writes through it. Route new engines
    through :func:`bvphoenix.db.engine.make_async_engine` /
    :func:`make_sync_engine` instead of adding an entry to
    ``_ALLOWED``.
    """
    scanned = _python_files()
    assert scanned, f"no Python sources found under {_SCAN_ROOTS} — check _REPO_ROOT"

    violations: list[str] = []
    for path in scanned:
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno in _calls_a_factory(tree, _local_names_bound_to_factories(tree)):
            violations.append(f"{rel}:{lineno}")

    assert not violations, (
        "SQLAlchemy engines must come from bvphoenix.db.engine "
        "(json_serializer wiring); direct factory calls at: " + ", ".join(violations)
    )


def test_alembic_env_wires_the_same_codec() -> None:
    """``alembic/env.py`` is allowed its own ``async_engine_from_config``
    because it reads the alembic ini section, but a migration that
    backfills a JSONB column binds through the same path as the app and
    must not choke on a ``date``."""
    env = _REPO_ROOT / "backend/alembic/env.py"
    assert env.exists(), "backend/alembic/env.py is missing"
    source = env.read_text(encoding="utf-8")
    assert "json_serializer" in source, (
        "alembic/env.py builds its own engine and must pass json_serializer "
        "(bvphoenix.db.engine.dumps), or a JSONB-writing migration crashes on a date"
    )
    assert "json_deserializer" in source

    tree = ast.parse(source, filename=str(env))
    local = _local_names_bound_to_factories(tree)
    assert _calls_a_factory(tree, local), (
        "expected alembic/env.py to build the engine itself; if it now goes "
        "through bvphoenix.db.engine, drop it from _ALLOWED so the guard stays tight"
    )
