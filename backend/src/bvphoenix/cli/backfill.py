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
from bvphoenix.services.text_embedding import finding_embed_text
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
# Every ingest path now enqueues ``embed_series`` at import time via
# ``services.ingest_jobs.enqueue_postprocess_jobs`` (incl. ``bvphoenix-
# import``), and the worker cron ``reconcile_missing_embeddings`` heals any
# miss automatically. This command stays as the explicit, one-shot manual
# trigger (CI/ops, or to force a full re-embed): it enqueues ``embed_series``
# for embeddable image series, idempotently (skips series already vectored).
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


# Volume-geometry backfill ----------------------------------------------
# Migration 0009 added Derivative.geometry WITHOUT a backfill, so volumes
# packed before it carry geometry IS NULL — the viewer falls back to an
# identity frame and cross-study registration can't run in true patient
# space (LPS), which the longitudinal tumour-comparison feature needs.
# Re-running pack_volume recomputes geometry from the sorted DICOM and
# rewrites the (identical) blob; the task is idempotent.


def _geometry_candidate_ids(
    session: Session,
    *,
    patient_id: uuid.UUID | None,
    only_missing: bool,
) -> list[uuid.UUID]:
    where = ["d.kind = 'volume_f32'", "d.stack_index = 0"]
    params: dict[str, object] = {}
    if only_missing:
        where.append("d.geometry IS NULL")
    if patient_id is not None:
        where.append("st.patient_id = :pid")
        params["pid"] = patient_id
    sql = (
        "SELECT DISTINCT s.id FROM series s "
        "JOIN imaging_studies st ON st.id = s.study_id "
        "JOIN derivatives d ON d.series_id = s.id "
        "WHERE " + " AND ".join(where) + " ORDER BY s.id"
    )
    rows = session.execute(text(sql), params).all()
    return [uuid.UUID(str(r[0])) for r in rows]


async def _enqueue_pack(ids: list[uuid.UUID]) -> int:
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        count = 0
        for sid in ids:
            await redis.enqueue_job("pack_volume", str(sid))
            count += 1
        return count
    finally:
        await redis.close()


def _preview_candidate_ids(
    session: Session,
    *,
    patient_id: uuid.UUID | None,
    only_missing: bool,
) -> list[uuid.UUID]:
    """Series that have a packed full-res primary volume. With ``only_missing``
    (default) restrict to those LACKING the 1/8-res ``volume_f32_preview`` the
    viewer's progressive preview-first load needs."""
    joins = [
        "JOIN imaging_studies st ON st.id = s.study_id",
        "JOIN derivatives d ON d.series_id = s.id",
    ]
    where = ["d.kind = 'volume_f32'", "d.stack_index = 0"]
    params: dict[str, object] = {}
    if only_missing:
        joins.append(
            "LEFT JOIN derivatives p ON p.series_id = s.id AND p.kind = 'volume_f32_preview'"
        )
        where.append("p.id IS NULL")
    if patient_id is not None:
        where.append("st.patient_id = :pid")
        params["pid"] = patient_id
    sql = (
        "SELECT DISTINCT s.id FROM series s "
        + " ".join(joins)
        + " WHERE "
        + " AND ".join(where)
        + " ORDER BY s.id"
    )
    rows = session.execute(text(sql), params).all()
    return [uuid.UUID(str(r[0])) for r in rows]


async def _enqueue_prefetch(ids: list[uuid.UUID]) -> int:
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        count = 0
        for sid in ids:
            await redis.enqueue_job("prefetch_series", str(sid))
            count += 1
        return count
    finally:
        await redis.close()


@main.command("geometry")
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
    help="Backfill volume geometry for every patient. Use with care on large datasets.",
)
@click.option(
    "--only-missing/--all-volumes",
    "only_missing",
    default=True,
    show_default=True,
    help="Only volumes with geometry IS NULL (default), or re-pack every primary volume.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Count candidate series and print the plan without enqueueing.",
)
def geometry(
    patient: str | None,
    all_patients: bool,
    only_missing: bool,
    dry_run: bool,
) -> None:
    """Re-pack volumes whose ``Derivative.geometry`` is NULL so cross-study
    registration runs in true patient space (LPS).

    Enqueues ``pack_volume`` (idempotent) for each candidate series so the
    geometry is recomputed from the sorted DICOM. Requires an Arq worker
    running to process the queue.
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
        ids = _geometry_candidate_ids(session, patient_id=patient_uuid, only_missing=only_missing)

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

    n = asyncio.run(_enqueue_pack(ids))
    click.echo(f"enqueued pack_volume: {n} jobs")
    click.echo("Run an Arq worker to process the queue.")


@main.command("previews")
@click.option(
    "--patient", "patient", default=None, help="Patient UUID. Mutually exclusive with --all."
)
@click.option(
    "--all",
    "all_patients",
    is_flag=True,
    default=False,
    help="Generate previews for every patient. Use with care on large datasets.",
)
@click.option(
    "--only-missing/--all-volumes",
    "only_missing",
    default=True,
    show_default=True,
    help="Only series lacking a volume_f32_preview (default), or refresh every series.",
)
@click.option("--dry-run", is_flag=True, help="Count candidate series and print the plan.")
def previews(
    patient: str | None,
    all_patients: bool,
    only_missing: bool,
    dry_run: bool,
) -> None:
    """Pre-generate the 1/8-res ``volume_f32_preview`` for each series so the
    viewer's progressive preview-first load is instant (the first open of a
    series with no preview otherwise builds it on-the-fly, ~30 s over the
    throttled egress).

    Enqueues ``prefetch_series`` (idempotent — builds the preview from the
    cached full-res blob, no re-pack). Requires an Arq worker running.
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
        ids = _preview_candidate_ids(session, patient_id=patient_uuid, only_missing=only_missing)

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

    n = asyncio.run(_enqueue_prefetch(ids))
    click.echo(f"enqueued prefetch_series: {n} jobs")
    click.echo("Run an Arq worker to process the queue.")


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
    target_kind: str,
    rows: list[tuple[uuid.UUID, str]],
) -> int:
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        count = 0
        for cid, body in rows:
            await redis.enqueue_job(task_name, target_kind, str(cid), body)
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

    n = asyncio.run(_enqueue_embed_text(task_name, "document_chunk", rows))
    click.echo(f"enqueued {task_name}: {n} jobs")
    click.echo("Run an Arq worker with the `ai` extra to process the queue.")


# Finding coarse-text backfill -----------------------------------------
# The on-write path (api.findings._enqueue_finding_embed) fans a
# target_kind='finding' text-embed job out to every active model when a
# finding is created/updated, so newly-written findings land in MiniLM now
# and BGE-M3 the moment it is activated. But findings written BEFORE a
# model was activated never get its vector. This command is the catch-up:
# it recomposes each finding's coarse text with the SAME
# ``finding_embed_text`` the on-write path uses (single source of truth,
# so a re-embed is byte-identical) and enqueues the model's task.


def _finding_candidates(
    session: Session,
    *,
    patient_id: uuid.UUID | None,
    confirmed_only: bool,
    only_missing: bool,
    store_table: str,
    model_id: str,
) -> list[tuple[uuid.UUID, str]]:
    where = ["f.deleted_at IS NULL"]
    params: dict[str, object] = {}
    if patient_id is not None:
        where.append("f.patient_id = :pid")
        params["pid"] = patient_id
    if confirmed_only:
        where.append("f.status = 'confirmed'")
    if only_missing:
        # ``store_table`` is interpolated (table names cannot be bind
        # params); injection-safe — it is identifier-validated registry
        # data (spec_from_registry), never raw user input.
        where.append(
            f"NOT EXISTS (SELECT 1 FROM {store_table} te "
            "WHERE te.target_kind = 'finding' AND te.target_id = f.id "
            "AND te.model_id = :model)"
        )
        params["model"] = model_id
    clause = " WHERE " + " AND ".join(where)
    sql = (
        "SELECT f.id, ft.display, a.display, f.laterality, f.morphology_keys, f.description "
        "FROM findings f "
        "JOIN finding_types ft ON ft.id = f.finding_type_id "
        "LEFT JOIN anatomy_sites a ON a.id = f.anatomy_site_id"
        f"{clause} ORDER BY f.id"
    )
    rows = session.execute(text(sql), params).all()
    out: list[tuple[uuid.UUID, str]] = []
    for fid, type_display, anatomy_display, laterality, morphology, description in rows:
        body = finding_embed_text(
            type_display=type_display or "",
            anatomy_display=anatomy_display,
            laterality=laterality,
            morphology=list(morphology or []),
            description=description,
        )
        if body:  # skip blank compositions — the worker would no-op anyway
            out.append((uuid.UUID(str(fid)), body))
    return out


@main.command("embed-findings")
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
    help="Backfill finding embeddings for every patient. Use with care.",
)
@click.option(
    "--confirmed-only",
    is_flag=True,
    default=False,
    help="Only findings with status='confirmed' (default: every non-deleted finding).",
)
@click.option(
    "--only-missing/--all-findings",
    "only_missing",
    default=True,
    show_default=True,
    help="Only findings lacking a vector for the chosen model (default), or re-embed all.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Count candidate findings and print the plan without enqueueing.",
)
def embed_findings(
    model: str,
    patient: str | None,
    all_patients: bool,
    confirmed_only: bool,
    only_missing: bool,
    dry_run: bool,
) -> None:
    """Enqueue coarse text-embedding jobs (target_kind='finding') for the
    chosen model over pre-existing findings.

    Complements the on-write fan-out: use it after activating a new text
    model (e.g. bge-m3-v1) to give the historical corpus its vector so
    find_similar / semantic search see every finding. Routes to the model's
    Arq task + pgvector store from its registry row; idempotent (ON CONFLICT
    upsert). Requires an Arq worker with the ``ai`` extra running.
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
        rows = _finding_candidates(
            session,
            patient_id=patient_uuid,
            confirmed_only=confirmed_only,
            only_missing=only_missing,
            store_table=spec.store_table,
            model_id=spec.model_id,
        )
    task_name = spec.arq_task

    scope = f"patient {patient_uuid}" if patient_uuid else "ALL patients"
    click.echo(f"scope             : {scope}")
    click.echo(f"model             : {model}")
    click.echo(f"confirmed-only    : {confirmed_only}")
    click.echo(f"only-missing      : {only_missing}")
    click.echo(f"finding candidates: {len(rows)}")

    if dry_run:
        click.echo("DRY RUN — no jobs enqueued.")
        return
    if not rows:
        click.echo("nothing to do")
        return

    n = asyncio.run(_enqueue_embed_text(task_name, "finding", rows))
    click.echo(f"enqueued {task_name}: {n} jobs")
    click.echo("Run an Arq worker with the `ai` extra to process the queue.")


# Acquisition-timing backfill -------------------------------------------
# Phase 0 of the contrast-CT viewer added series.acquisition_time_of_day /
# contrast_bolus_agent / contrast_bolus_start_time and populates them at
# ingest. Series ingested BEFORE that have NULL timing, so the contrast-
# phase classifier can only use their description. This command reads the
# first instance's DICOM header from S3 and backfills the timing columns
# (a real header read — NOT faked). Unlike the enqueue commands above it
# does the S3 I/O inline, since it is a one-shot operational sweep. Run
# ``detect_study_phases`` afterwards (or the auto-classify path) to label.


def _timing_candidates(
    session: Session,
    *,
    patient_id: uuid.UUID | None,
    only_missing: bool,
) -> list[tuple[uuid.UUID, str, str]]:
    where = ["1=1"]
    params: dict[str, object] = {}
    if only_missing:
        where.append("s.acquisition_time_of_day IS NULL")
    if patient_id is not None:
        where.append("st.patient_id = :pid")
        params["pid"] = patient_id
    # One representative instance per series (lowest instance number) for
    # the header read.
    sql = (
        "SELECT s.id, i.s3_bucket, i.s3_key FROM series s "
        "JOIN imaging_studies st ON st.id = s.study_id "
        "JOIN LATERAL ("
        "  SELECT s3_bucket, s3_key FROM instances i2 "
        "  WHERE i2.series_id = s.id "
        "  ORDER BY i2.instance_number ASC NULLS LAST LIMIT 1"
        ") i ON true "
        "WHERE " + " AND ".join(where) + " ORDER BY s.id"
    )
    rows = session.execute(text(sql), params).all()
    return [(uuid.UUID(str(r[0])), r[1], r[2]) for r in rows]


@main.command("timing")
@click.option(
    "--patient", "patient", default=None, help="Patient UUID. Mutually exclusive with --all."
)
@click.option("--all", "all_patients", is_flag=True, default=False, help="Backfill every patient.")
@click.option(
    "--only-missing/--all-series",
    "only_missing",
    default=True,
    show_default=True,
    help="Only series with NULL acquisition timing (default), or re-read every series.",
)
@click.option("--dry-run", is_flag=True, help="Count candidates without reading S3 or writing.")
def timing(
    patient: str | None,
    all_patients: bool,
    only_missing: bool,
    dry_run: bool,
) -> None:
    """Backfill series acquisition timing from the first DICOM header.

    Reads ``AcquisitionTime`` (falling back to SeriesTime / ContentTime),
    ``ContrastBolusAgent`` and ``ContrastBolusStartTime`` from each series'
    first instance and writes them to the series row. Idempotent.
    """
    import io

    import pydicom

    from bvphoenix.services.dicom_ingest import _parse_dicom_time
    from bvphoenix.storage import get_s3_storage

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
        candidates = _timing_candidates(session, patient_id=patient_uuid, only_missing=only_missing)

    scope = f"patient {patient_uuid}" if patient_uuid else "ALL patients"
    click.echo(f"scope            : {scope}")
    click.echo(f"only-missing     : {only_missing}")
    click.echo(f"series candidates: {len(candidates)}")
    if dry_run:
        click.echo("DRY RUN — no S3 reads, no writes.")
        return
    if not candidates:
        click.echo("nothing to do")
        return

    storage = get_s3_storage()
    updated = 0
    failed = 0
    with Session(engine) as session:
        for i, (sid, bucket, key) in enumerate(candidates, start=1):
            try:
                data = storage.get_object_bytes(bucket=bucket, key=key)
                ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True)
                acq = (
                    _parse_dicom_time(getattr(ds, "AcquisitionTime", None))
                    or _parse_dicom_time(getattr(ds, "SeriesTime", None))
                    or _parse_dicom_time(getattr(ds, "ContentTime", None))
                )
                agent = getattr(ds, "ContrastBolusAgent", None) or None
                bolus = _parse_dicom_time(getattr(ds, "ContrastBolusStartTime", None))
                session.execute(
                    text(
                        "UPDATE series SET acquisition_time_of_day = :acq, "
                        "contrast_bolus_agent = :agent, contrast_bolus_start_time = :bolus "
                        "WHERE id = :id"
                    ),
                    {
                        "acq": acq,
                        "agent": str(agent) if agent else None,
                        "bolus": bolus,
                        "id": sid,
                    },
                )
                updated += 1
            except Exception as exc:
                failed += 1
                click.echo(f"  WARN series {sid}: {exc}", err=True)
            if i % 50 == 0:
                session.commit()
                click.echo(f"  …{i}/{len(candidates)}")
        session.commit()
    click.echo(f"updated          : {updated}")
    if failed:
        click.echo(f"failed (skipped) : {failed}")
    click.echo("Run `detect_study_phases` (MCP) or open the contrast viewer to classify.")


if __name__ == "__main__":
    main()
