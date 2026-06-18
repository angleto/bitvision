"""Training cohort assembly (P5 of the annotation overhaul).

Turns a structured Finding query into a training-ready, de-identified
**labels manifest**: the coded annotations (type / anatomy / morphology /
measurements + geometry pointers) the platform has accrued, selected by
attribute, gated by training consent + k-anonymity, and re-keyed so no
patient / study / author identifier leaks. This is the payoff of the
whole annotation overhaul — the structured metadata that makes the corpus
usable to train algorithms.

What this module does NOT do (tracked as P5-rest): stream the actual
image + mask bytes into a ZIP bundle. That reuses the existing
``services.patient_export`` streaming machinery (stream-zip +
``S3Storage.upload_iter`` + ``_fetch_blob_bytes(deidentify=True)``) behind
a Job; this module produces the cohort selection + the labels manifest
that bundle is built around.

Safety model (training visibility is a *separate, stricter* axis than
read visibility):

1. read boundary — ``visible_studies_filter`` (never widened);
2. structured query — ``_apply_structured_filters`` (the cohort);
3. training tier — ``ImagingStudy.contribution_tier IN ('t3','t4')``
   (T1/T2 are never poolable even if readable);
4. active consent — a non-revoked ``TrainingConsent`` for (study, tier);
5. k-anonymity — every (modality, body_part) bucket ≥ ``k_min`` or the
   request is rejected (we reject rather than silently suppress);
6. de-identification — the manifest is re-keyed to synthetic ids and
   carries no patient / study / finding / author UUID.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import String, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api.findings import _apply_structured_filters
from bvphoenix.db.models import (
    AnatomySite,
    Finding,
    FindingGeometry,
    FindingType,
    ImagingStudy,
    Instance,
    Segmentation,
    Series,
    TrainingConsent,
    User,
)
from bvphoenix.services import k_anonymity
from bvphoenix.services.permissions import apply_scope_filter, visible_studies_filter

# Measurement columns carried into the manifest (kept in sync with the
# Finding model + the API).
_MEASUREMENT_FIELDS = (
    "longest_diameter_mm",
    "short_axis_mm",
    "volume_ml",
    "suv_max",
    "suv_peak",
    "suv_mean",
    "hu_mean",
    "hu_std",
)

# Training-eligible contribution tiers (T1/T2 are never poolable).
TRAINING_TIERS: tuple[str, ...] = ("t3", "t4")


@dataclass(frozen=True)
class FindingExportRow:
    """One finding's raw fields + identifiers, as loaded from the DB.

    Carries the identifiers (study_id, author) deliberately so the *pure*
    manifest builder is the single place that strips + re-keys them — and
    so the de-identification can be unit-tested end-to-end."""

    finding_id: uuid.UUID
    study_id: uuid.UUID
    author_kind: str
    type_key: str
    type_category: str
    type_code: str | None
    anatomy_key: str | None
    anatomy_code: str | None
    laterality: str | None
    morphology: list[str]
    measurements: dict[str, float]
    bbox_lps: dict | None
    status: str
    confidence: float | None
    # [{role, kind: 'marker'|'segmentation', label?}]
    geometry: list[dict[str, Any]] = field(default_factory=list)


def synthetic_study_map(rows: list[FindingExportRow]) -> dict[uuid.UUID, str]:
    """Map each real study_id to a stable synthetic id (``study-0001``) in
    first-seen order. The single source of truth for re-keying, shared by
    the labels manifest AND the byte bundle so the ``images/``/``masks/``
    trees line up with ``labels.json`` (no drift)."""
    study_syn: dict[uuid.UUID, str] = {}
    for r in rows:
        if r.study_id not in study_syn:
            study_syn[r.study_id] = f"study-{len(study_syn) + 1:04d}"
    return study_syn


def build_labels_manifest(
    rows: list[FindingExportRow],
    *,
    dataset_id: str,
    generated_at: str,
    kanon: dict[str, int] | None = None,
    study_syn: dict[uuid.UUID, str] | None = None,
) -> dict[str, Any]:
    """Assemble the de-identified, coded labels manifest. PURE.

    Re-keys every study + finding to a synthetic, stable, per-cohort id
    (``study-0001`` / ``finding-0001``). The output contains NO patient,
    study, finding, or author UUID — only synthetic ids, coded vocabulary
    slugs/codes, measurements, and geometry roles. ``author_kind`` is kept
    (human / agent / system) so the provenance class is trainable-on
    without identifying the author. Pass ``study_syn`` (from
    :func:`synthetic_study_map`) to share the re-keying with the bundle.
    """
    if study_syn is None:
        study_syn = synthetic_study_map(rows)
    items: list[dict[str, Any]] = []
    for i, r in enumerate(rows, start=1):
        items.append(
            {
                "finding_id": f"finding-{i:04d}",
                "study_id": study_syn[r.study_id],
                "type": r.type_key,
                "category": r.type_category,
                "type_code": _code(r.type_code),
                "anatomy": r.anatomy_key,
                "anatomy_code": _code(r.anatomy_code),
                "laterality": r.laterality,
                "morphology": list(r.morphology or []),
                "measurements": dict(r.measurements or {}),
                "bbox_lps": r.bbox_lps,
                "status": r.status,
                "confidence": r.confidence,
                "author_kind": r.author_kind,
                "geometry": list(r.geometry or []),
            }
        )
    return {
        "dataset_id": dataset_id,
        "generated_at": generated_at,
        "schema": "bvphoenix.training-labels/v1",
        "study_count": len(study_syn),
        "finding_count": len(items),
        "k_anonymity": kanon or {},
        "items": items,
    }


def _code(code: str | None) -> dict[str, str] | None:
    """A code value carries its system; seeded NULL today (curated later)."""
    return {"code": code} if code else None


async def select_cohort(
    db: AsyncSession,
    user: User | None,
    *,
    type: str | None = None,
    anatomy: str | None = None,
    laterality: str | None = None,
    morphology: list[str] | None = None,
    status_filter: str | None = None,
    min_diameter_mm: float | None = None,
    max_diameter_mm: float | None = None,
    min_volume_ml: float | None = None,
    min_suv_max: float | None = None,
    scope: str = "all",
    k_min: int = k_anonymity.DEFAULT_K_MIN,
) -> tuple[list[FindingExportRow], dict[str, int]]:
    """Select the consented, k-anon-checked findings matching the query.

    Raises ``k_anonymity.KAnonymityError`` when a (modality, body_part)
    bucket is below ``k_min``. Returns ``(rows, kanon_distribution)``; each
    row carries identifiers for de-id at manifest-build time.
    """
    visible = apply_scope_filter(await visible_studies_filter(db, user), scope, user)
    visible_ids = visible.with_only_columns(ImagingStudy.id).subquery()

    # Active training consent for this study + its tier.
    consent_exists = (
        exists()
        .where(TrainingConsent.study_id == Finding.study_id)
        # contribution_tier is a PG enum; tier is varchar — cast the enum to
        # text so PG has a varchar = varchar operator (no implicit cast exists).
        .where(TrainingConsent.tier == ImagingStudy.contribution_tier.cast(String))
        .where(TrainingConsent.revoked_at.is_(None))
    )

    stmt = (
        select(
            Finding,
            FindingType.key,
            FindingType.category,
            FindingType.code,
            AnatomySite.key,
            AnatomySite.code,
            ImagingStudy.modalities,
            ImagingStudy.study_description,
        )
        .join(FindingType, FindingType.id == Finding.finding_type_id)
        .outerjoin(AnatomySite, AnatomySite.id == Finding.anatomy_site_id)
        .join(ImagingStudy, ImagingStudy.id == Finding.study_id)
        .where(
            Finding.study_id.in_(select(visible_ids.c.id)),
            Finding.deleted_at.is_(None),
            ImagingStudy.contribution_tier.in_(TRAINING_TIERS),
            consent_exists,
        )
        .order_by(Finding.study_id, Finding.created_at)
    )
    stmt = _apply_structured_filters(
        stmt,
        type=type,
        anatomy=anatomy,
        laterality=laterality,
        morphology=morphology,
        status_filter=status_filter,
        min_diameter_mm=min_diameter_mm,
        max_diameter_mm=max_diameter_mm,
        min_volume_ml=min_volume_ml,
        min_suv_max=min_suv_max,
    )
    db_rows = (await db.execute(stmt)).all()
    if not db_rows:
        return [], {}

    finding_ids = [r[0].id for r in db_rows]
    geom_by_finding = await _geometry_refs(db, finding_ids)

    rows: list[FindingExportRow] = []
    fingerprints: list[k_anonymity.StudyFingerprint] = []
    seen_studies: set[uuid.UUID] = set()
    for f, tkey, tcat, tcode, akey, acode, modalities, descr in db_rows:
        measurements = {m: getattr(f, m) for m in _MEASUREMENT_FIELDS if getattr(f, m) is not None}
        rows.append(
            FindingExportRow(
                finding_id=f.id,
                study_id=f.study_id,
                author_kind=f.author_kind,
                type_key=tkey,
                type_category=tcat,
                type_code=tcode,
                anatomy_key=akey,
                anatomy_code=acode,
                laterality=f.laterality,
                morphology=list(f.morphology_keys or []),
                measurements=measurements,
                bbox_lps=f.bbox_lps,
                status=f.status,
                confidence=f.confidence,
                geometry=geom_by_finding.get(f.id, []),
            )
        )
        if f.study_id not in seen_studies:
            seen_studies.add(f.study_id)
            tokens = (descr or "").strip().split()
            fingerprints.append(
                k_anonymity.StudyFingerprint(
                    modality=(modalities or ["unknown"])[0],
                    body_part=tokens[0] if tokens else "unknown",
                )
            )

    # k-anonymity gate — raises KAnonymityError on a sub-threshold bucket;
    # returns the bucket distribution on success.
    buckets = k_anonymity.enforce(fingerprints, k_min=k_min)
    distribution = {f"{mod}/{bp}": n for (mod, bp), n in buckets.items()}
    return rows, distribution


async def _geometry_refs(
    db: AsyncSession, finding_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Resolve each finding's geometry links to (role, kind[, mask label])."""
    if not finding_ids:
        return {}
    rows = (
        await db.execute(
            select(
                FindingGeometry.finding_id,
                FindingGeometry.role,
                FindingGeometry.marker_id,
                FindingGeometry.segmentation_id,
                Segmentation.label,
            )
            .outerjoin(Segmentation, Segmentation.id == FindingGeometry.segmentation_id)
            .where(FindingGeometry.finding_id.in_(finding_ids))
        )
    ).all()
    out: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for fid, role, marker_id, seg_id, seg_label in rows:
        ref: dict[str, Any] = {"role": role}
        if seg_id is not None:
            ref["kind"] = "segmentation"
            if seg_label:
                ref["mask_label"] = seg_label
        elif marker_id is not None:
            ref["kind"] = "marker"
        out.setdefault(fid, []).append(ref)
    return out


async def cohort_blob_plan(
    db: AsyncSession, study_syn: dict[uuid.UUID, str]
) -> list[dict[str, Any]]:
    """Enumerate the cohort's image + mask blobs, named by SYNTHETIC ids.

    Returns work items ``{kind, name, bucket, key, study_id}`` where ``name``
    is a synthetic, de-identified path (``study-0001/series-01/img-0001.dcm``
    / ``.../masks/<label>.bin``) — never a real study / series / instance
    UUID. ``kind`` drives the worker's per-blob de-id (DICOM scrubbed;
    masks are headerless raw uint8, no PHI). The byte fetch happens in the
    worker; this is the (DB-only) plan.

    Each item also carries the real ``study_id`` (a UUID) for worker-side
    ledger attribution (per-study bytes + content hash for the dataset
    producer). It is NEVER serialized into the artifact — only ``name``
    reaches the ZIP — so this does not weaken the de-identification.
    """
    work: list[dict[str, Any]] = []
    for study_id, syn in study_syn.items():
        series_rows = (
            (
                await db.execute(
                    select(Series)
                    .where(Series.study_id == study_id)
                    .order_by(Series.series_number.asc().nullslast())
                )
            )
            .scalars()
            .all()
        )
        for k, series in enumerate(series_rows, start=1):
            instances = (
                (
                    await db.execute(
                        select(Instance)
                        .where(Instance.series_id == series.id)
                        .order_by(Instance.instance_number.asc().nullslast())
                    )
                )
                .scalars()
                .all()
            )
            for j, inst in enumerate(instances, start=1):
                if inst.s3_bucket and inst.s3_key:
                    work.append(
                        {
                            "kind": "dicom",
                            "name": f"{syn}/series-{k:02d}/img-{j:04d}.dcm",
                            "bucket": inst.s3_bucket,
                            "key": inst.s3_key,
                            "study_id": study_id,
                        }
                    )
            seg_rows = (
                (await db.execute(select(Segmentation).where(Segmentation.series_id == series.id)))
                .scalars()
                .all()
            )
            for seg in seg_rows:
                if seg.s3_bucket and seg.s3_key:
                    work.append(
                        {
                            "kind": "mask",
                            "name": f"{syn}/series-{k:02d}/masks/{seg.label}.bin",
                            "bucket": seg.s3_bucket,
                            "key": seg.s3_key,
                            "study_id": study_id,
                        }
                    )
    return work


async def resolve_cohort_contributors(
    db: AsyncSession, study_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Map each cohort study to its contributor subject.

    The contributor is the user holding the active (non-revoked) training
    consent at the study's contribution tier — the same consent
    :func:`select_cohort` gated on, so every selected study resolves. A
    consent revoked in the race window between selection and this call simply
    drops out: that study gets no contributor, its ``DatasetStudy`` row
    carries a NULL ``contributor_subject_id`` and earns no payout weight.
    """
    if not study_ids:
        return {}
    rows = (
        await db.execute(
            select(TrainingConsent.study_id, TrainingConsent.user_subject_id)
            .join(ImagingStudy, ImagingStudy.id == TrainingConsent.study_id)
            .where(
                TrainingConsent.study_id.in_(study_ids),
                # Enum (contribution_tier) vs varchar (tier): cast to text.
                TrainingConsent.tier == ImagingStudy.contribution_tier.cast(String),
                TrainingConsent.revoked_at.is_(None),
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}
