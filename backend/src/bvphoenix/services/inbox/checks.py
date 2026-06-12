"""Profile-specific auto-checks for the patient inbox.

The common content-safety floor (ClamAV, magic allowlist, archive
guards, dedup, DICOM routing) lives in
``services/review_queue/plugins``; what is specific to the e-mail
ingress is *sender verification*: SPF / DKIM / DMARC are spoofing
signals for the reviewer — never an SMTP-time gate (a legitimate lab
behind a misconfigured relay must still reach the queue) — and the
only path to auto-accept is an explicit allowlist entry WITH
authentication alignment.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import InboxItem, InboxSenderAllowlist
from bvphoenix.services.review_queue.checks import CheckContext, CheckResult

_PASS = "pass"


class SenderVerifyCheck:
    """Surface the authentication posture of the sending domain.

    Verdicts: ``pass`` when nothing failed, ``warn`` when SPF / DKIM /
    DMARC carries an explicit failure (the reviewer sees *why* in the
    details). Deliberately never ``fail``/``block``: the decision over
    a spoofable-but-plausible sender belongs to the human/agent
    reviewer with context, not to a header heuristic.
    """

    name = "sender_verify"

    async def run(self, ctx: CheckContext) -> CheckResult:
        email_meta = (ctx.staged.manifest or {}).get("email", {})
        results = {
            "spf": email_meta.get("spf"),
            "dkim": email_meta.get("dkim"),
            "dmarc": email_meta.get("dmarc"),
        }
        failures = [mech for mech, outcome in results.items() if outcome == "fail"]
        details = {
            "from": email_meta.get("from"),
            **results,
            "failures": failures,
            "auto_submitted": bool(email_meta.get("auto_submitted")),
        }
        return CheckResult(verdict="warn" if failures else _PASS, details=details)


def _is_aligned(spf: str | None, dkim: str | None) -> bool:
    """Authentication alignment for auto-accept: at least one of SPF /
    DKIM must explicitly pass. (Stricter DMARC-style domain alignment
    is not computable here — the MTA records upstream results, it does
    not re-verify — so an explicit mechanism pass is the floor.)"""
    return spf == "pass" or dkim == "pass"


async def auto_accept_entry(db: AsyncSession, item: InboxItem) -> InboxSenderAllowlist | None:
    """The allowlist entry authorising this item to skip human review,
    or ``None``.

    Conditions: the sender is on the patient's active allowlist AND
    (unless the entry deliberately opts out) authentication is aligned
    AND every auto-check came back a clean ``pass`` — a single warn
    (dedup hit, SPF failure) keeps the item queued. The bare ``From``
    header alone is never sufficient. The entry is returned (not a
    bool) because the auto-accept decision is *attributed to the human
    who created it*: allowlisting a sender is the decision, taken in
    advance — the worker merely executes it.
    """
    if item.source_channel != "email":
        return None
    if item.auto_verdict != "pass":
        return None
    email_meta = (item.manifest or {}).get("email", {})
    if email_meta.get("auto_submitted"):
        return None
    sender = (email_meta.get("from") or "").lower()
    if not sender:
        return None
    entry = (
        await db.execute(
            select(InboxSenderAllowlist).where(
                InboxSenderAllowlist.patient_id == item.patient_id,
                InboxSenderAllowlist.sender_email == sender,
                InboxSenderAllowlist.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if entry is None or entry.created_by_subject_id is None:
        return None
    if entry.require_alignment and not _is_aligned(email_meta.get("spf"), email_meta.get("dkim")):
        return None
    return entry


__all__ = ["SenderVerifyCheck", "auto_accept_entry"]
