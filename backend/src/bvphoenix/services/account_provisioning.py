"""Bring a new local account into a state it can actually log in from.

Why this is a service and not a helper inside ``api/auth``
---------------------------------------------------------

There is more than one way to become a user of this platform:

* ``POST /api/auth/register`` — somebody signs themselves up;
* ``POST /api/share-links/{token}/claim`` and
  ``POST /api/share-sessions/claim`` — the recipient of an addressed
  share link turns their bearer access into a real account;
* ``cli/admin.py`` — an operator provisions one.

Everything a *usable* account needs was implemented on the first of
those and nowhere else, and that turned out to be a defect with teeth.
``require_email_verification`` is on in production, and the claim path
created its user with ``email_verified_at`` NULL and issued no
verification token, so ``POST /api/auth/login`` answered 403 to every
subsequent attempt while ``/api/auth/verify-email`` had no token to
consume. The account was locked out permanently, from the moment it was
created, with a correct password. One real person hit this — the
patient herself, on her own health record — and the only reason it
looked intermittent is that the claim response hands back a session, so
the first twelve hours worked.

So the two things a new local account needs are stated once, here, and
every creation path calls them:

:func:`start_email_verification` — mint a single-use token, put a
message on the delivery ledger, and hand the caller the background job
to run. The ledger is what turns a relay outage into a delayed
verification rather than a dead account.

:func:`record_required_consents` — persist the ToS / privacy rows.
Acceptance is implicit in account creation (see
``db.models.gdpr.REQUIRED_CONSENT_KINDS``); the rows exist so the
consent ledger reads from real records rather than synthesising them.

Both flush; the caller commits. Both are idempotent enough to call on a
path that may run twice: consents are skipped when already present, and
an extra verification token is harmless (single-use, same account).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import Settings
from bvphoenix.db.models import EmailVerificationToken, User
from bvphoenix.db.models.gdpr import REQUIRED_CONSENT_KINDS, Consent
from bvphoenix.services.email import EmailMessage, build_verification_email
from bvphoenix.services.email_delivery import enqueue

logger = logging.getLogger(__name__)


def hash_verification_token(raw: str) -> str:
    """SHA-256 hex digest — what is persisted.

    The plaintext leaves via email and is never stored, so a database
    snapshot cannot be replayed into a verification.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_verification_url(raw_token: str, settings: Settings) -> str:
    return (
        f"{settings.frontend_base_url.rstrip('/')}/verify-email?{urlencode({'token': raw_token})}"
    )


async def issue_verification_token(db: AsyncSession, user: User, settings: Settings) -> str:
    """Persist a fresh single-use verification token, return the raw value.

    The raw value is never logged and never stored — only its hash.
    """
    raw = secrets.token_urlsafe(32)
    db.add(
        EmailVerificationToken(
            user_subject_id=user.subject_id,
            token_hash=hash_verification_token(raw),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=settings.email_verification_ttl_seconds),
        )
    )
    await db.flush()
    return raw


@dataclass(frozen=True)
class PendingVerification:
    """A queued verification mail the caller still has to dispatch.

    The dispatch is deliberately not done here: it is slow (an SMTP
    round-trip) and belongs on a background task so it cannot delay the
    HTTP response, while the ledger row must be committed in the same
    transaction as the token it verifies.
    """

    delivery_id: uuid.UUID
    message: EmailMessage


async def start_email_verification(
    db: AsyncSession, *, user: User, settings: Settings
) -> PendingVerification | None:
    """Mint a token and queue its email. Returns the job for the caller.

    ``None`` when the address is already verified — re-verifying is not
    a thing, and mailing a link nobody needs is noise at best.
    """
    if user.email_verified_at is not None:
        return None
    raw_token = await issue_verification_token(db, user, settings)
    message = build_verification_email(
        to=user.email, token_url=build_verification_url(raw_token, settings)
    )
    delivery = await enqueue(
        db,
        purpose="email_verification",
        recipient_email=user.email,
        subject_line=message.subject,
        subject_id=user.subject_id,
    )
    await db.flush()
    return PendingVerification(delivery_id=delivery.id, message=message)


async def record_required_consents(db: AsyncSession, *, user: User, source: str) -> None:
    """Persist the ToS / privacy consent rows for a brand-new account.

    ``source`` names the flow that created the account
    (``"registration"``, ``"share_link_claim"``, ...) so the consent
    ledger can say where the acceptance came from. Existing rows are
    left alone: a consent already recorded is not re-granted by a code
    path running twice.
    """
    existing = set(
        (
            await db.execute(
                select(Consent.kind).where(
                    Consent.user_subject_id == user.subject_id,
                    Consent.kind.in_(tuple(REQUIRED_CONSENT_KINDS)),
                )
            )
        )
        .scalars()
        .all()
    )
    for kind in REQUIRED_CONSENT_KINDS:
        if kind in existing:
            continue
        db.add(
            Consent(
                user_subject_id=user.subject_id,
                kind=kind,
                metadata_={"source": source},
            )
        )
    await db.flush()


__all__ = [
    "PendingVerification",
    "build_verification_url",
    "hash_verification_token",
    "issue_verification_token",
    "record_required_consents",
    "start_email_verification",
]
