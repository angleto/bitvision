"""Accept / reject side-effects for public-contribution submissions.

``promote_submission`` publishes exactly what the reviewer approved:

* **t4 (public CC)** — clone-and-scrub, never flip-in-place. The staged
  redacted bytes (stamped ``BurnedInAnnotation=NO`` + CID 7050 ``113101`` /
  ``113102`` at this point, per the MIDI-B human-accept rule) are ingested
  through the normal :class:`DicomIngestor` pipeline into a NEW study owned by
  the platform-owner subject on a public OpenData patient. The clone is clean
  AT REST, so every downstream surface (viewer volumes, thumbnails, DICOMweb,
  exports) is safe by construction — no per-surface egress gate can be
  forgotten. The owner's original study is never mutated (same philosophy as
  ``services.publish``: anonymised data is a new artifact, GDPR Art. 4).
* **t3 (training pool)** — in-place tier flip (nothing becomes publicly
  readable), plus a verified-clean blob pointer per redacted instance
  (``instances.pixel_clean_s3_*`` + ``pixel_deid_status='approved'``) so the
  training-cohort export ships the human-approved redaction instead of
  excluding the instance forever.

Pixel-gated components that could not be staged (SR/encapsulated documents,
undecodable pixels, header-scrub failures) are recorded in ``promoted_refs``
and never ship — fail-closed, the drop is visible.

Retry semantics: the hook runs inside the engine's ``promoting`` transition;
any raise rolls the whole transaction back and the item stays ``accepted`` for
the maintenance sweep to retry. S3 uploads use deterministic keys where we
control them (``_pixel-clean/{instance_id}``); the clone path may leave
orphan blobs from a failed attempt (uuid4-keyed canonical uploads) — private
bucket, already-clean bytes, storage-only cost. Staged blobs are purged by the
WORKER after the commit, never inside this hook (a later raise could not
un-delete them).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import ImagingStudy, Instance, Patient, Subject, Submission
from bvphoenix.services.deid.errors import DeidVerificationError, RequiresReview
from bvphoenix.services.deidentify import deidentify_dicom_bytes
from bvphoenix.services.face_deid import get_defacer
from bvphoenix.services.pixel_deid import classify_pixel_risk_bytes
from bvphoenix.services.public_contribution.redaction import (
    CONTRIB_STAGED_PREFIX,
    pixel_clean_key,
    stamp_clean_provenance,
)
from bvphoenix.services.review_queue.actor import ReviewActor
from bvphoenix.storage import get_s3_storage

logger = logging.getLogger(__name__)

# ``source_collection`` of t4 clones published by the contribution pipeline.
# Provenance contract of the column: "this collection de-identified the bytes
# upstream, serve them verbatim" — here the upstream is our own staged
# redaction + human review.
CONTRIB_SOURCE_COLLECTION = "bvphoenix-contributions"


class PromotionIntegrityError(RuntimeError):
    """A staged blob is missing or fails its sha256 check — never publish
    bytes the reviewer did not see. The engine rolls back and the item stays
    ``accepted`` for the maintenance sweep to retry."""


def _pseudonym(submission_id: uuid.UUID) -> str:
    # Deterministic per submission so a promote retry names the same patient.
    return f"OpenData Contribution {hashlib.sha256(submission_id.bytes).hexdigest()[:8]}"


def _method_json(item: Submission, *, entry: dict | None, clean_at_rest: bool) -> dict[str, Any]:
    """The ``instances.pixel_deid_method`` audit blob for one approved instance."""
    codes: list[str] = []
    if entry is not None:
        if (entry.get("risk_level") or "") == "high":
            codes.append("113101")
        if entry.get("face_deid_applied"):
            codes.append("113102")
    method: dict[str, Any] = {
        "engine": "tesseract",
        "submission_id": str(item.id),
        "reviewed_by_subject_id": (
            str(item.reviewed_by_subject_id) if item.reviewed_by_subject_id else None
        ),
        "method_codes": codes,
        "clean_at_rest": clean_at_rest,
    }
    if entry is not None:
        method["staged_sha256"] = entry.get("staged_sha256")
        method["deid_method_version"] = entry.get("staged_deid_version")
        method["redaction_count"] = len(entry.get("staged_redactions") or [])
        method["residual_suspect"] = bool(entry.get("staged_residual"))
    return method


async def _load_stamped_clean(storage: Any, settings: Any, entry: dict) -> bytes:
    """Fetch one staged blob, verify its sha256 against what the reviewer saw,
    and stamp the human-accept provenance. Raises on any mismatch."""
    key = entry["staged_redacted_key"]
    try:
        blob = await asyncio.to_thread(
            storage.get_object_bytes, bucket=settings.s3_bucket_raw, key=key
        )
    except Exception as exc:
        raise PromotionIntegrityError(f"staged blob unavailable: {key}") from exc
    sha = hashlib.sha256(blob).hexdigest()
    if sha != entry.get("staged_sha256"):
        raise PromotionIntegrityError(f"staged blob sha256 mismatch: {key}")
    return await asyncio.to_thread(
        stamp_clean_provenance,
        blob,
        risk_level=str(entry.get("risk_level") or "high"),
        face_deid_applied=bool(entry.get("face_deid_applied")),
    )


async def promote_submission(
    db: AsyncSession, *, item: Submission, actor: ReviewActor
) -> dict[str, Any]:
    """Publish the approved submission. Runs inside the engine's ``promoting``
    transition; a raise aborts the publish (see module docstring)."""
    settings = get_settings()
    storage = get_s3_storage()
    now = datetime.now(UTC)
    entries = [i for i in (item.manifest or {}).get("instances", []) if i.get("s3_key")]

    refs: dict[str, Any] = {"target_tier": item.target_tier}
    if item.source_study_id is not None:
        refs["source_study_id"] = str(item.source_study_id)
    skipped: list[dict[str, Any]] = []

    if item.target_tier == "t3":
        clean_count = 0
        for entry in entries:
            iid = str(entry.get("instance_id") or "")
            if entry.get("staged_redacted_key"):
                clean = await _load_stamped_clean(storage, settings, entry)
                inst = await db.get(Instance, uuid.UUID(iid))
                if inst is None:
                    # Source withdrawn between accept and promote: the audit
                    # trail (manifest) keeps the evidence, nothing to stamp.
                    skipped.append({"instance_id": iid, "reason": "instance_row_missing"})
                    continue
                key = pixel_clean_key(iid)
                await asyncio.to_thread(
                    storage.upload_bytes, clean, bucket=settings.s3_bucket_raw, key=key
                )
                inst.pixel_deid_status = "approved"
                inst.pixel_clean_s3_bucket = settings.s3_bucket_raw
                inst.pixel_clean_s3_key = key
                inst.pixel_deid_method = _method_json(item, entry=entry, clean_at_rest=False)
                inst.pixel_deid_at = now
                clean_count += 1
            elif entry.get("staged_reason"):
                # Pixel-gated but never staged (SR / undecodable / header-scrub
                # failure): stays excluded from every egress, visibly.
                skipped.append({"instance_id": iid, "reason": entry["staged_reason"]})
        if item.source_study_id is not None:
            study = await db.get(ImagingStudy, item.source_study_id)
            if study is not None:
                study.contribution_tier = item.target_tier
                refs["study_id"] = str(study.id)
        refs["clean_count"] = clean_count

    else:  # t4 — clone-and-scrub into the public OpenData namespace
        from bvphoenix.services.dicom_ingest import DicomIngestor
        from bvphoenix.services.permissions import platform_owner_subject_id

        platform_id = platform_owner_subject_id()
        owner = await db.get(Subject, platform_id)
        if owner is None:  # fresh dev DB; prod is seeded by migration 0036
            owner = Subject(id=platform_id, kind="system")
            db.add(owner)
            await db.flush()

        deface_on = get_defacer() is not None
        # Ingest at the private default tier: the t4 flip happens below in the
        # same transaction, TOGETHER with the license/provenance fields the
        # ``ck_imaging_studies_t4_license`` CHECK requires of every t4 row.
        ingestor = DicomIngestor(
            db=db,
            storage=storage,
            bucket=settings.s3_bucket_raw,
            owner=owner,
        )
        from pydicom.errors import InvalidDicomError

        published = 0
        for entry in entries:
            iid = str(entry.get("instance_id") or "")
            if entry.get("staged_redacted_key"):
                # Transient S3 / sha errors raise PromotionIntegrityError ->
                # rollback -> retry (never a permanent skip on infra).
                blob = await _load_stamped_clean(storage, settings, entry)
            elif entry.get("staged_reason"):
                skipped.append({"instance_id": iid, "reason": entry["staged_reason"]})
                continue
            else:
                # Not pixel-gated at check time. Fetch (transient errors must
                # NOT be swallowed — they roll the whole promote back so the
                # maintenance sweep retries), then header-scrub + re-verify the
                # pixel risk from the bytes about to ship (defense in depth
                # against a stale manifest / classifier drift): a gated
                # instance without a reviewed redaction never ships.
                raw = await asyncio.to_thread(
                    storage.get_object_bytes,
                    bucket=entry.get("s3_bucket") or settings.s3_bucket_raw,
                    key=entry["s3_key"],
                )
                try:
                    blob = await asyncio.to_thread(deidentify_dicom_bytes, raw)
                except (RequiresReview, DeidVerificationError) as exc:
                    # Content the header engine cannot clear: a permanent,
                    # correct per-instance skip (never publishable).
                    skipped.append(
                        {"instance_id": iid, "reason": f"header_scrub:{type(exc).__name__}"}
                    )
                    continue
                risk = classify_pixel_risk_bytes(blob)
                if risk.is_high or (deface_on and risk.level == "low"):
                    skipped.append({"instance_id": iid, "reason": f"unstaged_{risk.level}_risk"})
                    continue
            try:
                result = await ingestor.ingest_blob(blob)
            except (InvalidDicomError, ValueError) as exc:
                # A genuinely unparseable/invalid blob is permanently
                # unpublishable -> per-instance skip. Storage / DB failures are
                # transient and MUST propagate so the transaction rolls back and
                # the item stays 'accepted' for the maintenance sweep to retry
                # (never a silently incomplete public clone).
                skipped.append({"instance_id": iid, "reason": f"ingest:{type(exc).__name__}"})
                continue
            published += int(result.created)

        public_patient_id: uuid.UUID | None = None
        for study in ingestor.touched_studies.values():
            if study.patient_id is None:
                if public_patient_id is None:
                    patient = Patient(
                        managed_by_subject_id=platform_id,
                        display_name=_pseudonym(item.id),
                    )
                    db.add(patient)
                    await db.flush()
                    public_patient_id = patient.id
                study.patient_id = public_patient_id
            else:
                # Same source study contributed twice: reuse the existing clone.
                public_patient_id = public_patient_id or study.patient_id
            # Publish: tier + the license/provenance the t4 CHECK requires.
            # License is the platform's OpenData default (see
            # api/governance.py); per-contributor license choice is a future
            # option. ``source_collection`` names OUR contribution pipeline as
            # the (de-identifying) upstream — the same contract as external
            # collections (TCIA, ...): bytes are clean at rest, serve verbatim.
            study.contribution_tier = "t4"
            study.is_public = True
            study.license_spdx = study.license_spdx or "CC-BY-4.0"
            study.source_collection = study.source_collection or CONTRIB_SOURCE_COLLECTION
            study.source_subject_id = study.source_subject_id or _pseudonym(item.id)
            # The clone's bytes went through the CURRENT header engine at
            # staging/publish — stamp the at-rest fast path so public serve
            # streams stored bytes instead of re-scrubbing per download.
            study.deidentified_at = now
            study.deid_method_version = settings.deid_method_version
        await ingestor.finalize()

        # Every clone instance is human-approved by definition (the reviewer
        # accepted this exact content) and clean at rest: NULL pointer, the
        # stored s3_key IS the verified-clean blob. Stamp ONLY instances not
        # already approved by a prior promote: a clone study is fresh here
        # (offer_submission blocks re-offering a live/promoted study), but this
        # guard keeps a rare concurrent double-offer from re-attributing an
        # existing clone's provenance to a later submission.
        series_ids = [s.id for s in ingestor.touched_series.values()]
        if series_ids:
            rows = (
                (
                    await db.execute(
                        select(Instance).where(
                            Instance.series_id.in_(series_ids),
                            (Instance.pixel_deid_status.is_(None))
                            | (Instance.pixel_deid_status != "approved"),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for inst in rows:
                inst.pixel_deid_status = "approved"
                inst.pixel_deid_method = _method_json(item, entry=None, clean_at_rest=True)
                inst.pixel_deid_at = now

        item.public_patient_id = public_patient_id
        refs["published"] = published
        refs["public_patient_id"] = str(public_patient_id) if public_patient_id else None
        refs["public_study_ids"] = [str(s.id) for s in ingestor.touched_studies.values()]
        # (series_id, modality) pairs for the worker's post-commit
        # pack_volume/embed_series enqueue — the same tail every ingest runs.
        refs["public_series"] = [[str(s.id), s.modality] for s in ingestor.touched_series.values()]
        if published == 0:
            # Nothing publishable (every component unstageable). Promote
            # completes — the empty outcome is recorded, a human can see it —
            # rather than raising into an infinite retry loop.
            logger.warning("submission %s promoted with zero publishable instances", item.id)

    if skipped:
        refs["skipped"] = skipped
        logger.warning(
            "submission %s: %d component(s) did not ship: %s",
            item.id,
            len(skipped),
            sorted({s["reason"] for s in skipped}),
        )
    item.promoted_refs = refs
    return refs


async def purge_submission_staged(item: Submission) -> int:
    """Delete the staged redacted blobs (reject / post-promote cleanup).

    Best-effort + idempotent (S3 deletes tolerate a missing key); returns the
    count removed. The ``_contrib/`` prefix guard mirrors the inbox
    ``purge_staged`` so a malformed manifest can never aim the delete at
    canonical keys. Does NOT mutate the manifest (that would need a second
    commit at every call site); the reviewer read paths tolerate an
    already-purged blob by falling back to the on-the-fly recompute, so a
    dangling ``staged_redacted_key`` is harmless."""
    settings = get_settings()
    storage = get_s3_storage()
    removed = 0
    for entry in (item.manifest or {}).get("instances", []):
        key = entry.get("staged_redacted_key")
        if not key or not key.startswith(f"{CONTRIB_STAGED_PREFIX}/"):
            continue
        try:
            await asyncio.to_thread(storage.delete_object, bucket=settings.s3_bucket_raw, key=key)
            removed += 1
        except Exception:
            logger.warning("failed to purge staged blob %s", key, exc_info=True)
    return removed


__all__ = ["PromotionIntegrityError", "promote_submission", "purge_submission_staged"]
