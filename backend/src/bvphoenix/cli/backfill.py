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
from bvphoenix.services.embeddable import (
    embeddable_modality_clause,
    embeddable_sop_class_clause,
)
from bvphoenix.services.text_models import load_text_model_specs_sync


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


# Image-embedding backfill ----------------------------------------------
# ``bvphoenix-import`` only enqueues ``pack_volume`` for imported series,
# never ``embed_series`` — so bulk-imported studies have no BiomedCLIP
# image vector and similarity search (``/api/similar-to``) finds nothing.
# This command backfills those vectors. ``embed_series`` is idempotent.
_IMAGE_MODEL_ID = "biomedclip-v1"  # what workers/embed_series.py MODEL_ID writes


def _series_candidate_ids(
    session: Session,
    *,
    patient_id: uuid.UUID | None,
    only_missing: bool,
) -> list[uuid.UUID]:
    where: list[str] = []
    params: dict[str, object] = {}
    # Never enqueue non-image series — they can't be embedded and would only
    # churn the worker + perpetually count as "missing". Two filters mirror
    # what ``embed_series`` actually embeds (single source of truth:
    # bvphoenix.services.embeddable):
    #   1. modality is not a known non-image one (SR / PR / KO / ...);
    #   2. the series has at least one instance with an embeddable SOP class
    #      (the worker filters to image instances and skips a series with
    #      none — e.g. a Raw Data Storage / SEG-only series that slips the
    #      modality filter). Without (2) such series re-enqueue forever and
    #      ``--only-missing`` never converges.
    where.append(embeddable_modality_clause("s.modality"))
    where.append(
        "EXISTS (SELECT 1 FROM instances i WHERE i.series_id = s.id AND "
        + embeddable_sop_class_clause("i.sop_class_uid")
        + ")"
    )
    if patient_id is not None:
        where.append("st.patient_id = :pid")
        params["pid"] = patient_id
    if only_missing:
        where.append(
            "NOT EXISTS (SELECT 1 FROM embeddings e "
            "WHERE e.target_kind = 'series' AND e.target_id = s.id "
            "AND e.model_id = :model)"
        )
        params["model"] = _IMAGE_MODEL_ID
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT s.id FROM series s "
        "JOIN imaging_studies st ON st.id = s.study_id"
        f"{clause} ORDER BY s.id"
    )
    rows = session.execute(text(sql), params).all()
    return [uuid.UUID(str(r[0])) for r in rows]


async def _enqueue_embed(ids: list[uuid.UUID]) -> int:
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        count = 0
        for sid in ids:
            await redis.enqueue_job("embed_series", str(sid))
            count += 1
        return count
    finally:
        await redis.close()


@main.command("embed")
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
    help="Backfill image embeddings for every patient. Use with care on large datasets.",
)
@click.option(
    "--only-missing/--all-series",
    "only_missing",
    default=True,
    show_default=True,
    help="Only series lacking a biomedclip-v1 vector (default), or re-embed every series.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Count candidate series and print the plan without enqueueing.",
)
def embed(
    patient: str | None,
    all_patients: bool,
    only_missing: bool,
    dry_run: bool,
) -> None:
    """Enqueue BiomedCLIP image-embedding jobs so similarity search works.

    ``bvphoenix-import`` only enqueues volume packing, so imported series
    have no image vector. This enqueues ``embed_series`` for each
    candidate series (idempotent). Requires an Arq worker with the ``ai``
    extra running to process the queue.
    """
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

    engine = _engine()
    with Session(engine) as session:
        ids = _series_candidate_ids(session, patient_id=patient_uuid, only_missing=only_missing)

    scope = f"patient {patient_uuid}" if patient_uuid else "ALL patients"
    click.echo(f"scope            : {scope}")
    click.echo(f"only-missing     : {only_missing}")
    click.echo(f"series candidates: {len(ids)}")

    if dry_run:
        click.echo("DRY RUN — no jobs enqueued.")
        return
    if not ids:
        click.echo("nothing to do")
        return

    n = asyncio.run(_enqueue_embed(ids))
    click.echo(f"enqueued embed_series: {n} jobs")
    click.echo("Run an Arq worker with the `ai` extra to process the queue.")


# Text-chunk embedding backfill -----------------------------------------
# Replaces the ad-hoc one-off snippet used to backfill the 122 document
# chunks. The model -> {arq_task, store_table} routing lives on the
# embedding_models registry row (model_metadata, migration 0023) — the
# same row the query path in chunk_search and the write path in
# chunk_and_embed resolve — so a model/table change is a registry write,
# not a code change. The --model value is the registry name, which by
# design equals the worker MODEL_ID and the value written into the
# store's ``model_id`` column.


def _chunk_candidates(
    session: Session,
    *,
    patient_id: uuid.UUID | None,
    only_missing: bool,
    store_table: str,
    model_id: str,
) -> list[tuple[uuid.UUID, str]]:
    where: list[str] = []
    params: dict[str, object] = {}
    if patient_id is not None:
        where.append("tc.patient_id = :pid")
        params["pid"] = patient_id
    if only_missing:
        # ``store_table`` is f-string-interpolated because table names cannot
        # be bind parameters. It is injection-safe: the value is
        # identifier-validated registry data (spec_from_registry), never
        # raw user input.
        where.append(
            f"NOT EXISTS (SELECT 1 FROM {store_table} te "
            "WHERE te.target_kind = 'document_chunk' AND te.target_id = tc.id "
            "AND te.model_id = :model)"
        )
        params["model"] = model_id
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT tc.id, tc.text FROM text_chunks tc{clause} ORDER BY tc.id"
    rows = session.execute(text(sql), params).all()
    return [(uuid.UUID(str(r[0])), r[1]) for r in rows]


async def _enqueue_embed_text(
    task_name: str,
    rows: list[tuple[uuid.UUID, str]],
) -> int:
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        count = 0
        for cid, body in rows:
            await redis.enqueue_job(task_name, "document_chunk", str(cid), body)
            count += 1
        return count
    finally:
        await redis.close()


@main.command("embed-text")
@click.option(
    "--model",
    "model",
    required=True,
    help=(
        "Text embedding model name (a routed, active row of the "
        "embedding_models registry, e.g. minilm-multi-v1 | bge-m3-v1)."
    ),
)
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
    help="Backfill text-chunk embeddings for every patient. Use with care.",
)
@click.option(
    "--only-missing/--all-chunks",
    "only_missing",
    default=True,
    show_default=True,
    help="Only chunks lacking a vector for the chosen model (default), or re-embed all.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Count candidate chunks and print the plan without enqueueing.",
)
def embed_text(
    model: str,
    patient: str | None,
    all_patients: bool,
    only_missing: bool,
    dry_run: bool,
) -> None:
    """Enqueue per-chunk text-embedding jobs for the chosen model.

    Routes to the model's arq task and pgvector store table as recorded
    on its embedding_models registry row (minilm-multi-v1 ->
    embed_text_ml / text_embeddings; bge-m3-v1 -> embed_bge_m3_all /
    text_embeddings_bge_m3). The tasks are idempotent (ON CONFLICT
    upsert) and no-op on blank chunk text. Requires an Arq worker with
    the ``ai`` extra running to process the queue.
    """
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

    engine = _engine()
    with Session(engine) as session:
        specs = load_text_model_specs_sync(session)
        spec = specs.get(model)
        if spec is None:
            click.echo(
                f"unknown or unrouted text model {model!r}; known: {sorted(specs)}",
                err=True,
            )
            sys.exit(2)
        rows = _chunk_candidates(
            session,
            patient_id=patient_uuid,
            only_missing=only_missing,
            store_table=spec.store_table,
            model_id=spec.model_id,
        )
    task_name = spec.arq_task

    scope = f"patient {patient_uuid}" if patient_uuid else "ALL patients"
    click.echo(f"scope            : {scope}")
    click.echo(f"model            : {model}")
    click.echo(f"only-missing     : {only_missing}")
    click.echo(f"chunk candidates : {len(rows)}")

    if dry_run:
        click.echo("DRY RUN — no jobs enqueued.")
        return
    if not rows:
        click.echo("nothing to do")
        return

    n = asyncio.run(_enqueue_embed_text(task_name, rows))
    click.echo(f"enqueued {task_name}: {n} jobs")
    click.echo("Run an Arq worker with the `ai` extra to process the queue.")


if __name__ == "__main__":
    main()
