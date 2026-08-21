"""Arq task: generate a cascading LLM summary for a target.

Wraps :func:`bvphoenix.services.summarizer.summarize_series` /
``summarize_study`` / ``summarize_patient`` from the backend so the HTTP
layer can enqueue expensive study / patient summaries without blocking
the request. The task opens its own database session (the worker does
not share the backend's FastAPI engine) bound to the ``service``
principal so RLS stays out of the way.

Fallback: if the backend package is not importable — e.g. the worker
image was built without ``bvphoenix`` on PYTHONPATH — we log a warning
and degrade to a minimal SQL-only stub that records a placeholder
summary row. This keeps queue consumers healthy in unusual deployments;
production installs are expected to ship both packages together so the
real cascade runs.
"""

from __future__ import annotations

import logging
import uuid

from bvphoenix.db.engine import make_async_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings

log = logging.getLogger(__name__)

VALID_KINDS = frozenset({"series", "study", "patient"})


async def generate_summary(
    ctx: dict,  # type: ignore[type-arg]
    target_kind: str,
    target_id: str,
    lang: str = "en",
    force_refresh: bool = False,
    user_subject_id: str | None = None,
) -> dict:
    """Arq entry point.

    Args:
        ctx: arq context.
        target_kind: 'series' | 'study' | 'patient'.
        target_id: UUID string of the target row.
        lang: ISO 639-1 language code. Defaults to English.
        force_refresh: when true, skip the cache and regenerate.
        user_subject_id: wallet to bill for the LLM call. ``None`` is
            accepted for backward compatibility with jobs enqueued
            before the billing hook landed — those skip the ledger.
    """
    if target_kind not in VALID_KINDS:
        return {"status": "error", "reason": f"invalid target_kind: {target_kind}"}

    try:
        tid = uuid.UUID(target_id)
    except (TypeError, ValueError):
        return {"status": "error", "reason": f"invalid target_id: {target_id!r}"}

    billing_subject: uuid.UUID | None = None
    if user_subject_id is not None:
        try:
            billing_subject = uuid.UUID(user_subject_id)
        except (TypeError, ValueError):
            # A bad id would silently disable billing, so surface it.
            return {
                "status": "error",
                "reason": f"invalid user_subject_id: {user_subject_id!r}",
            }

    settings = get_settings()

    try:
        # Preferred path: delegate to the backend service so the hash /
        # prompt / provider logic stays in a single place.
        from bvphoenix.db.session import SERVICE_SUBJECT, set_current_subject
        from bvphoenix.services.summarizer import (
            summarize_patient,
            summarize_series,
            summarize_study,
        )
    except ImportError as exc:  # pragma: no cover — only hit on slim images
        log.warning(
            "bvphoenix package not importable from worker (%s); "
            "falling back to placeholder summary row",
            exc,
        )
        return await _fallback_generate(
            settings.database_url,
            target_kind=target_kind,
            target_id=tid,
            lang=lang,
        )

    engine = make_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            # Worker contexts bypass RLS — the enqueuing backend has
            # already authorised the caller, and RLS service sentinel
            # lets the cascade read every row it needs.
            await set_current_subject(db, SERVICE_SUBJECT)

            if target_kind == "series":
                row = await summarize_series(
                    db,
                    tid,
                    lang,
                    force_refresh=force_refresh,
                    user_subject_id=billing_subject,
                )
            elif target_kind == "study":
                row = await summarize_study(
                    db,
                    tid,
                    lang,
                    force_refresh=force_refresh,
                    user_subject_id=billing_subject,
                )
            else:
                row = await summarize_patient(
                    db,
                    tid,
                    lang,
                    force_refresh=force_refresh,
                    user_subject_id=billing_subject,
                )

            return {
                "status": "ok",
                "summary_id": str(row.id),
                "target_kind": target_kind,
                "target_id": str(tid),
                "lang": lang,
                "model_id": row.model_id,
                "source_version_hash": row.source_version_hash,
            }
    except LookupError as exc:
        return {"status": "not_found", "reason": str(exc)}
    finally:
        await engine.dispose()


async def _fallback_generate(
    database_url: str,
    *,
    target_kind: str,
    target_id: uuid.UUID,
    lang: str,
) -> dict:
    """Minimal SQL-only stub for environments that don't ship bvphoenix.

    Inserts a placeholder row using a timestamp-based hash so retries
    still converge and don't stampede the table. The text is marked
    explicitly as degraded so downstream UIs can surface the failure
    mode instead of displaying a bogus clinical digest.
    """
    import hashlib
    from datetime import UTC, datetime

    engine = make_async_engine(database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            now = datetime.now(UTC).isoformat()
            h = hashlib.sha256(
                f"{target_kind}|{target_id}|{lang}|fallback|{now}".encode()
            ).hexdigest()
            await db.execute(
                text(
                    "INSERT INTO summaries "
                    "(target_kind, target_id, lang, text, model_id, provider, "
                    " source_version_hash, updated_at) "
                    "VALUES (:kind, :tid, :lang, :text, :mid, :prov, :hash, NOW()) "
                    "ON CONFLICT (target_kind, target_id, lang, "
                    " source_version_hash) DO NOTHING"
                ),
                {
                    "kind": target_kind,
                    "tid": target_id,
                    "lang": lang,
                    "text": (
                        "[fallback] summary service unavailable on worker. "
                        "Please retry from the backend."
                    ),
                    "mid": "fallback-stub",
                    "prov": "fallback",
                    "hash": h,
                },
            )
            await db.commit()
            return {
                "status": "degraded",
                "target_kind": target_kind,
                "target_id": str(target_id),
                "lang": lang,
            }
    finally:
        await engine.dispose()
