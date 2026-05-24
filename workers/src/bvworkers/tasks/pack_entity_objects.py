"""F12.6 pack worker: convert long chains of full entity_objects into
delta-encoded ones, mirroring git's loose→pack lifecycle.

Initial writes always store ``storage_kind='full'`` to keep the write
path latency-free. This worker runs offline (cron, post-close-of-day)
to recompress the long chains that have built up. It iterates over
``(entity_kind, entity_id)`` pairs that have many versions and converts
the non-snapshot rows to ``storage_kind='delta'`` when the delta is at
least 50% smaller than the full payload.

Idempotent: a re-run on already-packed objects skips them. The pack
worker never touches:

  * tombstoned rows (they have no payload);
  * the ``_tree_`` blobs (the manifest serialisation isn't worth
    compressing relative to its parent);
  * rows that are already delta;
  * ``patient`` / ``study`` / ``series`` entities that change rarely
    (they have few versions, the savings don't justify the read cost).

The default policy compresses ``clinical_note``, ``report``,
``annotation``, ``consultation``, ``summary`` (the high-write-rate
entities). Tunable via the ``KINDS_TO_PACK`` constant.
"""

from __future__ import annotations

import uuid
from typing import Any

from arq.connections import ArqRedis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from bvworkers.config import get_settings


KINDS_TO_PACK: frozenset[str] = frozenset(
    {"clinical_note", "report", "annotation", "consultation", "summary"}
)


async def pack_entity_objects_task(
    ctx: dict[str, Any],
    *,
    snapshot_every: int = 10,
    delta_threshold: float = 0.5,
    max_entities_per_run: int = 5000,
) -> dict[str, Any]:
    """Pack chains of full entity_objects into delta-encoded form.

    Returns a summary dict with per-kind conversion counts.
    """
    # Lazy-import the service module so this worker file stays
    # importable even if bvphoenix isn't on the path (e.g. when arq's
    # task discovery scans names without resolving deps).
    from bvphoenix.services.versioning import pack_entity_objects

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    converted_per_kind: dict[str, int] = {}
    examined = 0

    try:
        async with factory() as db:
            # Bypass RLS: this is a maintenance worker, not a user request.
            await db.execute(
                text("SELECT set_config('app.current_subject_id', 'service', true)")
            )

            # Find candidate (kind, id) pairs: those with at least
            # ``snapshot_every`` versions in entity_objects via the
            # manifest. Limit to one batch per run.
            rows = (
                await db.execute(
                    text(
                        "SELECT me.entity_kind, me.entity_id, count(*) AS n "
                        "FROM manifest_entries me "
                        "JOIN entity_objects eo ON eo.object_hash = me.object_hash "
                        "WHERE me.entity_kind = ANY(:kinds) "
                        "  AND eo.storage_kind = 'full' "
                        "  AND eo.is_tombstoned = false "
                        "GROUP BY me.entity_kind, me.entity_id "
                        "HAVING count(*) >= :min "
                        "ORDER BY count(*) DESC "
                        "LIMIT :lim"
                    ),
                    {
                        "kinds": list(KINDS_TO_PACK),
                        "min": snapshot_every,
                        "lim": max_entities_per_run,
                    },
                )
            ).all()

            for kind, eid, _n in rows:
                examined += 1
                converted = await pack_entity_objects(
                    db,
                    entity_kind=kind,
                    entity_id=uuid.UUID(str(eid)) if not isinstance(eid, uuid.UUID) else eid,
                    snapshot_every=snapshot_every,
                    delta_threshold=delta_threshold,
                )
                if converted:
                    converted_per_kind[kind] = converted_per_kind.get(kind, 0) + converted
            await db.commit()
    finally:
        await engine.dispose()

    return {
        "status": "packed",
        "examined": examined,
        "converted_per_kind": converted_per_kind,
        "total_converted": sum(converted_per_kind.values()),
    }


# Optional: enqueue helper for callers (CLI / scheduled cron).
async def enqueue_pack(redis: ArqRedis) -> None:
    await redis.enqueue_job("pack_entity_objects_task")
