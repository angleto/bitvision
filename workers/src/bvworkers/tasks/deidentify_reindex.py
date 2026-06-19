"""Re-build derivatives + scrub raw DICOM after a tier change (F6.3).

Context: ``PATCH /api/studies/{id}/tier`` and the initial T3 / T4
upload enqueue this task. Three phases:

1. **Scrub raw DICOM** — for every ``Instance.s3_key`` of a T3 / T4
   study, download the blob, pass it through
   :func:`bvphoenix.services.deidentify.deidentify_dicom_bytes`, and
   re-upload to the same key. Makes the raw bucket safe to enumerate
   under the looser sharing posture of the commons tiers. Idempotent
   (running the scrub on already-scrubbed bytes is a no-op).
2. **Invalidate derivatives** — delete ``derivatives`` rows and their
   S3 blobs for every series of the study. The next request
   re-packs on demand (or via the pack_volume re-enqueue below).
3. **Re-enqueue pack_volume** for each series so the volume cache
   rebuilds in the background and the viewer is ready on first open.

The task is **idempotent** via the per-study DB stamp: it skips the scrub
phase when ``imaging_studies.deid_method_version`` already equals the current
engine version (a re-run leaves materialised bytes untouched). The engine
itself is NOT byte-idempotent (its UID remap + date shift are value-based), and
it deliberately does NOT trust the file's own ``PatientIdentityRemoved`` tag —
that signal is attacker-forgeable; only the DB stamp is authoritative. On
success the task writes the stamp, which the download path
(``api/studies/core.py``) then uses to serve the stored scrubbed bytes without
a per-download re-scrub.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import boto3
from botocore.client import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bvworkers.config import get_settings

log = logging.getLogger(__name__)


_COMMONS_TIERS = frozenset({"t3", "t4"})


def _scrub_bytes(raw: bytes) -> bytes:
    """Wrap the backend's DICOM de-identifier.

    Imported lazily so a worker image built without the ``bvphoenix``
    package still starts (it just cannot scrub, and the read path
    remains authoritative). Anything other than a successful scrub
    surfaces as an exception the caller catches per-instance.
    """
    from bvphoenix.services.deidentify import deidentify_dicom_bytes

    return deidentify_dicom_bytes(raw)


async def _scrub_instance(s3: object, bucket: str, key: str) -> str:
    """Download, scrub, and re-upload one raw DICOM blob.

    Returns ``"scrubbed"``, ``"unchanged"`` (deid reduced the payload
    to the same bytes — idempotent second run), or ``"error"`` so the
    task summary can count outcomes per class.

    ``s3`` is typed ``object`` because ``boto3.client("s3")`` is a
    runtime-generated type; the methods we touch (``get_object``,
    ``put_object``) are stable enough that an untyped view is the
    least-bad option.
    """
    try:
        obj = await asyncio.to_thread(s3.get_object, Bucket=bucket, Key=key)  # type: ignore[attr-defined]
        src = obj["Body"].read()
    except Exception as exc:
        log.warning(
            "deidentify_reindex: failed to download s3://%s/%s: %s",
            bucket,
            key,
            exc,
        )
        return "error"

    try:
        scrubbed = await asyncio.to_thread(_scrub_bytes, src)
    except Exception as exc:
        log.warning(
            "deidentify_reindex: failed to de-identify s3://%s/%s: %s",
            bucket,
            key,
            exc,
        )
        return "error"

    if scrubbed == src:
        # Already clean — common on replays. Skip the PUT to save a
        # round-trip and (on versioned buckets) an extra version.
        return "unchanged"

    try:
        await asyncio.to_thread(
            s3.put_object,  # type: ignore[attr-defined]
            Bucket=bucket,
            Key=key,
            Body=scrubbed,
            ContentType="application/dicom",
        )
    except Exception as exc:
        log.warning(
            "deidentify_reindex: failed to upload s3://%s/%s: %s",
            bucket,
            key,
            exc,
        )
        return "error"

    return "scrubbed"


async def deidentify_reindex_study(ctx: dict, study_id: str) -> dict:  # type: ignore[type-arg]
    """Scrub raw DICOM, invalidate cached derivatives, and re-enqueue
    the volume pack for every series of ``study_id``."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    sid = uuid.UUID(study_id)
    series_rebuilt: list[str] = []
    derivatives_deleted = 0
    scrub_summary = {"scrubbed": 0, "unchanged": 0, "error": 0}

    async with AsyncSession(engine) as db:
        study_row = (
            await db.execute(
                text(
                    "SELECT id, contribution_tier, deid_method_version FROM studies WHERE id = :sid"
                ),
                {"sid": sid},
            )
        ).first()
        if study_row is None:
            await engine.dispose()
            return {"status": "not_found", "study_id": study_id}

        tier = study_row[1]
        already_deid_version = study_row[2]

        # DB-driven idempotency (the unforgeable "already scrubbed" signal): a
        # study already scrubbed at the CURRENT engine version is not re-scrubbed.
        # The engine's UID remap + date shift are value-based, not byte-idempotent,
        # so a second pass would re-mutate the materialised bytes (drifting UIDs,
        # double-shifting dates). A version mismatch (engine upgraded) DOES
        # re-scrub. We deliberately do NOT trust the file's own
        # PatientIdentityRemoved tag for this — only the DB stamp.
        from bvphoenix.config import get_settings as _bvp_get_settings

        current_version = _bvp_get_settings().deid_method_version

        # Phase 1: scrub raw DICOM for commons tiers. Private / shared-
        # controlled studies stay as uploaded because the user's own
        # tenant is still the only audience; only T3 / T4 land in a
        # sharing posture that assumes PHI has been removed from disk.
        if tier in _COMMONS_TIERS and already_deid_version != current_version:
            instance_rows = (
                await db.execute(
                    text(
                        "SELECT i.s3_bucket, i.s3_key "
                        "FROM instances i "
                        "JOIN series s ON s.id = i.series_id "
                        "WHERE s.study_id = :sid"
                    ),
                    {"sid": sid},
                )
            ).all()

            for bucket, key in instance_rows:
                outcome = await _scrub_instance(s3, bucket, key)
                scrub_summary[outcome] += 1

        # Phase 2: derivatives. The blobs go first so a row without a
        # corresponding S3 object never survives the transaction.
        deriv_rows = (
            await db.execute(
                text(
                    "SELECT d.id, d.s3_bucket, d.s3_key "
                    "FROM derivatives d "
                    "JOIN series s ON s.id = d.series_id "
                    "WHERE s.study_id = :sid"
                ),
                {"sid": sid},
            )
        ).all()

        for _, bucket, key in deriv_rows:
            try:
                await asyncio.to_thread(s3.delete_object, Bucket=bucket, Key=key)
            except Exception as exc:
                log.warning(
                    "deidentify_reindex: failed to delete s3://%s/%s: %s",
                    bucket,
                    key,
                    exc,
                )

        if deriv_rows:
            await db.execute(
                text(
                    "DELETE FROM derivatives "
                    "WHERE series_id IN (SELECT id FROM series WHERE study_id = :sid)"
                ),
                {"sid": sid},
            )
            derivatives_deleted = len(deriv_rows)

        # Phase 3 prep: series ids for pack_volume re-enqueue.
        series_rows = (
            await db.execute(
                text("SELECT id FROM series WHERE study_id = :sid"),
                {"sid": sid},
            )
        ).all()
        series_rebuilt = [str(r[0]) for r in series_rows]

        # Stamp the study as de-identified at rest after a clean commons-tier
        # scrub, so the egress download path (api/studies/core.py) can serve the
        # stored scrubbed bytes directly instead of re-scrubbing on every read.
        # The version must equal what the engine wrote into the bytes, so read
        # it from the backend engine config (lazy import: the worker image may
        # ship without bvphoenix, in which case the scrub already failed above).
        if tier in _COMMONS_TIERS and scrub_summary["error"] == 0:
            await db.execute(
                text(
                    "UPDATE imaging_studies SET deidentified_at = now(), "
                    "deid_method_version = :v WHERE id = :sid"
                ),
                {"v": current_version, "sid": sid},
            )

        await db.commit()

    await engine.dispose()

    # Phase 3: fresh pack per series. Reuse the arq pool the worker
    # was started with when available; the ``None`` branch is taken
    # by unit tests that invoke the task outside a real loop.
    enqueued = 0
    redis_pool = ctx.get("redis") if ctx else None
    if redis_pool is not None:
        for sid_s in series_rebuilt:
            try:
                await redis_pool.enqueue_job("pack_volume", sid_s)
                enqueued += 1
            except Exception as exc:
                log.warning(
                    "deidentify_reindex: failed to enqueue pack_volume for %s: %s",
                    sid_s,
                    exc,
                )

    return {
        "status": "reindexed",
        "study_id": study_id,
        "tier": tier,
        "instances_scrubbed": scrub_summary["scrubbed"],
        "instances_unchanged": scrub_summary["unchanged"],
        "instances_errored": scrub_summary["error"],
        "derivatives_deleted": derivatives_deleted,
        "series_rebuilt": len(series_rebuilt),
        "pack_volume_enqueued": enqueued,
    }
