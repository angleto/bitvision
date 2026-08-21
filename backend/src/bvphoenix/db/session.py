"""Async SQLAlchemy engine and session factory.

The session also carries the **RLS principal context**: every FastAPI
request sets ``app.current_subject_id`` once the user is resolved (see
``bvphoenix.auth.deps.optional_user``), and background / CLI contexts
set it to the sentinel ``'service'`` to take the service-bypass branch
of the RLS policies installed by migration ``0009_rls_policies``.

Surgical rule: never query a user-scoped table before the variable has
been set — otherwise RLS correctly returns zero rows. ``get_db``
initialises the variable to ``'anonymous'`` on every request so no
caller can forget to.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bvphoenix.config import get_settings
from bvphoenix.db.engine import make_async_engine

_settings = get_settings()

# Pool sizing
# -----------
# Defaults: pool_size=5, max_overflow=10 (15 connections per process).
# Each backend pod runs uvicorn with 1 worker by default, so a 2-pod
# deployment can hold up to 30 concurrent DB connections. Combined
# with the worker pool (replicas=2) and the migration job, the
# managed Postgres ``max_connections`` budget (~200 on the
# Scaleway DB-PLAY2-NANO tier) is well within reach. Bumping the pool
# any further is therefore wasted — we hit other bottlenecks (asyncpg
# event loop, structlog serialisation) before saturating the
# connection cap.
#
# ``pool_recycle=3600`` evicts idle connections after an hour, which
# matches the managed Postgres backend's ``tcp_keepalives_idle`` so a
# server-side keepalive collapse can't surface as a stale-connection
# 500 on the first request after an off-peak quiet hour.
#
# ``connect_args.statement_cache_size`` raises asyncpg's prepared-
# statement cache from the 100 default to 250 — the API surface emits
# ~150 distinct queries (search facets, audit aggregates, owner-
# scoped UID variants) so the 100-slot LRU was thrashing on the hot
# path. 250 is wide enough that cache turnover under typical traffic
# stays at noise level (verified on staging).
# ``make_async_engine`` (not ``create_async_engine``) so the JSON/JSONB
# bind codecs are wired: see ``bvphoenix.db.engine``. Building an engine
# anywhere else is a test failure (tests/test_db_engine_factory.py).
engine = make_async_engine(
    _settings.database_url,
    echo=_settings.env == "development",
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    pool_recycle=3600,
    pool_timeout=30,
    connect_args={"statement_cache_size": 250},
)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


# Sentinel values recognised by the ``app_current_subject()`` SQL
# helper. Exported as module-level constants so callers don't have to
# repeat the string literals.
ANONYMOUS_SUBJECT = "anonymous"
SERVICE_SUBJECT = "service"


async def set_current_subject(session: AsyncSession, subject_id: str | None) -> None:
    """Bind ``app.current_subject_id`` to this session's transaction.

    Pass a UUID string for authenticated users, ``SERVICE_SUBJECT`` for
    worker / CLI contexts that should bypass RLS, or ``None`` /
    ``ANONYMOUS_SUBJECT`` for unauthenticated traffic (public-only
    visibility). Uses ``set_config(..., is_local := true)`` so the
    binding is scoped to the current transaction and can't leak across
    pooled connections.
    """
    value = subject_id or ANONYMOUS_SUBJECT
    await session.execute(
        text("SELECT set_config('app.current_subject_id', :v, true)"),
        {"v": value},
    )


@asynccontextmanager
async def get_session(
    *,
    subject_id: str | None = ANONYMOUS_SUBJECT,
) -> AsyncIterator[AsyncSession]:
    """Yield a database session; commit on success, rollback on failure.

    ``subject_id`` binds ``app.current_subject_id`` — pass
    ``SERVICE_SUBJECT`` for worker / CLI / migration contexts that need
    to bypass RLS, or a UUID string to act as that user. Defaults to
    anonymous so accidental misuse errs on the side of returning fewer
    rows rather than more.
    """
    session = SessionFactory()
    try:
        await set_current_subject(session, subject_id)
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Caller is responsible for explicit commits;
    we still rollback on exception so a failed handler never leaves
    half-applied state.

    Initialises ``app.current_subject_id = 'anonymous'`` so any query
    that runs *before* ``optional_user`` / ``require_user`` resolves
    the caller is safely restricted to public rows. The auth deps
    overwrite this with the real subject id once the token is decoded.
    """
    session = SessionFactory()
    try:
        await set_current_subject(session, ANONYMOUS_SUBJECT)
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
