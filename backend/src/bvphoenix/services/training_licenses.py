"""Training-license lifecycle service (F10).

The only transition this module owns today is ``draft / pending_duc /
approved  → signed``. Making it a dedicated service keeps the DUC
veto from turning into a convention: any call site that wants to
sign a license goes through :func:`sign_license`, which refuses to
flip the status unless the linked :class:`DUCRequest` is ``approved``.

The Data Use Committee has a veto on every license deal. The DB's
CheckConstraint only scopes the status
vocabulary, it does not tie ``signed`` to a DUC decision. Closing
that gap at the service layer lets us keep the storage schema simple
while still making the "no DUC, no signature" rule impossible to
sidestep from backend code. A DB-level trigger to catch out-of-band
UPDATEs is tracked as a follow-up.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import DUCRequest, TrainingLicense


class TrainingLicenseError(RuntimeError):
    """Raised when a license transition cannot be performed.

    The message is safe to surface to the caller: it names the rule
    that failed (missing DUC approval, already signed, revoked, etc.)
    without leaking any internal identifiers the caller didn't
    already know.
    """


async def sign_license(
    db: AsyncSession,
    *,
    license_id: uuid.UUID,
) -> TrainingLicense:
    """Transition a training license to ``signed``.

    Preconditions enforced here (all raise :class:`TrainingLicenseError`
    before any mutation — the transaction is either committed by the
    caller or rolled back cleanly):

    * The license row exists.
    * Its current ``status`` is ``approved``. ``draft`` / ``pending_duc``
      mean the committee has not cleared it; ``signed`` means the
      operation already ran; ``revoked`` means it was taken back after
      signing and should not be resurrected without a new deal.
    * ``duc_request_id`` is set and points at a ``DUCRequest`` that
      itself is ``approved``. This is the belt on top of the status
      check: even if someone flipped the license to ``approved`` out
      of band, the DUC row still has to have run the vote.

    On success, ``status`` becomes ``signed`` and ``signed_at`` is
    stamped with ``datetime.now(UTC)``. The row is ``flush``ed; the
    caller owns the commit so audit logging and other side-effects
    can land in the same transaction.
    """
    license_ = (
        await db.execute(select(TrainingLicense).where(TrainingLicense.id == license_id))
    ).scalar_one_or_none()
    if license_ is None:
        raise TrainingLicenseError(f"training license {license_id} not found")

    if license_.status == "signed":
        raise TrainingLicenseError("license is already signed")
    if license_.status == "revoked":
        raise TrainingLicenseError("license is revoked; sign a fresh deal instead")
    if license_.status != "approved":
        # 'draft' and 'pending_duc' both land here — the user-facing
        # hint says what needs to happen next.
        raise TrainingLicenseError(
            f"license is in status '{license_.status}'; it must be DUC-approved "
            "before it can be signed"
        )

    if license_.duc_request_id is None:
        # Belt-and-suspenders: an 'approved' license without a DUC
        # request was either hand-edited or predates the workflow.
        # Either way, the DUC veto forbids signing it.
        raise TrainingLicenseError(
            "license has no linked DUC request; cannot sign without committee review"
        )

    duc = (
        await db.execute(select(DUCRequest).where(DUCRequest.id == license_.duc_request_id))
    ).scalar_one_or_none()
    if duc is None:
        raise TrainingLicenseError(
            "linked DUC request is missing; cannot verify committee approval"
        )
    if duc.status != "approved":
        raise TrainingLicenseError(
            f"linked DUC request is in status '{duc.status}'; committee has not approved the deal"
        )

    license_.status = "signed"
    license_.signed_at = datetime.now(UTC)
    await db.flush()
    return license_


__all__ = ["TrainingLicenseError", "sign_license"]
