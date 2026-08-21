"""``bvphoenix-reembed`` — rollback-safe embedding model swap orchestrator.

Unit E5. The overall flow:

    $ bvphoenix-reembed start \\
          --target-kind series \\
          --to-model biomedclip-v2 \\
          [--from-model biomedclip-v1] \\
          --batch 100 [--dry-run]

    # watch it
    $ bvphoenix-reembed status <job-id>

    # pause in-flight (batches drain, new ones don't start)
    $ bvphoenix-reembed cancel <job-id>

    # delete the new vectors we just wrote if the new model was bad
    $ bvphoenix-reembed rollback <job-id>

Key properties:

* Old and new embeddings coexist: the job only INSERTs into ``embeddings``
  under ``to_model_id``, never touches rows under ``from_model_id``.
* The default model for each ``target_kind`` is *not* flipped here — the
  admin does that out-of-band with ``bvphoenix-embed-models activate``
  (unit E4). Until that flip, search keeps serving the old vectors.
* Resume-safe: each batch commits per-target progress; a crashed CLI
  can just re-run ``start`` with the same job id (not implemented —
  we prefer explicit resume: the CLI records the job row, then any
  operator can re-enqueue the tail batches with the same offsets).
* Rollback is a narrow, bounded ``DELETE`` of rows written after
  ``started_at`` under ``to_model_id``. The job is frozen to
  ``rolled_back`` before the delete so concurrent batches halt.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime

import click
from arq import create_pool
from sqlalchemy import text
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.engine import make_sync_engine
from bvphoenix.services.arq_redis import redis_settings

VALID_TARGET_KINDS = ("study", "series", "instance")

# Per-target embedding cost is dominated by model inference + S3 I/O.
# This is a crude estimate surfaced in dry-run only — it intentionally
# errs high so operators plan for the worst case.
EST_SECONDS_PER_TARGET = 2.0


@click.group()
def main() -> None:
    """Rollback-safe embedding model swap orchestrator."""


def _engine():
    return make_sync_engine(get_settings().database_url_sync)


def _count_candidates(
    session: Session,
    target_kind: str,
    from_model_id: str | None,
    to_model_id: str,
) -> int:
    """Mirror of the worker's candidate selection, minus OFFSET/LIMIT.

    Kept textually close to the worker query so divergence bugs surface
    loudly (e.g. fresh-embed targets that actually already have a vector
    would show up as extra rows here too).
    """
    table = {"study": "studies", "series": "series", "instance": "instances"}[target_kind]
    if from_model_id is None:
        sql = text(
            f"""
            SELECT COUNT(*) FROM {table} t
            WHERE NOT EXISTS (
                SELECT 1 FROM embeddings e
                WHERE e.target_kind = :kind
                  AND e.target_id = t.id
                  AND e.model_id = :to_model
            )
            """
        )
        return int(
            session.execute(sql, {"kind": target_kind, "to_model": to_model_id}).scalar_one()
        )
    sql = text(
        """
        SELECT COUNT(*) FROM embeddings e_from
        WHERE e_from.target_kind = :kind
          AND e_from.model_id = :from_model
          AND NOT EXISTS (
              SELECT 1 FROM embeddings e_to
              WHERE e_to.target_kind = :kind
                AND e_to.target_id = e_from.target_id
                AND e_to.model_id = :to_model
          )
        """
    )
    return int(
        session.execute(
            sql,
            {
                "kind": target_kind,
                "from_model": from_model_id,
                "to_model": to_model_id,
            },
        ).scalar_one()
    )


def _fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"~{seconds:.0f}s"
    if seconds < 3600:
        return f"~{seconds / 60:.1f}m"
    return f"~{seconds / 3600:.1f}h"


async def _enqueue_batches(job_id: uuid.UUID, total: int, batch: int) -> int:
    """Enqueue one arq job per batch. Returns the number of jobs queued.

    Each enqueued call is ``reindex_batch(job_id, offset, batch)`` so the
    worker reads the job row itself and adapts (the candidate set at
    offset=O may have shrunk between enqueue and run — the worker's
    ORDER BY id makes that deterministic).
    """
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        count = 0
        offset = 0
        while offset < total:
            await redis.enqueue_job("reindex_batch", str(job_id), offset, batch)
            offset += batch
            count += 1
        return count
    finally:
        await redis.close()


@main.command("start")
@click.option(
    "--target-kind",
    type=click.Choice(VALID_TARGET_KINDS),
    required=True,
    help="Which embedding targets to reindex.",
)
@click.option(
    "--to-model",
    "to_model",
    required=True,
    help="Target model id (e.g. biomedclip-v2).",
)
@click.option(
    "--from-model",
    "from_model",
    default=None,
    help="Source model id. Omit for a fresh embed (no migration source).",
)
@click.option(
    "--batch",
    type=int,
    default=100,
    show_default=True,
    help="Targets per worker batch. 1..10000.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Count candidate rows and estimate runtime without enqueueing.",
)
def start(
    target_kind: str,
    to_model: str,
    from_model: str | None,
    batch: int,
    dry_run: bool,
) -> None:
    """Plan or kick off a reindex job."""
    if batch < 1 or batch > 10000:
        click.echo("--batch must be in 1..10000", err=True)
        sys.exit(2)
    if from_model is not None and from_model == to_model:
        click.echo("--from-model and --to-model cannot match", err=True)
        sys.exit(2)

    engine = _engine()
    with Session(engine) as session:
        total = _count_candidates(session, target_kind, from_model, to_model)

        if dry_run:
            click.echo(f"target_kind       : {target_kind}")
            click.echo(f"from_model        : {from_model or '(fresh embed)'}")
            click.echo(f"to_model          : {to_model}")
            click.echo(f"batch_size        : {batch}")
            click.echo(f"candidate rows    : {total}")
            click.echo(f"estimated batches : {(total + batch - 1) // batch}")
            click.echo(f"estimated runtime : {_fmt_eta(total * EST_SECONDS_PER_TARGET)}")
            click.echo("DRY RUN — no job created, no batches enqueued.")
            return

        if total == 0:
            click.echo("no candidate rows — nothing to do", err=True)
            sys.exit(0)

        row = session.execute(
            text(
                "INSERT INTO reindex_jobs "
                "(target_kind, from_model_id, to_model_id, status, "
                " total_items, batch_size) "
                "VALUES (:kind, :from, :to, 'pending', :total, :batch) "
                "RETURNING id"
            ),
            {
                "kind": target_kind,
                "from": from_model,
                "to": to_model,
                "total": total,
                "batch": batch,
            },
        ).scalar_one()
        session.commit()

        job_id = uuid.UUID(str(row))

    enqueued = asyncio.run(_enqueue_batches(job_id, total, batch))
    click.echo(f"job_id   : {job_id}")
    click.echo(f"target   : {target_kind} | {from_model or '(fresh)'} -> {to_model}")
    click.echo(f"total    : {total} rows")
    click.echo(f"batches  : {enqueued} enqueued (size={batch})")
    click.echo("Use `bvphoenix-reembed status <job-id>` to track progress.")


@main.command("status")
@click.argument("job_id")
def status(job_id: str) -> None:
    """Show progress and last error for a reindex job."""
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        click.echo(f"not a uuid: {job_id!r}", err=True)
        sys.exit(2)

    engine = _engine()
    with Session(engine) as session:
        row = session.execute(
            text(
                "SELECT id, target_kind, from_model_id, to_model_id, status, "
                "total_items, processed_items, failed_items, batch_size, "
                "error_summary, created_at, started_at, completed_at "
                "FROM reindex_jobs WHERE id = :jid"
            ),
            {"jid": jid},
        ).first()
        if row is None:
            click.echo(f"no reindex_job with id {job_id}", err=True)
            sys.exit(1)

    total = row.total_items or 0
    done = row.processed_items or 0
    pct = (100.0 * done / total) if total else 0.0
    click.echo(f"job_id       : {row.id}")
    click.echo(f"status       : {row.status}")
    click.echo(
        f"target       : {row.target_kind} | {row.from_model_id or '(fresh)'} -> {row.to_model_id}"
    )
    click.echo(f"progress     : {done}/{total} ({pct:.1f}%)  failed={row.failed_items}")
    click.echo(f"batch_size   : {row.batch_size}")
    click.echo(f"created_at   : {row.created_at}")
    click.echo(f"started_at   : {row.started_at}")
    click.echo(f"completed_at : {row.completed_at}")
    if row.error_summary:
        click.echo(f"last_error   : {row.error_summary}")


@main.command("cancel")
@click.argument("job_id")
def cancel(job_id: str) -> None:
    """Pause a running reindex job. In-flight batches finish, no new ones start.

    This is a cooperative cancel: the worker re-reads the job row
    between targets and aborts when it sees ``paused``. Nothing written
    so far is deleted — use ``rollback`` for that.
    """
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        click.echo(f"not a uuid: {job_id!r}", err=True)
        sys.exit(2)

    engine = _engine()
    with Session(engine) as session:
        result = session.execute(
            text(
                "UPDATE reindex_jobs SET status = 'paused' "
                "WHERE id = :jid AND status IN ('pending','running') "
                "RETURNING id, status"
            ),
            {"jid": jid},
        ).first()
        if result is None:
            # Either the id is bogus or the job isn't cancellable.
            existing = session.execute(
                text("SELECT status FROM reindex_jobs WHERE id = :jid"),
                {"jid": jid},
            ).first()
            if existing is None:
                click.echo(f"no reindex_job with id {job_id}", err=True)
                sys.exit(1)
            click.echo(
                f"job {job_id} is in status {existing.status!r} — cannot cancel",
                err=True,
            )
            sys.exit(1)
        session.commit()
    click.echo(f"job {job_id} paused — in-flight batches will drain and stop.")


@main.command("rollback")
@click.argument("job_id")
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the interactive confirmation.",
)
def rollback(job_id: str, yes: bool) -> None:
    """Delete every embedding row written by this job under ``to_model_id``.

    Bounded by ``(target_kind, to_model_id, created_at >= started_at)``,
    so existing vectors under the old model are safe. The job row
    itself is marked ``rolled_back`` *before* the DELETE so any
    concurrent worker halts on its next batch boundary.
    """
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        click.echo(f"not a uuid: {job_id!r}", err=True)
        sys.exit(2)

    engine = _engine()
    with Session(engine) as session:
        row = session.execute(
            text(
                "SELECT status, target_kind, to_model_id, started_at "
                "FROM reindex_jobs WHERE id = :jid"
            ),
            {"jid": jid},
        ).first()
        if row is None:
            click.echo(f"no reindex_job with id {job_id}", err=True)
            sys.exit(1)
        if row.started_at is None:
            click.echo(f"job {job_id} never started — nothing to roll back", err=True)
            sys.exit(1)
        if row.status == "running":
            click.echo(f"job {job_id} is still running — cancel it first", err=True)
            sys.exit(1)
        if row.status == "rolled_back":
            click.echo(f"job {job_id} already rolled back", err=True)
            sys.exit(0)

        if not yes:
            click.confirm(
                f"Delete all {row.target_kind} embeddings for model "
                f"{row.to_model_id!r} written since {row.started_at}? "
                "This cannot be undone.",
                abort=True,
            )

        # Flip status first so any worker still draining halts.
        session.execute(
            text(
                "UPDATE reindex_jobs SET status = 'rolled_back', "
                "completed_at = COALESCE(completed_at, :now) "
                "WHERE id = :jid"
            ),
            {"jid": jid, "now": datetime.now(UTC)},
        )
        session.commit()

        deleted = session.execute(
            text(
                "DELETE FROM embeddings "
                "WHERE target_kind = :kind "
                "  AND model_id = :model "
                "  AND created_at >= :since"
            ),
            {
                "kind": row.target_kind,
                "model": row.to_model_id,
                "since": row.started_at,
            },
        ).rowcount
        session.commit()
    click.echo(
        f"rolled back job {job_id}: deleted {deleted} embedding rows "
        f"for {row.target_kind}/{row.to_model_id}"
    )


if __name__ == "__main__":
    main()
