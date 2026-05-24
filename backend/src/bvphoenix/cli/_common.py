"""Shared helpers used by every ``bvphoenix-*`` CLI entrypoint.

Until now the same one-liner — look up a user by email, return their
Subject row — lived as a private ``_resolve_owner`` copy in both
``cli/import_dicom`` and ``cli/import_pathology``. Same logic, two
spots, opportunity for drift.

Anything added here must be:
  * usable from a synchronous sqlalchemy ``Session`` (CLIs avoid the
    async stack — they run as one-shot scripts);
  * dependent only on ``db.models``, never on api/ or service code
    that needs FastAPI request context.
"""

from __future__ import annotations

import click
from sqlalchemy import select
from sqlalchemy.orm import Session

from bvphoenix.db.models import Subject, User


def resolve_owner_by_email(session: Session, email: str) -> Subject:
    """Look up the user by email and return the linked Subject row.

    Raises :class:`click.ClickException` when no user matches — the
    CLI surfaces it as a normal exit-1 with the email quoted so the
    operator can spot a typo. Account creation is intentionally out
    of scope: every import CLI requires the owner to already exist.
    """
    row = session.execute(
        select(Subject).join(User, User.subject_id == Subject.id).where(User.email == email)
    ).scalar_one_or_none()
    if row is None:
        raise click.ClickException(f"no user found with email {email!r}")
    return row


__all__ = ["resolve_owner_by_email"]
