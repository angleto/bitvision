"""``bvphoenix-tile-pathology`` — backfill DZI tile pyramids.

Enqueues the ``tile_wsi`` worker for pathology slides that have no
deep-zoom pyramid yet (``dzi_ready = false``). Use it to tile slides
ingested before the viewer shipped, or to retry failures.

Idempotent end-to-end: the worker self-skips on ``dzi_ready`` and bounds
its own per-pod concurrency, so a ``--all`` over hundreds of slides
drains serially without a stampede.

    bvphoenix-tile-pathology --all
    bvphoenix-tile-pathology --slide-id <uuid> --slide-id <uuid>
    bvphoenix-tile-pathology --all --include-failed   # also retry dzi_error rows
    bvphoenix-tile-pathology --all --dry-run          # list candidates only
"""

from __future__ import annotations

import sys
import uuid

import click
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings


@click.command(
    name="bvphoenix-tile-pathology",
    help="Backfill DeepZoom tile pyramids for pathology slides (enqueues tile_wsi).",
)
@click.option("--all", "all_pending", is_flag=True, help="Tile every slide with dzi_ready=false.")
@click.option(
    "--slide-id",
    "slide_ids",
    type=click.UUID,
    multiple=True,
    help="Explicit slide UUID(s) to (re)tile. Repeatable.",
)
@click.option(
    "--include-failed",
    is_flag=True,
    help="With --all, also re-queue slides whose last tiling failed (dzi_error set).",
)
@click.option("--dry-run", is_flag=True, help="List candidate slide ids without enqueueing.")
def main(
    all_pending: bool,
    slide_ids: tuple[uuid.UUID, ...],
    include_failed: bool,
    dry_run: bool,
) -> None:
    if not all_pending and not slide_ids:
        raise click.ClickException("pass --all or one or more --slide-id")

    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)

    with Session(engine) as session:
        if slide_ids:
            rows = session.execute(
                text(
                    "SELECT id::text FROM pathology_slides "
                    "WHERE id = ANY(:ids) AND dzi_ready = false"
                ),
                {"ids": [str(s) for s in slide_ids]},
            ).all()
        else:
            # --all: pending slides; skip already-failed ones unless asked.
            where_failed = "" if include_failed else "AND dzi_error IS NULL"
            rows = session.execute(
                text(
                    "SELECT id::text FROM pathology_slides "
                    f"WHERE dzi_ready = false {where_failed} ORDER BY created_at ASC"
                )
            ).all()

    candidates = [r[0] for r in rows]
    if not candidates:
        click.echo("no slides pending tiling.")
        return

    click.echo(f"{len(candidates)} slide(s) pending tiling")
    if dry_run:
        for sid in candidates:
            click.echo(f"  - {sid}")
        click.echo("(dry-run: nothing enqueued)")
        return

    try:
        import asyncio

        from arq import create_pool

        from bvphoenix.services.arq_redis import redis_settings
        from bvphoenix.services.pathology_jobs import enqueue_tile_jobs

        async def _enqueue() -> int:
            redis = await create_pool(redis_settings(settings.redis_url))
            try:
                return await enqueue_tile_jobs(redis, candidates)
            finally:
                await redis.close()

        n = asyncio.run(_enqueue())
        click.echo(f"enqueued {n} DZI tiling job(s)")
    except Exception as exc:
        click.echo(f"error: could not enqueue tiling jobs: {exc}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
