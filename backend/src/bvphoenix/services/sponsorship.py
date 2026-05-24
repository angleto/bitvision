"""Wallet sponsorship: cross-subject billing resolver and CRUD.

The resolver answers: when caller X is about to spend Y cents on
resource R, whose wallet is debited and against which sponsorship?

Specificity ordering returns the most specific applicable sponsorship:
``consultation > patient > organization > global``. Within the same
specificity tier the most recently created sponsorship wins (mirrors
the standard "last-grant-wins" pattern in the codebase).

Cap accounting is atomic: ``consume_sponsorship`` runs a
``SELECT ... FOR UPDATE`` on the sponsorship row, verifies
``spent_cents + amount <= cap_cents``, and increments ``spent_cents``
inside the same transaction the caller will use to write the ledger
row. A concurrent debit on the same sponsorship is therefore
serialised, and the second contender that would overflow the cap is
refused before any ledger row is materialised.

The resolver does NOT include BYOK precedence: callers (e.g.
``billing.debit_llm_call``) decide BYOK first; only when they are
about to write a ledger debit do they consult this resolver.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models.app_settings import AppSetting
from bvphoenix.db.models.wallet_sponsorships import (
    SCOPE_KINDS,
    SCOPE_SPECIFICITY,
    WalletSponsorship,
    WalletSponsorshipAudit,
)

logger = logging.getLogger(__name__)


KEY_DEFAULT_CAP_CENTS = "sponsorship.default_cap_cents"
KEY_MAX_CAP_CENTS = "sponsorship.max_cap_cents"
DEFAULT_CAP_CENTS = 500  # €5 fallback when nothing is configured.


class SponsorshipError(RuntimeError):
    """Base for sponsorship operation failures (cap exceeded, revoked, ...)."""


class CapExceededError(SponsorshipError):
    def __init__(
        self, sponsorship_id: uuid.UUID, cap_cents: int, spent_cents: int, requested_cents: int
    ) -> None:
        self.sponsorship_id = sponsorship_id
        self.cap_cents = cap_cents
        self.spent_cents = spent_cents
        self.requested_cents = requested_cents
        super().__init__(
            f"sponsorship {sponsorship_id} cap exceeded: "
            f"spent={spent_cents} + requested={requested_cents} > cap={cap_cents}"
        )


class CapCeilingError(SponsorshipError):
    def __init__(self, requested_cents: int, ceiling_cents: int) -> None:
        self.requested_cents = requested_cents
        self.ceiling_cents = ceiling_cents
        super().__init__(
            f"requested cap {requested_cents} exceeds workspace ceiling {ceiling_cents}"
        )


@dataclass(slots=True, frozen=True)
class BillingTarget:
    """Outcome of ``resolve_billing``.

    ``billed_subject_id == caller_subject_id`` and ``sponsorship is None``
    means self-pay (legacy behaviour). Otherwise the call is sponsored
    and ``sponsorship`` carries the row that authorised it (callers must
    invoke ``consume_sponsorship`` to advance the cap before booking the
    ledger debit)."""

    billed_subject_id: uuid.UUID
    caller_subject_id: uuid.UUID
    sponsorship: WalletSponsorship | None

    @property
    def is_sponsored(self) -> bool:
        return self.sponsorship is not None


@dataclass(slots=True, frozen=True)
class ScopeMatch:
    """One scope to consider in resolution. Order does not matter; the
    resolver itself sorts by ``SCOPE_SPECIFICITY``."""

    scope_kind: str
    scope_id: uuid.UUID | None  # NULL only for ``scope_kind='global'``


# ---------------------------------------------------------------------------
# Workspace settings
# ---------------------------------------------------------------------------


async def _read_int_setting(db: AsyncSession, key: str) -> int | None:
    row = await db.execute(select(AppSetting).where(AppSetting.key == key))
    setting = row.scalar_one_or_none()
    if setting is None:
        return None
    raw = setting.value
    try:
        if isinstance(raw, dict):
            for k in ("value", "cents"):
                if k in raw:
                    inner = raw[k]
                    if isinstance(inner, (int, str, float)):
                        return int(inner)
            return None
        if isinstance(raw, (int, str, float)):
            return int(raw)
        return None
    except (TypeError, ValueError):
        logger.warning("malformed app_settings %s = %r", key, raw)
        return None


async def get_default_cap_cents(db: AsyncSession) -> int:
    """Workspace fallback for new sponsorships when the creator does not
    specify a cap. Hard-coded floor: ``DEFAULT_CAP_CENTS``."""
    cents = await _read_int_setting(db, KEY_DEFAULT_CAP_CENTS)
    return cents if cents and cents > 0 else DEFAULT_CAP_CENTS


async def get_cap_ceiling_cents(db: AsyncSession) -> int | None:
    """Optional workspace-wide upper bound on cap_cents. ``None`` means
    no ceiling enforced."""
    cents = await _read_int_setting(db, KEY_MAX_CAP_CENTS)
    return cents if cents and cents > 0 else None


async def _enforce_ceiling(db: AsyncSession, cap_cents: int) -> None:
    ceiling = await get_cap_ceiling_cents(db)
    if ceiling is not None and cap_cents > ceiling:
        raise CapCeilingError(requested_cents=cap_cents, ceiling_cents=ceiling)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create_sponsorship(
    db: AsyncSession,
    *,
    sponsor_subject_id: uuid.UUID,
    sponsored_subject_id: uuid.UUID,
    scope_kind: str,
    scope_id: uuid.UUID | None,
    cap_cents: int | None = None,
    valid_until: datetime | None = None,
    purpose: str | None = None,
) -> WalletSponsorship:
    """Insert a new sponsorship row authorised by the sponsor.

    Caller is responsible for verifying that ``sponsor_subject_id`` is
    the authenticated principal (only the wallet owner may emit
    sponsorships on their wallet)."""
    if scope_kind not in SCOPE_KINDS:
        raise ValueError(f"unknown scope_kind: {scope_kind}")
    if scope_kind == "global":
        if scope_id is not None:
            raise ValueError("scope_id must be NULL for scope_kind='global'")
    elif scope_id is None:
        raise ValueError(f"scope_id required for scope_kind={scope_kind}")
    if sponsor_subject_id == sponsored_subject_id:
        raise ValueError("sponsor and sponsored must differ")

    effective_cap = cap_cents if cap_cents and cap_cents > 0 else await get_default_cap_cents(db)
    await _enforce_ceiling(db, effective_cap)

    row = WalletSponsorship(
        sponsor_subject_id=sponsor_subject_id,
        sponsored_subject_id=sponsored_subject_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        cap_cents=effective_cap,
        valid_until=valid_until,
        purpose=purpose,
    )
    db.add(row)
    await db.flush()
    db.add(
        WalletSponsorshipAudit(
            sponsorship_id=row.id,
            actor_subject_id=sponsor_subject_id,
            action="created",
            after_cap_cents=effective_cap,
            notes={"scope_kind": scope_kind, "scope_id": str(scope_id) if scope_id else None},
        )
    )
    await db.flush()
    return row


async def update_cap(
    db: AsyncSession,
    *,
    sponsorship_id: uuid.UUID,
    actor_subject_id: uuid.UUID,
    new_cap_cents: int,
) -> WalletSponsorship:
    """Raise or lower the cap. ``actor_subject_id`` must match
    ``sponsor_subject_id`` on the row (caller-side check). Lowering
    below ``spent_cents`` is allowed and effectively exhausts the
    sponsorship."""
    if new_cap_cents <= 0:
        raise ValueError("cap must be positive")
    await _enforce_ceiling(db, new_cap_cents)

    row = await db.get(WalletSponsorship, sponsorship_id, with_for_update=True)
    if row is None:
        raise LookupError(f"sponsorship {sponsorship_id} not found")
    if row.sponsor_subject_id != actor_subject_id:
        raise PermissionError("only the sponsor may modify the cap")
    if row.revoked_at is not None:
        raise SponsorshipError("sponsorship is revoked, cannot modify")

    before = int(row.cap_cents)
    row.cap_cents = int(new_cap_cents)
    action = "cap_raised" if new_cap_cents > before else "cap_lowered"
    db.add(
        WalletSponsorshipAudit(
            sponsorship_id=row.id,
            actor_subject_id=actor_subject_id,
            action=action,
            before_cap_cents=before,
            after_cap_cents=int(new_cap_cents),
        )
    )
    await db.flush()
    return row


async def revoke_sponsorship(
    db: AsyncSession,
    *,
    sponsorship_id: uuid.UUID,
    actor_subject_id: uuid.UUID,
) -> WalletSponsorship:
    """Revoke a sponsorship. Only the sponsor may revoke."""
    row = await db.get(WalletSponsorship, sponsorship_id, with_for_update=True)
    if row is None:
        raise LookupError(f"sponsorship {sponsorship_id} not found")
    if row.sponsor_subject_id != actor_subject_id:
        raise PermissionError("only the sponsor may revoke")
    if row.revoked_at is not None:
        return row  # already revoked, no-op idempotent

    row.revoked_at = datetime.now(tz=row.created_at.tzinfo) if row.created_at else None
    row.revoked_by_subject_id = actor_subject_id
    db.add(
        WalletSponsorshipAudit(
            sponsorship_id=row.id,
            actor_subject_id=actor_subject_id,
            action="revoked",
            before_cap_cents=int(row.cap_cents),
            after_cap_cents=int(row.cap_cents),
        )
    )
    await db.flush()
    return row


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _scope_match_predicate(scopes: list[ScopeMatch]):
    """Build the OR predicate matching any of the provided scopes."""
    parts = []
    for sc in scopes:
        if sc.scope_kind == "global":
            parts.append(WalletSponsorship.scope_kind == "global")
        else:
            parts.append(
                and_(
                    WalletSponsorship.scope_kind == sc.scope_kind,
                    WalletSponsorship.scope_id == sc.scope_id,
                )
            )
    if not parts:
        return None
    return or_(*parts)


async def resolve_billing(
    db: AsyncSession,
    *,
    caller_subject_id: uuid.UUID,
    scopes: list[ScopeMatch],
    estimated_cents: int,
    now: datetime | None = None,
) -> BillingTarget:
    """Pick the wallet that pays for a call about to be made.

    ``scopes`` is the list of resources the call belongs to (e.g. for a
    Q&A on patient X within consultation C the caller passes
    ``[ScopeMatch('consultation', C), ScopeMatch('patient', X), ScopeMatch('global', None)]``;
    the resolver picks the most specific match with cap headroom).

    Returns ``BillingTarget(self-pay)`` when no sponsorship matches; the
    caller decides whether to proceed (default behaviour) or to refuse
    based on its own policy."""
    predicate = _scope_match_predicate(scopes)
    if predicate is None:
        return BillingTarget(
            billed_subject_id=caller_subject_id,
            caller_subject_id=caller_subject_id,
            sponsorship=None,
        )

    now = now or datetime.now(tz=None)
    stmt = (
        select(WalletSponsorship)
        .where(
            WalletSponsorship.sponsored_subject_id == caller_subject_id,
            WalletSponsorship.revoked_at.is_(None),
            WalletSponsorship.valid_from <= now,
            or_(WalletSponsorship.valid_until.is_(None), WalletSponsorship.valid_until > now),
            (WalletSponsorship.cap_cents - WalletSponsorship.spent_cents) >= estimated_cents,
            predicate,
        )
        .order_by(WalletSponsorship.created_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return BillingTarget(
            billed_subject_id=caller_subject_id,
            caller_subject_id=caller_subject_id,
            sponsorship=None,
        )

    # Most-specific scope wins; ties broken by recency (the SQL ORDER BY
    # already gave us recency-desc within scope_kind).
    rows_sorted = sorted(rows, key=lambda r: SCOPE_SPECIFICITY.get(r.scope_kind, 99))
    chosen = rows_sorted[0]
    return BillingTarget(
        billed_subject_id=chosen.sponsor_subject_id,
        caller_subject_id=caller_subject_id,
        sponsorship=chosen,
    )


async def consume_sponsorship(
    db: AsyncSession,
    *,
    sponsorship_id: uuid.UUID,
    amount_cents: int,
) -> int:
    """Atomically advance ``spent_cents`` on the sponsorship row.

    Uses ``SELECT ... FOR UPDATE`` to serialise concurrent debits on the
    same sponsorship. Returns the new ``spent_cents``. Raises
    :class:`CapExceededError` when the projected total would overflow
    the cap; the caller must abort the ledger debit in that case."""
    if amount_cents <= 0:
        raise ValueError("amount must be positive")

    row = await db.get(WalletSponsorship, sponsorship_id, with_for_update=True)
    if row is None:
        raise LookupError(f"sponsorship {sponsorship_id} not found")
    if row.revoked_at is not None:
        raise SponsorshipError(f"sponsorship {sponsorship_id} is revoked")

    cap = int(row.cap_cents)
    spent = int(row.spent_cents)
    projected = spent + int(amount_cents)
    if projected > cap:
        raise CapExceededError(
            sponsorship_id=sponsorship_id,
            cap_cents=cap,
            spent_cents=spent,
            requested_cents=int(amount_cents),
        )
    # Execute via UPDATE so the row is touched even when the ORM session
    # gets flushed lazily; the FOR UPDATE lock protects the read-modify-write.
    await db.execute(
        update(WalletSponsorship)
        .where(WalletSponsorship.id == sponsorship_id)
        .values(spent_cents=projected)
    )
    return projected


# ---------------------------------------------------------------------------
# Listing helpers (used by API)
# ---------------------------------------------------------------------------


async def list_emitted(
    db: AsyncSession,
    *,
    sponsor_subject_id: uuid.UUID,
    include_revoked: bool = False,
) -> list[WalletSponsorship]:
    stmt = select(WalletSponsorship).where(
        WalletSponsorship.sponsor_subject_id == sponsor_subject_id
    )
    if not include_revoked:
        stmt = stmt.where(WalletSponsorship.revoked_at.is_(None))
    stmt = stmt.order_by(WalletSponsorship.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def list_received(
    db: AsyncSession,
    *,
    sponsored_subject_id: uuid.UUID,
    include_revoked: bool = False,
) -> list[WalletSponsorship]:
    stmt = select(WalletSponsorship).where(
        WalletSponsorship.sponsored_subject_id == sponsored_subject_id
    )
    if not include_revoked:
        stmt = stmt.where(WalletSponsorship.revoked_at.is_(None))
    stmt = stmt.order_by(WalletSponsorship.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


__all__ = [
    "DEFAULT_CAP_CENTS",
    "KEY_DEFAULT_CAP_CENTS",
    "KEY_MAX_CAP_CENTS",
    "SCOPE_KINDS",
    "BillingTarget",
    "CapCeilingError",
    "CapExceededError",
    "ScopeMatch",
    "SponsorshipError",
    "consume_sponsorship",
    "create_sponsorship",
    "get_cap_ceiling_cents",
    "get_default_cap_cents",
    "list_emitted",
    "list_received",
    "resolve_billing",
    "revoke_sponsorship",
    "update_cap",
]
