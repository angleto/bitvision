"""`bvphoenix-admin` — admin & bootstrap CLI.

The first user on a fresh deployment must be created out-of-band; the
``/api/auth/register`` route always creates regular users. After the
first admin exists, further admins can be promoted from the API or the
Postgres console.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import click
import pyotp
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bvphoenix.auth import hash_password
from bvphoenix.config import get_settings
from bvphoenix.db.models import Subject, User


@click.group()
def main() -> None:
    """Admin operations for bitvision phoenix."""


@main.command("create-user")
@click.option("--email", required=True)
@click.option("--password", required=True)
@click.option("--display-name", required=True)
@click.option("--admin", is_flag=True, help="Grant admin role.")
def create_user(email: str, password: str, display_name: str, admin: bool) -> None:
    """Create a user. Use --admin to grant the admin role."""
    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    email_norm = email.strip().lower()
    with Session(engine) as session:
        existing = session.execute(
            select(User).where(User.email == email_norm)
        ).scalar_one_or_none()
        if existing is not None:
            click.echo(f"user {email_norm!r} already exists", err=True)
            sys.exit(1)

        subject = Subject(kind="user", display_name=display_name)
        session.add(subject)
        session.flush()
        user = User(
            subject_id=subject.id,
            email=email_norm,
            password_hash=hash_password(password),
            is_admin=admin,
        )
        session.add(user)
        session.commit()
        click.echo(f"created user {email_norm} (subject_id={subject.id}, admin={admin})")


@main.command("promote")
@click.option("--email", required=True)
def promote(email: str) -> None:
    """Promote an existing user to admin."""
    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    with Session(engine) as session:
        user = session.execute(
            select(User).where(User.email == email.strip().lower())
        ).scalar_one_or_none()
        if user is None:
            click.echo(f"no user with email {email!r}", err=True)
            sys.exit(1)
        user.is_admin = True
        session.commit()
        click.echo(f"{user.email} is now admin")


@main.command("mfa-bootstrap")
@click.option("--email", required=True)
def mfa_bootstrap(email: str) -> None:
    """Mint a TOTP secret for an admin (bootstrap only).

    Since ``BVP_REQUIRE_MFA_FOR_ADMIN`` refuses plain-password admin
    login, the first admin on a fresh deployment needs an out-of-band
    enrolment path: run this, paste the URI into an authenticator app,
    then log in via ``/api/auth/login-mfa``. See docs/security-mfa.md.
    """
    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    email_norm = email.strip().lower()
    with Session(engine) as session:
        user = session.execute(select(User).where(User.email == email_norm)).scalar_one_or_none()
        if user is None:
            click.echo(f"no user with email {email!r}", err=True)
            sys.exit(1)
        if user.mfa_enabled_at is not None:
            click.echo(f"{email_norm}: MFA already active", err=True)
            sys.exit(1)
        secret = pyotp.random_base32()
        user.mfa_secret = secret
        user.mfa_enabled_at = datetime.now(UTC)
        user.backup_codes_hash = None
        session.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=email_norm,
        issuer_name=settings.mfa_issuer,
    )
    click.echo(f"MFA bootstrapped for {email_norm}")
    click.echo(f"secret: {secret}")
    click.echo(f"otpauth URI: {uri}")
    click.echo("Import the URI into your authenticator, then call /api/auth/login-mfa.")


if __name__ == "__main__":
    main()
