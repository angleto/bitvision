"""Per-study training consent management (F6).

Owns the write-side lifecycle of :class:`TrainingConsent` rows:

* :func:`ensure_tier_consent` — mint (or re-use) the active consent
  row that authorises a single study for the T3 / T4 training pool.
  Called on upload (``dicom_upload`` / ``bulk_upload``) and on every
  T1/T2 → T3/T4 tier change (``PATCH /api/studies/{id}/tier``).
* :func:`revoke_tier_consent_for_study` — set ``revoked_at`` on the
  active row for a study. Called on T3/T4 → T1/T2 downgrade and by
  ``DELETE /api/studies/{id}/training-consent``.

Why a dedicated table and not the shared ``Consent``:

The GDPR-style ``Consent`` table tracks user-level opt-ins (privacy
policy, marketing email, global research toggle). It did double duty
for T3 per-study consent by extending a ``metadata['study_ids']``
list, which made revocation ambiguous (revoke which entry?) and
k-anonymity assembly slow (JSONB scan). Splitting them keeps each
table's indexing and lifecycle coherent.

The consent text is versioned. ``consent_version`` is an integer
that bumps whenever the UI copy changes; every row stores the
``consent_hash`` (sha256 of ``version + tier + text``) so an auditor
can pair each consent to the exact copy the user saw. Today the
version is hardcoded; wiring it to the actual Markdown text is a UI
follow-up.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import TrainingConsent

logger = logging.getLogger(__name__)


# Bumped whenever the consent text shown to the user changes. Any
# row written under an older version keeps its original hash; a new
# upload (or a re-grant after revoke) writes a row with the fresh
# version so an auditor can pair the row to the copy the user saw.
CURRENT_CONSENT_VERSION = 1


# Reference text used to derive ``consent_hash``. Short, stable, and
# lives next to the code that writes the rows so a drift between UI
# copy and stored hash is noticed in review rather than at audit time.
# Future work: inject the actual Markdown the frontend renders.
_CONSENT_TEMPLATES: dict[str, str] = {
    "t3": (
        "I consent to include this study in the de-identified training "
        "pool for AI model development. I may revoke this consent at "
        "any time; data already included in signed licence deals "
        "cannot be retroactively removed."
    ),
    "t4": (
        "I publish this study under a permissive CC license. Third "
        "parties, including commercial entities, may use it for any "
        "purpose compatible with the license."
    ),
}


def _compute_consent_hash(tier: str, version: int) -> str:
    """Return a stable sha256 hex digest for (tier, version, text)."""
    text = _CONSENT_TEMPLATES[tier]
    payload = f"v{version}|{tier}|{text}".encode()
    return hashlib.sha256(payload).hexdigest()


async def ensure_tier_consent(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID,
    study_id: uuid.UUID,
    tier: str,
) -> TrainingConsent | None:
    """Materialise the active consent row for (user, study, tier).

    No-op for T1 / T2 (returns ``None``): those tiers do not require
    a training-pool consent. For T3 / T4:

    * If an active row already exists for (user, study, tier), it is
      returned unchanged — idempotent re-upload is safe.
    * Otherwise a fresh row is inserted with the current consent
      version + hash.

    The partial unique index backs this up at the DB level so a race
    between two concurrent uploads collapses to a single active row.
    """
    if tier not in ("t3", "t4"):
        return None

    existing = (
        await db.execute(
            select(TrainingConsent).where(
                TrainingConsent.user_subject_id == user_subject_id,
                TrainingConsent.study_id == study_id,
                TrainingConsent.tier == tier,
                TrainingConsent.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = TrainingConsent(
        user_subject_id=user_subject_id,
        study_id=study_id,
        tier=tier,
        consent_version=CURRENT_CONSENT_VERSION,
        consent_hash=_compute_consent_hash(tier, CURRENT_CONSENT_VERSION),
        metadata_={"source": "upload_tier"},
    )
    db.add(row)
    await db.flush()
    logger.info(
        "training_consent.granted user=%s study=%s tier=%s version=%d",
        user_subject_id,
        study_id,
        tier,
        CURRENT_CONSENT_VERSION,
    )
    return row


async def ensure_tier_consents(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID,
    tier: str,
    study_ids: Iterable[uuid.UUID],
) -> list[TrainingConsent]:
    """Batch variant: one consent row per study.

    Kept as a plural wrapper so existing call sites that pass a list
    of studies (``bulk_upload``) stay succinct. T4 implies both tiers'
    rows only in the old ``Consent`` model — the new per-study layout
    collapses to a single row tagged ``tier='t4'`` because the study
    carries the tier as a column already; assembly code reads tier +
    active consent together. Returns the list of touched rows (empty
    for T1 / T2).
    """
    out: list[TrainingConsent] = []
    for sid in study_ids:
        row = await ensure_tier_consent(
            db,
            user_subject_id=user_subject_id,
            study_id=sid,
            tier=tier,
        )
        if row is not None:
            out.append(row)
    return out


async def revoke_tier_consent_for_study(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID,
    study_id: uuid.UUID,
    reason: str | None = None,
) -> list[TrainingConsent]:
    """Opt a single study back out of the training pool.

    Sets ``revoked_at`` on every active row for the (user, study)
    pair — usually just one row, but both ``tier='t3'`` and
    ``tier='t4'`` are handled in case the study was re-tiered without
    a clean revoke in between. Returns the rows that were touched
    (empty list when nothing was active).

    The consent row is not deleted: the revoked row stays around for
    audit. A subsequent re-grant inserts a fresh active row.
    """
    rows = (
        (
            await db.execute(
                select(TrainingConsent).where(
                    TrainingConsent.user_subject_id == user_subject_id,
                    TrainingConsent.study_id == study_id,
                    TrainingConsent.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
        if reason is not None:
            row.revoke_reason = reason

    await db.flush()
    # Propagate the revoke into the dataset ledger: any OPEN dataset that
    # already bundled this study now holds revoked data and must be rebuilt
    # before it can be licensed (Option 3 sovereignty gate; the sign gate
    # refuses a stale dataset). Local import to avoid a service-layer cycle.
    from bvphoenix.services.training_licenses import stale_open_datasets_for_study

    staled = await stale_open_datasets_for_study(db, study_id)
    logger.info(
        "training_consent.revoked user=%s study=%s rows=%d staled_datasets=%d",
        user_subject_id,
        study_id,
        len(rows),
        len(staled),
    )
    return rows


__all__ = [
    "CURRENT_CONSENT_VERSION",
    "ensure_tier_consent",
    "ensure_tier_consents",
    "revoke_tier_consent_for_study",
]
