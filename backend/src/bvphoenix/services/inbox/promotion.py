"""Promotion / rejection outcomes for accepted inbox items.

``promote_item_payload`` is the body of the profile's ``on_accept``
hook: it runs inside the engine's ``promoting`` state, in the worker,
with the deciding actor's identity threaded through for provenance.
Per component (excluded and blocked ones skipped):

* DICOM Part-10 → :class:`DicomIngestor` under the item's patient,
  exactly the bulk-upload pipeline (UID-level dedup included), then
  ``pack_volume`` / ``embed_series`` post-processing;
* anything else → :func:`ingest_document_blob` (no-orphan folder
  placement + provenance pointing back at the inbox item);
* a held bulk-upload job → its deferred ``ingest_bulk_files`` enqueue.

The reviewer's options (target folder, per-component exclusions,
opt-in e-mail body promotion) travel in
``item.manifest["review_options"]``, stamped by the accept endpoint
before the decision lands — the manifest is the only channel wide
enough to survive the HTTP→worker hop without a parallel table.

Raising aborts the promotion (engine lands the item on ``failed`` with
the error in provenance); partial work stays in the transaction the
engine rolls into that outcome, so a failed promotion never half-fills
the fascicolo.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from arq import create_pool
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import InboundEmail, InboxItem, Job, Patient, Subject
from bvphoenix.services.arq_redis import redis_settings
from bvphoenix.services.bulk_ingest import _guess_document_type
from bvphoenix.services.dicom_ingest import DicomIngestor, has_dicm_preamble
from bvphoenix.services.documents.ingest_blob import ingest_document_blob
from bvphoenix.services.inbox.mime import parse_inbound_email
from bvphoenix.services.ingest_jobs import enqueue_postprocess_jobs
from bvphoenix.services.jobs import set_arq_job_id
from bvphoenix.services.review_queue.actor import ReviewActor
from bvphoenix.services.storage_quota import check_storage_quota
from bvphoenix.storage import get_s3_storage

logger = logging.getLogger(__name__)


def blocked_component_names(item: InboxItem) -> set[str]:
    """Names of components any auto-check hard-flagged.

    The plugins record per-component outcomes under
    ``details.components[name]`` with slightly different shapes
    (``{"status": "infected"}`` for ClamAV, ``{"verdict": "block"}``
    for the structural gates); a component is non-promotable when any
    check blocked it, even on an item a reviewer is allowed to accept
    (lot-level accept, component-level exclusion of the bad apples).
    """
    blocked: set[str] = set()
    checks = (item.auto_checks or {}).get("checks", {})
    for entry in checks.values():
        for name, comp in (entry.get("details", {}).get("components", {}) or {}).items():
            if not isinstance(comp, dict):
                continue
            if comp.get("verdict") == "block" or comp.get("status") == "infected":
                blocked.add(name)
    return blocked


def promotable_components(item: InboxItem) -> tuple[list[dict], list[dict]]:
    """Split the manifest into (to_promote, skipped) honouring reviewer
    exclusions and auto-check blocks. ``skipped`` carries the reason."""
    manifest = item.manifest or {}
    options = manifest.get("review_options", {})
    excluded = set(options.get("excluded_components", []))
    blocked = blocked_component_names(item)
    promote: list[dict] = []
    skipped: list[dict] = []
    for comp in manifest.get("components", []):
        name = comp.get("name")
        if name in blocked:
            skipped.append({"name": name, "reason": "blocked_by_auto_checks"})
        elif name in excluded:
            skipped.append({"name": name, "reason": "excluded_by_reviewer"})
        else:
            promote.append(comp)
    return promote, skipped


async def _promote_email_components(
    db: AsyncSession,
    *,
    item: InboxItem,
    patient: Patient,
    actor: ReviewActor,
    components: list[dict],
    folder_id: uuid.UUID | None,
) -> dict[str, Any]:
    settings = get_settings()
    storage = get_s3_storage()
    owner_subject_id = patient.managed_by_subject_id or patient.self_user_subject_id
    owner = await db.get(Subject, owner_subject_id) if owner_subject_id else None
    if owner is None:
        raise RuntimeError(f"patient {patient.id} has no managing subject")

    # Quota gate (defense in depth — the accept endpoint pre-checks so
    # the reviewer gets a clean 413 instead of a failed promotion).
    total_bytes = sum(int(c.get("size_bytes") or 0) for c in components)
    await check_storage_quota(db, subject_id=owner.id, additional_bytes=total_bytes)

    documents: list[dict] = []
    dicom_blobs: list[bytes] = []
    dicom_names: list[str] = []
    for comp in components:
        blob = await asyncio.to_thread(
            storage.get_object_bytes, bucket=settings.s3_bucket_raw, key=comp["s3_key"]
        )
        if has_dicm_preamble(blob):
            dicom_blobs.append(blob)
            dicom_names.append(comp["name"])
            continue
        # Document path. Kind via the same filename heuristic the bulk
        # pipeline uses; provenance is structurally ``email_attachment``.
        doc = await ingest_document_blob(
            db,
            patient=patient,
            actor=actor,
            uploaded_by_subject_id=actor.subject_id,
            filename=comp["name"],
            binary=blob,
            text=None,
            content_type=comp.get("content_type"),
            kind_id=_guess_document_type(comp["name"]),
            provenance_id="email_attachment",
            authority_id="original",
            folder_id=folder_id,
            source_kind="inbox_item",
            source_id=item.id,
        )
        documents.append({"id": str(doc.id), "name": comp["name"]})

    studies: list[str] = []
    series_pairs: list[tuple[uuid.UUID, str | None]] = []
    if dicom_blobs:
        ingestor = DicomIngestor(
            db=db,
            storage=storage,
            bucket=settings.s3_bucket_raw,
            owner=owner,
            tier="t1",
            is_public=False,
        )
        for name, blob in zip(dicom_names, dicom_blobs, strict=True):
            try:
                await ingestor.ingest_blob(blob)
            except Exception as exc:
                # One unparseable slice must not void the lot the
                # reviewer accepted: record and continue, the manifest
                # keeps the evidence.
                logger.warning("inbox DICOM component %s failed ingest: %s", name, exc)
                documents.append({"name": name, "error": str(exc)})
        for study in ingestor.touched_studies.values():
            study.patient_id = patient.id
        await ingestor.finalize()
        studies = [str(s.id) for s in ingestor.touched_studies.values()]
        series_pairs = [(s.id, s.modality) for s in ingestor.touched_series.values()]

    body_doc: dict | None = None
    options = (item.manifest or {}).get("review_options", {})
    if options.get("include_body") and item.inbound_email_id is not None:
        inbound = await db.get(InboundEmail, item.inbound_email_id)
        if inbound is not None:
            raw = await asyncio.to_thread(
                storage.get_object_bytes, bucket=settings.s3_bucket_raw, key=inbound.raw_s3_key
            )
            body_text = parse_inbound_email(raw).body_text
            if body_text:
                doc = await ingest_document_blob(
                    db,
                    patient=patient,
                    actor=actor,
                    uploaded_by_subject_id=actor.subject_id,
                    filename=f"{inbound.subject or 'email'}.txt",
                    binary=None,
                    text=body_text,
                    content_type="text/plain",
                    kind_id="unclassified",
                    provenance_id="email_body",
                    authority_id="original",
                    title=inbound.subject or "Email",
                    folder_id=folder_id,
                    source_kind="inbox_item",
                    source_id=item.id,
                )
                body_doc = {"id": str(doc.id), "kind": "email_body"}

    # Post-processing for freshly ingested series (pack + embed), the
    # same tail every other ingest path runs.
    if series_pairs:
        redis = await create_pool(redis_settings(settings.redis_url))
        try:
            await enqueue_postprocess_jobs(redis, series_pairs)
        finally:
            await redis.close()

    outcome: dict[str, Any] = {"documents": documents, "studies": studies}
    if body_doc:
        outcome["body_document"] = body_doc
    return outcome


async def _promote_held_upload(db: AsyncSession, *, item: InboxItem) -> dict[str, Any]:
    """Release the deferred ``ingest_bulk_files`` enqueue of a held
    upload job. Idempotent: a job already carrying an arq id (a retry
    after a crash between enqueue and commit) is not double-fired."""
    job = await db.get(Job, item.upload_job_id)
    if job is None:
        raise RuntimeError(f"inbox item {item.id} references missing job {item.upload_job_id}")
    if job.arq_job_id:
        return {"bulk_job": str(job.id), "already_enqueued": True}
    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        handle = await redis.enqueue_job("ingest_bulk_files", str(job.id))
    finally:
        await redis.close()
    if handle is not None:
        await set_arq_job_id(db, job.id, handle.job_id)
    return {"bulk_job": str(job.id)}


async def promote_item_payload(
    db: AsyncSession, *, item: InboxItem, actor: ReviewActor
) -> dict[str, Any]:
    """The ``on_accept`` hook body: ingest the lot, return the outcome
    (stored by the engine in provenance and mirrored on the row)."""
    patient = await db.get(Patient, item.patient_id)
    if patient is None:
        raise RuntimeError(f"inbox item {item.id} references missing patient")

    if item.upload_job_id is not None:
        outcome = await _promote_held_upload(db, item=item)
    else:
        components, skipped = promotable_components(item)
        options = (item.manifest or {}).get("review_options", {})
        folder_id = uuid.UUID(options["folder_id"]) if options.get("folder_id") else None
        outcome = await _promote_email_components(
            db,
            item=item,
            patient=patient,
            actor=actor,
            components=components,
            folder_id=folder_id,
        )
        if skipped:
            outcome["skipped"] = skipped

    item.promoted_refs = outcome
    return outcome


async def reject_item_cleanup(db: AsyncSession, *, item: InboxItem) -> None:
    """Rejection side-effects beyond the staged-blob purge: a held
    upload job is cancelled so the session sweeper reclaims its
    staging. Never raises for ordinary cleanup trouble (the rejection
    is already decided)."""
    if item.upload_job_id is None:
        return
    job = await db.get(Job, item.upload_job_id)
    if job is not None and job.status == "queued" and not job.arq_job_id:
        job.status = "cancelled"


__all__ = [
    "blocked_component_names",
    "promotable_components",
    "promote_item_payload",
    "reject_item_cleanup",
]
