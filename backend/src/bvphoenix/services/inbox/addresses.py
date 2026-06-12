"""Inbox address provisioning — create / list / relabel / revoke / route.

An address is a bearer capability (see ``codes``): provisioning is
lazy and opt-in per patient, a patient may hold several live addresses
(one per correspondent, like app-specific passwords) and revoking one
never disturbs the others. Every lifecycle write lands a provenance
row (target kind ``inbox_address``) — handing out or burning an
ingress capability is a security-relevant act on the record.

The caller owns the transaction (no commits here), the API layer owns
ETag enforcement; functions raise :class:`InboxAddressError` for
caller-mappable domain failures.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import Patient, PatientInboxAddress
from bvphoenix.services.inbox.codes import generate_code
from bvphoenix.services.provenance_log import record_provenance_event
from bvphoenix.services.review_queue.actor import ReviewActor

# A patient hardly needs more live addresses than correspondents; the
# cap is an anti-runaway guard (an agent in a loop), not a product
# limit — revoked addresses do not count.
MAX_ACTIVE_ADDRESSES_PER_PATIENT = 20


class InboxAddressError(ValueError):
    """Domain failure the API maps onto a structured 4xx."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def render_address(address: PatientInboxAddress) -> str:
    """The full deliverable form ``{code}+{tag}@{domain}``."""
    settings = get_settings()
    return f"{address.code}+{address.tag}@{settings.inbound_email_domain}"


async def create_address(
    db: AsyncSession,
    *,
    patient: Patient,
    actor: ReviewActor,
    label: str | None = None,
) -> PatientInboxAddress:
    settings = get_settings()
    if not settings.inbound_email_enabled:
        raise InboxAddressError(
            "inbox.disabled",
            "inbound email is not enabled on this deployment",
        )
    active_count = (
        await db.execute(
            select(func.count())
            .select_from(PatientInboxAddress)
            .where(
                PatientInboxAddress.patient_id == patient.id,
                PatientInboxAddress.active.is_(True),
            )
        )
    ).scalar_one()
    if active_count >= MAX_ACTIVE_ADDRESSES_PER_PATIENT:
        raise InboxAddressError(
            "inbox.address_cap",
            f"patient already has {active_count} active inbox addresses",
        )
    address = PatientInboxAddress(
        id=uuid.uuid4(),
        patient_id=patient.id,
        code=generate_code(settings.inbound_email_code_bits),
        tag=settings.inbound_email_tag,
        label=(label or "").strip() or None,
        active=True,
        created_by_subject_id=actor.subject_id,
    )
    db.add(address)
    await db.flush()
    record_provenance_event(
        db,
        target_kind="inbox_address",
        target_id=address.id,
        activity="create",
        agent_kind=actor.kind,
        agent_subject_id=actor.subject_id,
        agent_token_id=actor.agent_token_id,
        agent_assistant_id=actor.agent_assistant_id,
        diff={"label": address.label},
    )
    return address


async def list_addresses(
    db: AsyncSession, *, patient_id: uuid.UUID, include_revoked: bool = True
) -> list[PatientInboxAddress]:
    stmt = (
        select(PatientInboxAddress)
        .where(PatientInboxAddress.patient_id == patient_id)
        .order_by(PatientInboxAddress.created_at.desc())
    )
    if not include_revoked:
        stmt = stmt.where(PatientInboxAddress.active.is_(True))
    return list((await db.execute(stmt)).scalars().all())


async def get_address(
    db: AsyncSession, *, patient_id: uuid.UUID, address_id: uuid.UUID
) -> PatientInboxAddress | None:
    return (
        await db.execute(
            select(PatientInboxAddress).where(
                PatientInboxAddress.patient_id == patient_id,
                PatientInboxAddress.id == address_id,
            )
        )
    ).scalar_one_or_none()


async def set_label(
    db: AsyncSession,
    *,
    address: PatientInboxAddress,
    actor: ReviewActor,
    label: str | None,
) -> PatientInboxAddress:
    before = address.label
    address.label = (label or "").strip() or None
    address.etag = uuid.uuid4()
    record_provenance_event(
        db,
        target_kind="inbox_address",
        target_id=address.id,
        activity="update",
        agent_kind=actor.kind,
        agent_subject_id=actor.subject_id,
        agent_token_id=actor.agent_token_id,
        agent_assistant_id=actor.agent_assistant_id,
        diff={"label": {"from": before, "to": address.label}},
    )
    return address


async def revoke_address(
    db: AsyncSession,
    *,
    address: PatientInboxAddress,
    actor: ReviewActor,
    reason: str | None = None,
) -> PatientInboxAddress:
    """Burn the capability. Idempotent: revoking a revoked address is a
    no-op (the first revocation's audit row stands)."""
    if not address.active:
        return address
    address.active = False
    address.revoked_at = datetime.now(UTC)
    address.revoke_reason = (reason or "").strip() or None
    address.etag = uuid.uuid4()
    record_provenance_event(
        db,
        target_kind="inbox_address",
        target_id=address.id,
        activity="revoke",
        agent_kind=actor.kind,
        agent_subject_id=actor.subject_id,
        agent_token_id=actor.agent_token_id,
        agent_assistant_id=actor.agent_assistant_id,
        diff={"reason": address.revoke_reason},
    )
    return address


async def resolve_active_code(db: AsyncSession, code: str) -> PatientInboxAddress | None:
    """RCPT routing lookup: the *active* address owning ``code``.

    The caller normalises first (``codes.normalize_code``); a miss —
    unknown or revoked — is a 550 at the MTA, indistinguishable on
    purpose (a revoked code must not confirm it once existed).
    """
    return (
        await db.execute(
            select(PatientInboxAddress).where(
                PatientInboxAddress.code == code,
                PatientInboxAddress.active.is_(True),
            )
        )
    ).scalar_one_or_none()


__all__ = [
    "MAX_ACTIVE_ADDRESSES_PER_PATIENT",
    "InboxAddressError",
    "create_address",
    "get_address",
    "list_addresses",
    "render_address",
    "resolve_active_code",
    "revoke_address",
    "set_label",
]
