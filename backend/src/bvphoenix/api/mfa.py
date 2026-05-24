"""MFA / TOTP enrolment + enforcement (docs/security-mfa.md).

Flow
----
1. ``POST /api/mfa/setup`` — authenticated user mints a TOTP secret (or
   re-uses a pending one) and receives a provisioning URI + QR PNG.
   ``mfa_enabled_at`` stays NULL; ``mfa_secret`` becomes non-NULL.
2. ``POST /api/mfa/activate {totp_code}`` — verifies the code, stamps
   ``mfa_enabled_at = now()`` and returns 10 one-shot backup codes. The
   codes are stored as bcrypt hashes in ``backup_codes_hash``.
3. ``POST /api/mfa/disable {totp_code}`` — requires a currently-valid
   TOTP (or backup code) and clears every MFA column.
4. ``POST /api/auth/login-mfa`` — combined login when MFA is active.
   ``POST /api/auth/login`` returns 401 with ``detail = "mfa_required"``
   if a user with active MFA omits the TOTP, so the frontend can switch
   to the MFA form mid-flow.

Admin enforcement: when ``BVP_REQUIRE_MFA_FOR_ADMIN`` is true (default),
the plain ``/login`` refuses admins that have not activated MFA — they
must complete setup out-of-band first. This is intentionally strict:
admins who can't yet enrol are issued a first login through CLI
bootstrap which should force enrolment immediately.
"""

from __future__ import annotations

import base64
import io
import secrets
import string
from datetime import UTC, datetime
from typing import Annotated

import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import (
    hash_password,
    issue_access_token,
    require_user,
    verify_password,
)
from bvphoenix.config import get_settings
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db

router = APIRouter(tags=["mfa"])

# Backup-code shape — 8 alphanumeric chars. Users see each code exactly
# once at activation; consumed entries are removed from the array.
BACKUP_CODE_ALPHABET = string.ascii_uppercase + string.digits
BACKUP_CODE_LENGTH = 8
BACKUP_CODE_COUNT = 10

# TOTP verification window in 30s steps. 1 covers typical clock skew.
TOTP_VALID_WINDOW = 1


def _generate_backup_code() -> str:
    return "".join(secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))


def _qr_png_base64(data: str) -> str:
    """Return a base64-encoded PNG of ``data`` as a QR code."""
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def verify_totp(secret: str | None, code: str) -> bool:
    """Constant-ish-time verify. Returns False for missing secret."""
    if not secret:
        return False
    code = (code or "").strip()
    if not code:
        return False
    totp = pyotp.TOTP(secret)
    return bool(totp.verify(code, valid_window=TOTP_VALID_WINDOW))


def _consume_backup_code(user: User, code: str) -> bool:
    """If ``code`` matches one of the user's backup-code hashes, remove
    the hash from the array and return True. Returns False otherwise."""
    hashes = user.backup_codes_hash or []
    if not hashes:
        return False
    normalized = code.strip().upper()
    for i, h in enumerate(hashes):
        if verify_password(normalized, h):
            # SQLAlchemy needs a new list for mutation tracking on
            # ARRAY columns — in-place edits don't flip is_modified.
            user.backup_codes_hash = hashes[:i] + hashes[i + 1 :]
            return True
    return False


def verify_mfa_code(user: User, code: str) -> bool:
    """Verify either a TOTP code or a backup code. Consumes the backup
    code if one is matched. Caller must ``await db.commit()``."""
    if verify_totp(user.mfa_secret, code):
        return True
    return _consume_backup_code(user, code)


# ---------------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------------


class MfaSetupOut(BaseModel):
    provisioning_uri: str
    qr_png_base64: str
    secret: str = Field(description="Base32 TOTP secret — only shown during setup.")


class MfaActivateIn(BaseModel):
    totp_code: str = Field(min_length=6, max_length=10)


class MfaActivateOut(BaseModel):
    backup_codes: list[str] = Field(description="Shown once; store securely.")
    enabled_at: datetime


class MfaDisableIn(BaseModel):
    totp_code: str = Field(min_length=6, max_length=10)


class MfaStatusOut(BaseModel):
    enabled: bool
    pending: bool
    enabled_at: datetime | None
    backup_codes_remaining: int


class LoginMfaIn(BaseModel):
    email: EmailStr
    password: str
    totp_code: str = Field(min_length=6, max_length=10)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.get("/mfa/status", response_model=MfaStatusOut)
async def mfa_status(user: Annotated[User, Depends(require_user)]) -> MfaStatusOut:
    return MfaStatusOut(
        enabled=user.mfa_enabled_at is not None,
        pending=user.mfa_secret is not None and user.mfa_enabled_at is None,
        enabled_at=user.mfa_enabled_at,
        backup_codes_remaining=len(user.backup_codes_hash or []),
    )


@router.post("/mfa/setup", response_model=MfaSetupOut)
async def mfa_setup(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MfaSetupOut:
    """Start (or restart) TOTP enrolment.

    If MFA is already active we refuse — the user must disable first.
    Otherwise we (re)generate a secret and return the provisioning URI.
    """
    if user.mfa_enabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="mfa_already_enabled",
        )

    secret = pyotp.random_base32()
    user.mfa_secret = secret
    user.backup_codes_hash = None
    await db.commit()

    settings = get_settings()
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email,
        issuer_name=settings.mfa_issuer,
    )
    return MfaSetupOut(
        provisioning_uri=uri,
        qr_png_base64=_qr_png_base64(uri),
        secret=secret,
    )


@router.post("/mfa/activate", response_model=MfaActivateOut)
async def mfa_activate(
    body: MfaActivateIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MfaActivateOut:
    if user.mfa_enabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="mfa_already_enabled",
        )
    if user.mfa_secret is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mfa_setup_required",
        )
    if not verify_totp(user.mfa_secret, body.totp_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_totp",
        )

    plain_codes = [_generate_backup_code() for _ in range(BACKUP_CODE_COUNT)]
    user.backup_codes_hash = [hash_password(c) for c in plain_codes]
    user.mfa_enabled_at = datetime.now(UTC)
    await db.commit()

    return MfaActivateOut(backup_codes=plain_codes, enabled_at=user.mfa_enabled_at)


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_disable(
    body: MfaDisableIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Disable MFA. Requires a currently-valid TOTP or backup code to
    defend against a hijacked session lowering security."""
    if user.mfa_enabled_at is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mfa_not_enabled",
        )
    settings = get_settings()
    if user.is_admin and settings.require_mfa_for_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="mfa_required_for_admin",
        )
    if not verify_mfa_code(user, body.totp_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_totp",
        )
    user.mfa_secret = None
    user.mfa_enabled_at = None
    user.backup_codes_hash = None
    await db.commit()


@router.post("/auth/login-mfa", response_model=TokenOut)
async def login_mfa(
    request: Request,
    response: Response,
    body: LoginMfaIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenOut:
    """Combined password + TOTP login. Use this once the user has
    confirmed MFA is active on their account — ``/auth/login`` will
    otherwise respond with ``detail = "mfa_required"``."""
    user = (
        await db.execute(select(User).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    if (
        user is None
        or not user.password_hash
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )
    if user.mfa_enabled_at is None:
        # Admins without MFA still can't slip through this endpoint.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mfa_not_enabled",
        )
    if not verify_mfa_code(user, body.totp_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_totp",
        )
    await db.commit()  # persist backup-code consumption, if any
    token = issue_access_token(subject_id=user.subject_id, email=user.email, is_admin=user.is_admin)
    # Mirror the /login flow: emit the HttpOnly session cookie so the
    # SPA never has to touch the raw token.
    from bvphoenix.api.auth import _set_session_cookie
    from bvphoenix.config import get_settings as _get_settings

    _set_session_cookie(
        response,
        request,
        token,
        max_age=_get_settings().jwt_expires_seconds,
    )
    return TokenOut(access_token=token)
