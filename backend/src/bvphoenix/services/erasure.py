"""GDPR Art. 17 erasure service.

Anonymises a user and cascades the deletion / redaction across resources
they own. Invariants:

* Audit log entries about the user are retained (legal requirement —
  Art. 17(3)(b) allows retention to comply with other legal obligations)
  but their ``actor_subject_id`` is nulled via ``ON DELETE SET NULL`` on
  ``audit_log.actor_subject_id``. We explicitly keep the rows so incident
  response and security investigations can still reconstruct history.
* Publicly-published studies are kept (``is_public = true``): the user
  has released them under the public-domain-equivalent bucket and cannot
  retroactively pull them (contribution tiers).
  Their ownership is reassigned to the platform's anonymous ``public``
  subject. Private studies with no active external shares are deleted.
* Consents are not deleted — they are revoked (``revoked_at = now()``)
  so we retain proof of the consent history as required by Art. 7(1).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    AuditLog,
    Consent,
    DataErasureRequest,
    Grant,
    ImagingStudy,
    Patient,
    ShareLink,
    Subject,
    User,
)
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID


def _erased_email(subject_id: uuid.UUID) -> str:
    digest = hashlib.sha256(str(subject_id).encode("utf-8")).hexdigest()[:16]
    return f"deleted-{digest}@erased"


async def _user_has_legal_hold(db: AsyncSession, subject_id: uuid.UUID) -> bool:
    """Placeholder for future legal-hold integration.

    A legal hold would live in a dedicated table (``legal_holds``) joined
    on ``subject_id``. Until that feature lands, every request is
    unblocked. The call site still goes through this function so the
    policy seam is explicit.
    """
    return False


async def execute_erasure(
    db: AsyncSession,
    *,
    request: DataErasureRequest,
) -> dict[str, int]:
    """Perform the anonymisation for ``request``.

    Mutates database state. The caller is responsible for committing the
    surrounding transaction. Returns a counters dict for the audit log.
    """
    counters = {
        "consents_revoked": 0,
        "studies_deleted": 0,
        "studies_anonymised": 0,
        "grants_revoked": 0,
    }

    user = (
        await db.execute(select(User).where(User.subject_id == request.user_subject_id))
    ).scalar_one_or_none()
    if user is None:
        # Subject already erased or never existed as a user — nothing to do.
        request.status = "completed"
        request.completed_at = datetime.now(UTC)
        return counters

    subject_id = user.subject_id

    # 1. Revoke active consents (retain history per Art. 7(1)).
    now = datetime.now(UTC)
    if request.scope in ("self", "consents_only"):
        revoked_rows = (
            await db.execute(
                update(Consent)
                .where(
                    Consent.user_subject_id == subject_id,
                    Consent.revoked_at.is_(None),
                )
                .values(revoked_at=now)
                .returning(Consent.id)
            )
        ).all()
        counters["consents_revoked"] = len(revoked_rows)

    if request.scope == "consents_only":
        request.status = "completed"
        request.completed_at = now
        return counters

    # 2. Handle studies owned by the user.
    if request.scope in ("self", "studies"):
        studies = (
            (
                await db.execute(
                    select(ImagingStudy).where(ImagingStudy.owner_subject_id == subject_id)
                )
            )
            .scalars()
            .all()
        )
        study_ids = [s.id for s in studies]

        # Single query to find every study that still has at least one
        # active share link — avoids an N+1 scan across owned studies.
        shared_study_ids: set[uuid.UUID] = set()
        if study_ids:
            shared_rows = (
                await db.execute(
                    select(Grant.resource_id)
                    .join(ShareLink, ShareLink.grant_id == Grant.id)
                    .where(
                        Grant.resource_kind == "study",
                        Grant.resource_id.in_(study_ids),
                        Grant.revoked_at.is_(None),
                    )
                )
            ).all()
            shared_study_ids = {row[0] for row in shared_rows}

        for study in studies:
            if study.is_public or study.id in shared_study_ids:
                # Keep the data online; hand ownership to the anonymous public subject.
                study.owner_subject_id = PUBLIC_SUBJECT_ID
                counters["studies_anonymised"] += 1
            else:
                await db.delete(study)
                counters["studies_deleted"] += 1

    # 3. Revoke all grants *issued by* this user (they can no longer
    #    authorise access once erased).
    if request.scope in ("self", "studies", "annotations"):
        revoked = (
            await db.execute(
                update(Grant)
                .where(
                    Grant.grantor_subject_id == subject_id,
                    Grant.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoked_by_subject_id=subject_id)
                .returning(Grant.id)
            )
        ).all()
        counters["grants_revoked"] = len(revoked)

    # 4. Anonymise Health Records where the user is the self-subject.
    #    Managed Health Records stay visible to the managing clinician.
    if request.scope == "self":
        # Capture the id set BEFORE the update nulls ``self_user_subject_id``.
        # We need it for the entity_objects tombstoning below.
        self_patient_ids = [
            row[0]
            for row in (
                await db.execute(
                    select(Patient.id).where(Patient.self_user_subject_id == subject_id)
                )
            ).all()
        ]
        await db.execute(
            update(Patient)
            .where(Patient.self_user_subject_id == subject_id)
            .values(
                self_user_subject_id=None,
                display_name="Erased User",
                email=None,
                phone=None,
                address=None,
                tax_id=None,
                notes=None,
            )
        )

        # 4b. Tombstone the F12 entity_objects that carry textual PHI
        # written into the user's own fascicolo. We MUST NOT tombstone
        # objects that are also referenced from other patients' commits:
        # entity_objects are content-addressed (sha256 of canonical
        # JSON), so two clinically-identical short payloads dedup into a
        # single row across patients. The ``EXCEPT`` set-difference
        # below scopes the tombstone to objects exclusively used by the
        # erased user's own patients.
        if self_patient_ids:
            await db.execute(
                text(
                    """
                    WITH erased_objs AS (
                      SELECT DISTINCT me.object_hash
                      FROM manifest_entries me
                      JOIN commits c ON c.commit_hash = me.commit_hash
                      WHERE c.patient_id = ANY(:pids)
                        AND me.entity_kind != '_tree_'
                    ),
                    refs_elsewhere AS (
                      SELECT DISTINCT me.object_hash
                      FROM manifest_entries me
                      JOIN commits c ON c.commit_hash = me.commit_hash
                      WHERE me.object_hash IN (
                              SELECT object_hash FROM erased_objs
                            )
                        AND NOT (c.patient_id = ANY(:pids))
                    )
                    UPDATE entity_objects
                    SET payload = '{}'::jsonb,
                        is_tombstoned = true,
                        tombstoned_at = :now,
                        tombstoned_reason =
                          'gdpr.erasure_request:' || :req_id,
                        delta_bytes = NULL,
                        delta_parent_hash = NULL,
                        s3_bucket = NULL,
                        s3_key = NULL,
                        s3_etag = NULL,
                        storage_kind = 'full'
                    WHERE object_hash IN (
                            SELECT object_hash FROM erased_objs
                            EXCEPT
                            SELECT object_hash FROM refs_elsewhere
                          )
                      AND is_tombstoned = false
                    """
                ),
                {
                    "pids": self_patient_ids,
                    "now": now,
                    "req_id": str(request.id),
                },
            )

    # 5. Anonymise the user row itself. Audit log rows about the user are
    #    retained (``audit_log.actor_subject_id`` is ON DELETE SET NULL
    #    but we do NOT null them here — we redact identity instead).
    if request.scope == "self":
        user.email = _erased_email(subject_id)
        user.password_hash = None
        user.oidc_subject = None
        # Keep is_admin=False to be safe even though it should already be.
        user.is_admin = False

        # Redact the subject display_name.
        await db.execute(
            update(Subject).where(Subject.id == subject_id).values(display_name="Erased User")
        )

    # 6. Drop an audit breadcrumb so the erasure is itself provable.
    db.add(
        AuditLog(
            actor_subject_id=None,
            action="gdpr.erasure_executed",
            resource_kind="user",
            resource_id=subject_id,
            metadata_={
                "request_id": str(request.id),
                "scope": request.scope,
                "counters": counters,
            },
        )
    )

    request.status = "completed"
    request.completed_at = now
    return counters
