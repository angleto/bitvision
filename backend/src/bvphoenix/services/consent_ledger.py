"""Patient-visible consent ledger: the append-only grant/revoke history.

Derived from the authoritative consent rows, never a second copy of them.
The two consent tables are already append-only *episode* ledgers:

* :class:`~bvphoenix.db.models.gdpr.Consent` (account-level GDPR consents
  like ``research_use`` / ``ai_training``). ``api.gdpr.record_consent``
  inserts a fresh row on grant when none is open and stamps ``revoked_at``
  once on revoke; nothing ever resets ``revoked_at`` or resurrects a
  revoked row.
* :class:`~bvphoenix.db.models.training_consents.TrainingConsent` (per-study
  T3/T4 opt-ins). ``services.consent_auto`` follows the same posture:
  revoke stamps ``revoked_at``, a re-grant inserts a new row.

So exploding every row into its grant event (``granted_at``) and, when
present, its revoke event (``revoked_at``) reconstructs the full timeline
with zero loss. Crucially we read the *same* rows that gate processing —
``services.training_cohort.select_cohort`` filters
``TrainingConsent.revoked_at IS NULL`` — so the ledger cannot drift from
what actually governs data use. That is the whole point: GDPR Art. 7(1)
demonstrable consent, and a revoke whose effect is verifiable, not a
parallel audit copy that can silently disagree with reality.

A separate ``consent_events`` table was deliberately NOT added: it would
duplicate ``granted_at`` / ``revoked_at`` already on the rows, require a
dual-write at every grant/revoke site (a missed hook = a ledger that
lies), and need a backfill that would read exactly these rows anyway.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models.gdpr import CONSENT_KINDS, REQUIRED_CONSENT_KINDS, Consent
from bvphoenix.db.models.training_consents import TrainingConsent

# Honest framing, surfaced verbatim to the patient (mirrors the de-id
# provenance panel's ``scope`` line). Says exactly what a revoke does and
# does not do, so the ledger never overclaims.
LEDGER_SCOPE = (
    "Append-only history of every consent grant and revoke on your account, "
    "derived from the authoritative consent records that govern how your data "
    "may be used. It covers account-level GDPR consents (e.g. research use, AI "
    "model training) and per-study training opt-ins (tiers T3 / T4). A revoke "
    "takes effect immediately and excludes the affected studies from any "
    "future training cohort; it does not retroactively unwind cohorts already "
    "assembled and exported before the revoke. Per GDPR Art. 7(1) this is your "
    "point-in-time proof of consent."
)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def collapse_account_consents(rows: Sequence[Consent]) -> list[dict[str, Any]]:
    """Current state per account consent kind, collapsing the append-only
    rows to the latest episode (by ``granted_at``).

    Required kinds with no row are reported as implicitly granted —
    acceptance is a precondition of account creation. This is the single
    source of the collapse logic, shared with ``api.gdpr.list_consents``.
    """
    latest: dict[str, Consent] = {}
    for row in sorted(rows, key=lambda r: r.granted_at, reverse=True):
        latest.setdefault(row.kind, row)

    out: list[dict[str, Any]] = []
    for kind in CONSENT_KINDS:
        row = latest.get(kind)
        if row is None:
            out.append(
                {
                    "kind": kind,
                    "granted": kind in REQUIRED_CONSENT_KINDS,
                    "granted_at": None,
                    "revoked_at": None,
                }
            )
        else:
            granted = row.revoked_at is None
            out.append(
                {
                    "kind": kind,
                    "granted": granted,
                    "granted_at": _iso(row.granted_at) if granted else None,
                    "revoked_at": _iso(row.revoked_at),
                }
            )
    return out


def _account_entries(rows: Sequence[Consent]) -> list[tuple[datetime, str, dict[str, Any]]]:
    entries: list[tuple[datetime, str, dict[str, Any]]] = []
    for row in rows:
        base = {
            "scope": "account",
            "kind": row.kind,
            "tier": None,
            "study_id": None,
            "consent_version": None,
            "consent_hash": None,
            "reason": None,
        }
        entries.append(
            (row.granted_at, "granted", {**base, "at": _iso(row.granted_at), "action": "granted"})
        )
        if row.revoked_at is not None:
            entries.append(
                (
                    row.revoked_at,
                    "revoked",
                    {**base, "at": _iso(row.revoked_at), "action": "revoked"},
                )
            )
    return entries


def _study_entries(rows: Sequence[TrainingConsent]) -> list[tuple[datetime, str, dict[str, Any]]]:
    entries: list[tuple[datetime, str, dict[str, Any]]] = []
    for row in rows:
        base = {
            "scope": "study",
            "kind": None,
            "tier": row.tier,
            "study_id": str(row.study_id),
            "consent_version": row.consent_version,
            "consent_hash": row.consent_hash,
        }
        entries.append(
            (
                row.granted_at,
                "granted",
                {**base, "at": _iso(row.granted_at), "action": "granted", "reason": None},
            )
        )
        if row.revoked_at is not None:
            entries.append(
                (
                    row.revoked_at,
                    "revoked",
                    {
                        **base,
                        "at": _iso(row.revoked_at),
                        "action": "revoked",
                        "reason": row.revoke_reason,
                    },
                )
            )
    return entries


def _active_at(
    consent_rows: Sequence[Consent],
    training_rows: Sequence[TrainingConsent],
    ts: datetime,
) -> dict[str, Any]:
    """Consent state in effect at instant ``ts`` — the point-in-time proof.

    An episode was active at ``ts`` iff ``granted_at <= ts`` and the revoke
    had not yet happened (``revoked_at`` is null or strictly after ``ts``).
    """
    account = []
    for kind in CONSENT_KINDS:
        active = any(
            r.kind == kind and r.granted_at <= ts and (r.revoked_at is None or r.revoked_at > ts)
            for r in consent_rows
        )
        if not active and kind in REQUIRED_CONSENT_KINDS:
            # Required consents are implicit from account creation.
            active = True
        account.append({"kind": kind, "granted": active})

    active_study = sum(
        1
        for r in training_rows
        if r.granted_at <= ts and (r.revoked_at is None or r.revoked_at > ts)
    )
    return {"account": account, "active_study_consents": active_study}


async def build_consent_ledger(
    db: AsyncSession,
    user_subject_id: uuid.UUID,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the caller's consent ledger from the authoritative rows.

    Returns a JSON-serialisable dict: the full chronological event list
    (most recent first), the current per-kind account state, the
    currently-active per-study training consents, and — when ``as_of`` is
    given — the consent state that was in effect at that instant.
    """
    consent_rows = (
        (await db.execute(select(Consent).where(Consent.user_subject_id == user_subject_id)))
        .scalars()
        .all()
    )
    training_rows = (
        (
            await db.execute(
                select(TrainingConsent).where(TrainingConsent.user_subject_id == user_subject_id)
            )
        )
        .scalars()
        .all()
    )

    entries = _account_entries(consent_rows) + _study_entries(training_rows)
    # Most recent first; a stable secondary key on the action keeps a
    # grant and revoke stamped at the same instant deterministic.
    entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
    events = [e[2] for e in entries]

    active_study_consents = sorted(
        (
            {
                "study_id": str(r.study_id),
                "tier": r.tier,
                "granted_at": _iso(r.granted_at),
                "consent_version": r.consent_version,
                "consent_hash": r.consent_hash,
            }
            for r in training_rows
            if r.revoked_at is None
        ),
        key=lambda s: s["granted_at"] or "",
        reverse=True,
    )

    out: dict[str, Any] = {
        "subject_id": str(user_subject_id),
        "generated_at": datetime.now(UTC).isoformat(),
        "account_consents": collapse_account_consents(consent_rows),
        "active_study_consents": active_study_consents,
        "events": events,
        "scope": LEDGER_SCOPE,
        "as_of": None,
        "as_of_state": None,
    }
    if as_of is not None:
        out["as_of"] = as_of.isoformat()
        out["as_of_state"] = _active_at(consent_rows, training_rows, as_of)
    return out
