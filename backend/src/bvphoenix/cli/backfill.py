"""``bvphoenix-backfill`` — operational reprocessing CLI.

Subcommand:

    $ bvphoenix-backfill chunks --patient <id> [--source-kind document]
    $ bvphoenix-backfill chunks --all [--dry-run] [--chunker-version VER]

``chunks`` enqueues the appropriate ``chunk_and_embed_*`` Arq task for
every text-bearing source row of the selected scope:

* ``document`` — every document with at least one OCR row.
* ``clinical_note`` — every non-deleted clinical note.
* ``summary`` — every AI summary (no soft-delete on summaries today).
* ``report_content`` — every report content row, regardless of status.

The tasks themselves are idempotent (skip when the source's content
hash matches the chunk set already on file). Re-runs are safe and
cheap.

The CLI does *not* run inference inline; it only enqueues. An Arq
worker with the ``ai`` extra installed has to be running to process the
queue. Use ``--dry-run`` to preview the candidate count without
enqueueing.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import click
from arq import create_pool
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models.text_chunks import (
    CHUNK_SOURCE_KINDS,
    DEFAULT_CHUNKER_VERSION,
)
from bvphoenix.services.arq_redis import redis_settings


@click.group()
def main() -> None:
    """Operational reprocessing helpers."""


def _engine():
    return create_engine(get_settings().database_url_sync, future=True)


_SOURCE_QUERIES: dict[str, str] = {
    "document": """
        SELECT DISTINCT d.id
        FROM documents d
        JOIN document_ocr o ON o.document_id = d.id
        WHERE d.deleted_at IS NULL
          {patient_clause}
        ORDER BY d.id
    """,
    "clinical_note": """
        SELECT id
        FROM clinical_notes
        WHERE 1=1
          {patient_clause}
        ORDER BY id
    """,
    "summary": """
        SELECT s.id
        FROM summaries s
        WHERE 1=1
          {patient_clause}
        ORDER BY s.id
    """,
    "report_content": """
        SELECT rc.id
        FROM report_contents rc
        JOIN clinical_events ce ON ce.id = rc.clinical_event_id
        WHERE 1=1
          {patient_clause}
        ORDER BY rc.id
    """,
}

# Patient-filter SQL fragment per source kind. Some sources carry
# ``patient_id`` directly; others require a join.
_PATIENT_CLAUSES: dict[str, str] = {
    "document": "AND d.patient_id = :pid",
    "clinical_note": "AND patient_id = :pid",
    "summary": (
        "AND ("
        "  (s.target_kind = 'patient' AND s.target_id = :pid)"
        "  OR EXISTS (SELECT 1 FROM imaging_studies x"
        "             WHERE x.id = s.target_id AND s.target_kind = 'study'"
        "               AND x.patient_id = :pid)"
        "  OR EXISTS (SELECT 1 FROM documents dx"
        "             WHERE dx.id = s.target_id AND s.target_kind = 'document'"
        "               AND dx.patient_id = :pid)"
        ")"
    ),
    "report_content": "AND ce.patient_id = :pid",
}

_TASK_NAMES: dict[str, str] = {
    "document": "chunk_and_embed_document",
    "clinical_note": "chunk_and_embed_clinical_note",
    "summary": "chunk_and_embed_summary",
    "report_content": "chunk_and_embed_report_content",
}


def _candidate_ids(
    session: Session,
    *,
    source_kind: str,
    patient_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    sql_template = _SOURCE_QUERIES[source_kind]
    if patient_id is not None:
        sql = sql_template.format(patient_clause=_PATIENT_CLAUSES[source_kind])
        rows = session.execute(text(sql), {"pid": patient_id}).all()
    else:
        sql = sql_template.format(patient_clause="")
        rows = session.execute(text(sql)).all()
    return [uuid.UUID(str(r[0])) for r in rows]


async def _enqueue_all(
    source_kind: str,
    ids: list[uuid.UUID],
    chunker_version: str,
) -> int:
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    task_name = _TASK_NAMES[source_kind]
    try:
        count = 0
        for sid in ids:
            await redis.enqueue_job(task_name, str(sid), chunker_version)
            count += 1
        return count
    finally:
        await redis.close()


@main.command("chunks")
@click.option(
    "--patient",
    "patient",
    default=None,
    help="Patient UUID. Mutually exclusive with --all.",
)
@click.option(
    "--all",
    "all_patients",
    is_flag=True,
    default=False,
    help="Backfill chunks for every patient. Use with care on large datasets.",
)
@click.option(
    "--source-kind",
    "source_kinds",
    multiple=True,
    type=click.Choice(list(CHUNK_SOURCE_KINDS)),
    help=(
        "Restrict to a subset of source kinds. Pass multiple times to combine. "
        "Default: all four kinds."
    ),
)
@click.option(
    "--chunker-version",
    "chunker_version",
    default=DEFAULT_CHUNKER_VERSION,
    show_default=True,
    help="Chunker version label persisted on every row.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Count candidates and print the plan without enqueueing.",
)
def chunks(
    patient: str | None,
    all_patients: bool,
    source_kinds: tuple[str, ...],
    chunker_version: str,
    dry_run: bool,
) -> None:
    """Enqueue chunk-and-embed jobs across every text-bearing source."""
    if (patient is None) == (not all_patients):
        click.echo("specify exactly one of --patient <id> or --all", err=True)
        sys.exit(2)

    patient_uuid: uuid.UUID | None = None
    if patient is not None:
        try:
            patient_uuid = uuid.UUID(patient)
        except ValueError:
            click.echo(f"--patient must be a UUID, got {patient!r}", err=True)
            sys.exit(2)

    kinds = list(source_kinds) if source_kinds else list(CHUNK_SOURCE_KINDS)

    engine = _engine()
    plan: dict[str, list[uuid.UUID]] = {}
    with Session(engine) as session:
        for kind in kinds:
            plan[kind] = _candidate_ids(session, source_kind=kind, patient_id=patient_uuid)

    scope = f"patient {patient_uuid}" if patient_uuid else "ALL patients"
    click.echo(f"scope             : {scope}")
    click.echo(f"chunker_version   : {chunker_version}")
    total = 0
    for kind, ids in plan.items():
        click.echo(f"  {kind:<16}: {len(ids)} candidates")
        total += len(ids)
    click.echo(f"total candidates  : {total}")

    if dry_run:
        click.echo("DRY RUN — no jobs enqueued.")
        return
    if total == 0:
        click.echo("nothing to do")
        return

    grand_total = 0
    for kind, ids in plan.items():
        if not ids:
            continue
        n = asyncio.run(_enqueue_all(kind, ids, chunker_version))
        click.echo(f"enqueued {kind:<16}: {n} jobs")
        grand_total += n
    click.echo(f"enqueued total    : {grand_total}")
    click.echo("Run an Arq worker with the `ai` extra to process the queue.")


if __name__ == "__main__":
    main()
