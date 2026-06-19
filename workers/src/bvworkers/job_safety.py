"""Raw-SQL job state updates as a last-resort safety net.

The arq tasks defined in ``bvworkers.tasks.*`` typically import
``bvphoenix.services.jobs`` to drive Job state transitions through
the SQLAlchemy ORM. That works as long as bvphoenix imports cleanly
inside the worker process — but if anything in that import chain
raises (missing module, broken settings, partial code deploy), the
task crashes BEFORE it can call ``mark_running`` or ``mark_failed``.
The Job row then sits in ``queued`` forever, the UI shows it as
"in progress", and the user has no way to know the worker has given
up.

This module talks straight to PostgreSQL via ``asyncpg`` so it can
flip such jobs to ``failed`` even when the bvphoenix codebase is
unreachable. Wrap your task body in
:func:`with_safety_net` to opt in:

.. code-block:: python

    @with_safety_net("my_task")
    async def my_task(ctx, job_id, ...):
        ...

The decorator is idempotent against a successful task that already
called ``mark_succeeded`` — the raw UPDATE is gated on
``status IN ('queued','running')`` so a terminal row is left alone.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import ssl as ssl_mod
import traceback
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


def _to_asyncpg_dsn(sqlalchemy_url: str) -> tuple[str, dict]:
    """Convert a SQLAlchemy DSN to a vanilla asyncpg one.

    Strips the ``+asyncpg`` / ``+psycopg`` driver suffix and the
    ``ssl=...`` / ``sslmode=...`` query params. Returns ``(dsn,
    kwargs)`` where ``kwargs`` carries the SSL config asyncpg wants
    as a separate argument.
    """
    url = sqlalchemy_url
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    url = url.replace("postgresql+psycopg://", "postgresql://")

    kw: dict[str, Any] = {}
    # Scaleway Managed Redis / PG ships with a self-signed cert; the
    # legacy SQLAlchemy URL says ``ssl=require`` (asyncpg) or
    # ``sslmode=require`` (psycopg). Translate to asyncpg's ``ssl``
    # kwarg with cert verification disabled, mirroring what the
    # backend already does in its own engine config.
    if re.search(r"\b(ssl|sslmode)=require\b", url):
        ctx = ssl_mod.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl_mod.CERT_NONE
        kw["ssl"] = ctx
    url = re.sub(r"\?.*$", "", url)
    return url, kw


async def mark_job_failed_raw(
    job_id: str,
    *,
    code: str,
    message: str,
    db_url_env: str = "BVP_DATABASE_URL_SYNC",
) -> bool:
    """Best-effort UPDATE of the jobs row to ``status=failed``.

    Returns True if the row was actually updated, False on any
    failure (missing env, bad UUID, no matching row, raw connect
    refused). Never raises — the caller is already in a degraded
    code path and we don't want to mask the original error.
    """
    try:
        jid = uuid.UUID(job_id)
    except (TypeError, ValueError):
        log.error("mark_job_failed_raw: invalid job_id %r", job_id)
        return False

    dsn_raw = os.environ.get(db_url_env)
    if not dsn_raw:
        log.error("mark_job_failed_raw: %s not set", db_url_env)
        return False

    try:
        dsn, kw = _to_asyncpg_dsn(dsn_raw)
        conn = await asyncpg.connect(dsn, **kw)
    except Exception as exc:
        log.error("mark_job_failed_raw: cannot connect: %s", exc)
        return False

    try:
        result = await conn.execute(
            """
            UPDATE jobs
            SET status = 'failed',
                error = $1::jsonb,
                finished_at = now(),
                updated_at = now()
            WHERE id = $2
              AND status IN ('queued', 'running')
            """,
            json.dumps({"code": code, "message": message}),
            jid,
        )
        # asyncpg returns "UPDATE <n>"
        updated = result.startswith("UPDATE ") and not result.endswith(" 0")
        if updated:
            log.warning("mark_job_failed_raw: flipped job %s to failed (%s)", jid, code)
        return updated
    except Exception as exc:
        log.error("mark_job_failed_raw: UPDATE failed: %s", exc)
        return False
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await conn.close()


async def mark_registration_failed_raw(
    registration_id: str,
    *,
    code: str,
    message: str,
    db_url_env: str = "BVP_DATABASE_URL_SYNC",
) -> bool:
    """Best-effort raw UPDATE of a ``registrations`` row (and its linked
    ``jobs`` row) to ``failed``.

    The follow-up viewer polls the REGISTRATION status, not the Job — so a
    crash that escapes the task body must flip the registration, otherwise
    the UI hangs until its own poll timeout ("L'allineamento sta impiegando
    troppo"). Gated on ``status IN ('queued','running')`` so a terminal row
    is left alone. Never raises.
    """
    try:
        rid = uuid.UUID(registration_id)
    except (TypeError, ValueError):
        log.error("mark_registration_failed_raw: invalid id %r", registration_id)
        return False

    dsn_raw = os.environ.get(db_url_env)
    if not dsn_raw:
        log.error("mark_registration_failed_raw: %s not set", db_url_env)
        return False

    try:
        dsn, kw = _to_asyncpg_dsn(dsn_raw)
        conn = await asyncpg.connect(dsn, **kw)
    except Exception as exc:
        log.error("mark_registration_failed_raw: cannot connect: %s", exc)
        return False

    try:
        # registrations.error is TEXT; jobs.error is JSONB.
        row = await conn.fetchrow(
            """
            UPDATE registrations
            SET status = 'failed', error = $1, finished_at = now()
            WHERE id = $2 AND status IN ('queued', 'running')
            RETURNING job_id
            """,
            f"{code}: {message}",
            rid,
        )
        if row is None:
            return False
        job_id = row["job_id"]
        if job_id is not None:
            await conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', error = $1::jsonb,
                    finished_at = now(), updated_at = now()
                WHERE id = $2 AND status IN ('queued', 'running')
                """,
                json.dumps({"code": code, "message": message}),
                job_id,
            )
        log.warning(
            "mark_registration_failed_raw: flipped registration %s to failed (%s)", rid, code
        )
        return True
    except Exception as exc:
        log.error("mark_registration_failed_raw: UPDATE failed: %s", exc)
        return False
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await conn.close()


def with_safety_net(
    task_name: str,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator: wrap an arq task so any unhandled exception flips
    the Job row to ``failed`` even if the task body crashed before
    it could call the bvphoenix-side ``mark_failed``.

    Convention: the wrapped task takes ``(ctx, job_id, *args)`` —
    same shape as ``ingest_bulk_files`` / ``export_patient_zip`` /
    etc. The decorator extracts ``job_id`` and uses
    :func:`mark_job_failed_raw` as the outer guard.
    """

    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(ctx: dict, job_id: str, *args, **kwargs):  # type: ignore[type-arg]
            try:
                return await fn(ctx, job_id, *args, **kwargs)
            except BaseException as exc:
                # Anything that escapes the task body lands here. Flip
                # the row to failed (no-op if the task already marked
                # it terminal). Re-raise so arq still sees the
                # exception in its own logs / retry semantics.
                tb = traceback.format_exc()
                log.exception(
                    "%s: unhandled exception in job %s — flipping row to failed",
                    task_name,
                    job_id,
                )
                await mark_job_failed_raw(
                    job_id,
                    code=f"{task_name}_unhandled",
                    message=f"{type(exc).__name__}: {exc}\n{tb[-500:]}",
                )
                raise

        return wrapper

    return deco


def with_registration_safety_net(
    task_name: str,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Like :func:`with_safety_net`, but the task's second positional arg is a
    REGISTRATION id (not a Job id) and the follow-up UI polls the registration
    row. An unhandled crash flips the registration (and its linked job) to
    ``failed`` via :func:`mark_registration_failed_raw`, so the viewer sees a
    terminal error instead of hanging.
    """

    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(ctx: dict, registration_id: str, *args, **kwargs):  # type: ignore[type-arg]
            try:
                return await fn(ctx, registration_id, *args, **kwargs)
            except BaseException as exc:
                tb = traceback.format_exc()
                log.exception(
                    "%s: unhandled exception in registration %s — flipping row to failed",
                    task_name,
                    registration_id,
                )
                await mark_registration_failed_raw(
                    registration_id,
                    code=f"{task_name}_unhandled",
                    message=f"{type(exc).__name__}: {exc}\n{tb[-500:]}",
                )
                raise

        return wrapper

    return deco
