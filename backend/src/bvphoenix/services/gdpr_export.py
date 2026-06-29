"""GDPR Art. 20 data-portability ZIP builder.

Mirrors :mod:`bvphoenix.services.patient_export` for a single-user
"all my data" bundle. The route in :mod:`bvphoenix.api.gdpr` and the
async worker ``export_gdpr_zip`` both delegate here so the manifest
schema and the README payload stay in sync between sync and async
paths.

Scope (docs/security-gdpr.md): consents history, erasure requests,
studies the user owns, reports they authored, markers they authored,
Health Records they manage or self-own, patient documents they
uploaded or attach to a managed patient, and the user's own audit-
log entries. DICOM pixel data is intentionally NOT included; users
who want raw images go through the per-study download endpoint or
the Fascicolo export.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    AuditLog,
    Consent,
    DataErasureRequest,
    Document,
    ImagingStudy,
    Marker,
    Patient,
    ReportContent,
    User,
)
from bvphoenix.storage import get_s3_storage

logger = logging.getLogger(__name__)


GDPR_EXPORT_SCHEMA_VERSION = 1

# Self-identifying container tag. The manifest carries this so any
# consumer (a re-import, a third-party PHR, a CI validator) can detect
# "this is a bitvision PHR-Bundle" without sniffing the shape, and pair
# it with ``schema_version`` to pick the right reader. The format and
# its JSON Schema are published as an open spec in docs/phr-bundle.md +
# docs/schemas/phr-bundle.v1.schema.json; the conformance test pins the
# two constants to the schema's ``const`` declarations so the code and
# the published spec can never drift silently.
PHR_BUNDLE_FORMAT = "bitvision.phr-bundle"


def _serialize_report(r: ReportContent) -> dict[str, Any]:
    """Project a v3 ``ReportContent`` row into a PHR-Bundle entry.

    Pure (no DB), so the conformance test can exercise the exact
    serialization that the legacy ``Report`` dead-symbol bug silently
    broke, without seeding a row. ``author_kind`` is surfaced so
    AI-drafted content stays visibly distinct from human-written.
    """
    return {
        "id": str(r.id),
        "clinical_event_id": str(r.clinical_event_id),
        "authority_id": r.authority_id,
        "status": r.status,
        "title": r.title,
        "narrative_md": r.narrative_md,
        "author_kind": r.author_kind,
        "model_id": r.model_id,
        "provider": r.provider,
        "created_at": r.created_at.isoformat(),
    }


def _serialize_document(d: Document) -> dict[str, Any]:
    """Project a ``Document`` row into a PHR-Bundle entry. Pure (no DB).

    v3: the document "type" is the ``kind_id`` taxonomy slug (e.g.
    'referto', 'unclassified'); the blob MIME is a separate field. Soft-
    deleted-but-not-purged documents are still held by the platform, so
    an honest "all your data" export lists them with ``deleted_at``
    rather than hiding them.
    """
    return {
        "id": str(d.id),
        "patient_id": str(d.patient_id) if d.patient_id else None,
        "document_kind": d.kind_id,
        "authority_id": d.authority_id,
        "title": d.title,
        "text": d.text,
        "content_type": d.file_content_type,
        "content_sha256": d.content_sha256,
        "document_date": str(d.document_date) if d.document_date else None,
        "deleted_at": d.deleted_at.isoformat() if d.deleted_at else None,
        "created_at": d.created_at.isoformat(),
    }


async def build_gdpr_bundle(db: AsyncSession, user: User) -> dict[str, Any]:
    """Assemble the manifest dict. Pure read-side; no commit.

    Kept separate from the ZIP-packing step so callers that want JSON
    only (e.g. an admin compliance audit) can use this without paying
    the zip cost.
    """
    bundle: dict[str, Any] = {
        "format": PHR_BUNDLE_FORMAT,
        "schema_version": GDPR_EXPORT_SCHEMA_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "user": {
            "subject_id": str(user.subject_id),
            "email": user.email,
            "oidc_subject": user.oidc_subject,
            "is_admin": user.is_admin,
        },
    }

    consent_rows = list(
        (
            await db.execute(
                select(Consent)
                .where(Consent.user_subject_id == user.subject_id)
                .order_by(Consent.granted_at)
            )
        )
        .scalars()
        .all()
    )
    bundle["consents"] = [
        {
            "id": str(c.id),
            "kind": c.kind,
            "granted_at": c.granted_at.isoformat(),
            "revoked_at": c.revoked_at.isoformat() if c.revoked_at else None,
            "metadata": c.metadata_,
        }
        for c in consent_rows
    ]

    erasure_rows = list(
        (
            await db.execute(
                select(DataErasureRequest)
                .where(DataErasureRequest.user_subject_id == user.subject_id)
                .order_by(DataErasureRequest.requested_at)
            )
        )
        .scalars()
        .all()
    )
    bundle["erasure_requests"] = [
        {
            "id": str(r.id),
            "scope": r.scope,
            "status": r.status,
            "reason": r.reason,
            "requested_at": r.requested_at.isoformat(),
            "completed_at": (r.completed_at.isoformat() if r.completed_at else None),
        }
        for r in erasure_rows
    ]

    studies = list(
        (
            await db.execute(
                select(ImagingStudy).where(ImagingStudy.owner_subject_id == user.subject_id)
            )
        )
        .scalars()
        .all()
    )
    bundle["studies"] = [
        {
            "id": str(s.id),
            "study_instance_uid": s.study_instance_uid,
            "patient_id": str(s.patient_id) if s.patient_id else None,
            "contribution_tier": s.contribution_tier,
            "is_public": s.is_public,
            "study_description": s.study_description,
            "study_date": str(s.study_date) if s.study_date else None,
            "modalities": s.modalities or [],
            "created_at": s.created_at.isoformat(),
        }
        for s in studies
    ]

    # v3: the legacy study-scoped ``Report`` was replaced by the
    # clinical-event-scoped ``ReportContent`` (narrative markdown, with
    # an explicit human/agent authoring trail). "Reports the user
    # authored" maps to ``created_by_subject_id``. We surface the
    # author_kind so AI-drafted content stays visibly distinct from
    # human-written content even inside the user's own data export.
    reports = list(
        (
            await db.execute(
                select(ReportContent)
                .where(ReportContent.created_by_subject_id == user.subject_id)
                .order_by(ReportContent.created_at)
            )
        )
        .scalars()
        .all()
    )
    bundle["reports"] = [_serialize_report(r) for r in reports]

    markers = list(
        (await db.execute(select(Marker).where(Marker.author_subject_id == user.subject_id)))
        .scalars()
        .all()
    )
    bundle["markers"] = [
        {
            "id": str(m.id),
            "target_kind": m.target_kind,
            "target_id": str(m.target_id),
            "kind": m.kind,
            "geometry": m.geometry,
            "computed": m.computed,
            "body": m.body,
            "created_at": m.created_at.isoformat(),
        }
        for m in markers
    ]

    patients = list(
        (
            await db.execute(
                select(Patient).where(
                    or_(
                        Patient.managed_by_subject_id == user.subject_id,
                        Patient.self_user_subject_id == user.subject_id,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    patient_ids = [p.id for p in patients]
    bundle["patients"] = [
        {
            "id": str(p.id),
            "display_name": p.display_name,
            # v3: derive legacy tax_id + external_id from the
            # external_identifiers JSONB. The full identifier list is
            # also surfaced so the GDPR export carries every business
            # id the patient has on file.
            "external_id": next(
                (
                    e.get("value")
                    for e in (p.external_identifiers or [])
                    if isinstance(e, dict) and e.get("type") == "MR"
                ),
                None,
            ),
            "birth_date": str(p.birth_date) if p.birth_date else None,
            "sex": p.sex,
            "tax_id": next(
                (
                    e.get("value")
                    for e in (p.external_identifiers or [])
                    if isinstance(e, dict) and e.get("type") == "fiscal-code"
                ),
                None,
            ),
            "external_identifiers": list(p.external_identifiers or []),
            "phone": p.phone,
            "email": p.email,
            "address": p.address,
            "blood_type": p.blood_type,
            "allergies": p.allergies,
            "notes": p.notes,
            "managed_by_subject_id": (
                str(p.managed_by_subject_id) if p.managed_by_subject_id else None
            ),
            "self_user_subject_id": (
                str(p.self_user_subject_id) if p.self_user_subject_id else None
            ),
            "created_at": p.created_at.isoformat(),
        }
        for p in patients
    ]

    doc_filter = Document.uploaded_by_subject_id == user.subject_id
    if patient_ids:
        doc_filter = or_(doc_filter, Document.patient_id.in_(patient_ids))
    docs = list((await db.execute(select(Document).where(doc_filter))).scalars().all())
    bundle["patient_documents"] = [_serialize_document(d) for d in docs]

    audit_rows = list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.actor_subject_id == user.subject_id)
                .order_by(AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    bundle["audit_log"] = [
        {
            "id": str(a.id),
            "action": a.action,
            "resource_kind": a.resource_kind,
            "resource_id": str(a.resource_id) if a.resource_id else None,
            "metadata": a.metadata_,
            "ip_address": str(a.ip_address) if a.ip_address else None,
            "user_agent": a.user_agent,
            "created_at": a.created_at.isoformat(),
        }
        for a in audit_rows
    ]

    return bundle


def pack_gdpr_zip(*, bundle: dict[str, Any], user: User) -> bytes:
    """Wrap the manifest dict in a self-describing ZIP.

    A ``README.txt`` explains scope and intentional omissions
    (DICOM); ``manifest.json`` is the canonical payload.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(bundle, indent=2, default=str, ensure_ascii=False),
        )
        zf.writestr(
            "README.txt",
            (
                "bitvision phoenix — PHR-Bundle (your portable health record)\n"
                f"Format: {bundle['format']} v{bundle['schema_version']}\n"
                f"Exported for: {user.email}\n"
                f"Generated at: {bundle['exported_at']}\n\n"
                "manifest.json contains every record the platform holds\n"
                "about you, grouped by domain (user / consents / studies /\n"
                "reports / markers / patients / patient_documents /\n"
                "audit_log / erasure_requests). It is also a GDPR Art. 20\n"
                "data-portability export.\n\n"
                "The PHR-Bundle is an open, versioned container: the\n"
                "manifest schema is published at docs/phr-bundle.md and\n"
                "docs/schemas/phr-bundle.v1.schema.json so the file can be\n"
                "re-imported here or read by any third-party tool.\n\n"
                "DICOM pixel data is NOT included in this bundle — use the\n"
                "per-study download endpoint or the Fascicolo export if you\n"
                "also need the raw images.\n"
            ),
        )
    return buf.getvalue()


async def build_gdpr_zip(db: AsyncSession, user: User) -> tuple[bytes, dict[str, Any]]:
    """Convenience: ``build_gdpr_bundle`` + ``pack_gdpr_zip`` in one
    call. Returns ``(zip_bytes, bundle)`` matching the patient_export
    contract."""
    bundle = await build_gdpr_bundle(db, user)
    return pack_gdpr_zip(bundle=bundle, user=user), bundle


def gdpr_export_filename(user: User) -> str:
    """Stable filename: keyed by subject_id (privacy-safe; no email
    in path) plus a unix-timestamp suffix so two consecutive exports
    by the same user do not collide on object storage."""
    ts = int(datetime.now(UTC).timestamp())
    return f"bvphoenix-export-{user.subject_id}-{ts}.zip"


def gdpr_export_s3_key(*, job_id: uuid.UUID, user: User) -> str:
    """Canonical S3 key. Job-scoped to avoid concurrent collisions
    when the same user retries with a different idempotency key (e.g.
    after the cached result expired)."""
    return f"exports/gdpr/{job_id}/{gdpr_export_filename(user)}"


def upload_gdpr_zip(zip_bytes: bytes, *, job_id: uuid.UUID, user: User) -> tuple[str, str]:
    """Persist the bundle on S3, return ``(bucket, key)``."""
    storage = get_s3_storage()
    settings = get_settings()
    bucket = settings.s3_bucket_derivatives
    key = gdpr_export_s3_key(job_id=job_id, user=user)
    storage.upload_bytes(zip_bytes, bucket=bucket, key=key)
    return bucket, key


__all__ = [
    "GDPR_EXPORT_SCHEMA_VERSION",
    "PHR_BUNDLE_FORMAT",
    "build_gdpr_bundle",
    "build_gdpr_zip",
    "gdpr_export_filename",
    "gdpr_export_s3_key",
    "pack_gdpr_zip",
    "upload_gdpr_zip",
]
