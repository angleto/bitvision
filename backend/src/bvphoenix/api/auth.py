"""Authentication routes — local password + JWT path.

The phoenix login flow is local: bcrypt + JWT signed with
``BVP_JWT_SECRET``. Optional external OIDC SSO is wired separately
(see ``auth/oidc.py``) but disabled by default. AI assistants do
not use these endpoints; they connect via the MCP transport with
per-assistant client_id/client_secret pairs.

Email verification: on registration we mint a one-shot token, mail the
raw value to the user, and persist only its SHA-256 hash. ``POST
/auth/verify-email`` consumes it; ``POST /auth/resend-verification``
mints a replacement. Login refuses unverified accounts when
``BVP_REQUIRE_EMAIL_VERIFICATION`` is true.
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import (
    hash_password,
    issue_access_token,
    require_user,
    verify_password,
)
from bvphoenix.auth.deps import clear_session_cookie, set_session_cookie
from bvphoenix.auth.tokens import decode_token
from bvphoenix.config import Settings, get_settings
from bvphoenix.db.models import (
    Consent,
    EmailVerificationToken,
    PasswordResetToken,
    RevokedToken,
    Subject,
    User,
)
from bvphoenix.db.models.gdpr import REQUIRED_CONSENT_KINDS
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.email import (
    build_password_reset_email,
    send_email,
    send_verification_email,
)
from bvphoenix.services.quota import (
    STORAGE_FREE_TIER_BYTES,
    get_user_storage_usage,
)
from bvphoenix.services.rate_limit import (
    LOGIN_LIMIT,
    REGISTER_LIMIT,
    SlidingWindowRateLimiter,
    clear_login_failures,
    client_ip,
    is_locked_out,
    limiter,
    record_login_failure,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# 10 requests / minute / IP on both endpoints. See
# ``services/rate_limit.py`` for why this is per-process rather than
# Redis-backed today.
_forgot_limiter = SlidingWindowRateLimiter(max_hits=10, window_seconds=60.0)
_reset_limiter = SlidingWindowRateLimiter(max_hits=10, window_seconds=60.0)


# ---------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------
#
# Browser SPAs receive their access token as an HttpOnly cookie rather
# than reading it from the JSON body. The legacy ``access_token`` field
# on the response is retained for non-browser callers (curl smoke
# tests, the bvphoenix-import CLI, MCP integration tests) but the
# frontend bundle no longer reads it. See docs/security-auth-cookies.md.
SESSION_COOKIE_NAME = "bvp_session"


def _set_session_cookie(
    response: Response,
    request: Request,
    token: str,
    *,
    max_age: int,
) -> None:
    """Thin wrapper over the canonical writer in ``auth.deps`` so login,
    OIDC, and the share-link recipient flows emit byte-identical cookie
    attributes (the reader ``_creds_from_request`` lives next to it)."""
    set_session_cookie(response, request, token, max_age=max_age)


def _clear_session_cookie(response: Response, request: Request) -> None:
    """Mirror of ``_set_session_cookie`` for logout."""
    clear_session_cookie(response, request)


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(min_length=1, max_length=255)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterOut(BaseModel):
    """Registration response.

    When email verification is required, ``access_token`` is null until
    the user completes the flow — the frontend shows the "check your
    email" state instead of logging the user in. When verification is
    optional we still ship a token so the current UX doesn't regress.
    """

    subject_id: str
    email: EmailStr
    email_verification_required: bool
    access_token: str | None = None
    token_type: str = "bearer"


class VerifyEmailIn(BaseModel):
    token: str = Field(min_length=16, max_length=256)


class ResendVerificationIn(BaseModel):
    email: EmailStr


class UserOut(BaseModel):
    subject_id: str
    email: EmailStr
    display_name: str
    is_admin: bool
    email_verified: bool
    # True when the caller authenticated via a share-link JWT (anonymous
    # PUBLIC principal scoped to one grant), not a real account. The SPA
    # uses it to show the "create an account to keep this access" banner
    # and to avoid treating the guest as a fully signed-in user.
    is_anonymous_share: bool = False


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    new_password: str = Field(min_length=8, max_length=256)


def _user_to_out(user: User, subject: Subject) -> UserOut:
    return UserOut(
        subject_id=str(user.subject_id),
        email=user.email,
        display_name=subject.display_name,
        is_admin=user.is_admin,
        email_verified=user.email_verified_at is not None,
    )


def _hash_reset_token(plaintext: str) -> str:
    """SHA-256 hex of the token. See ``docs/security-password-reset.md``."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _hash_token(raw: str) -> str:
    """SHA-256 hex digest — what we persist in the DB."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_verification_url(raw_token: str, settings: Settings) -> str:
    base = settings.frontend_base_url.rstrip("/")
    return f"{base}/verify-email?{urlencode({'token': raw_token})}"


async def _issue_verification_token(db: AsyncSession, user: User, settings: Settings) -> str:
    """Mint a new one-shot verification token and persist its hash.

    Returns the raw token so the caller can mail it — the raw value is
    never logged and never stored.
    """
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.email_verification_ttl_seconds)
    db.add(
        EmailVerificationToken(
            user_subject_id=user.subject_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    await db.flush()
    return raw


@router.post("/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(REGISTER_LIMIT)
async def register(
    request: Request,
    body: RegisterIn,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: AuditDep,
) -> RegisterOut:
    existing = (
        await db.execute(select(User).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")

    subject = Subject(kind="user", display_name=body.display_name)
    db.add(subject)
    await db.flush()
    user = User(
        subject_id=subject.id,
        email=body.email.lower(),
        password_hash=hash_password(body.password),
    )
    db.add(user)
    await db.flush()

    # Required consents (ToS, Privacy Policy) are a precondition to
    # using the platform; record explicit rows so the audit trail
    # carries a real grant, not a synthesised one.
    for kind in REQUIRED_CONSENT_KINDS:
        db.add(
            Consent(
                user_subject_id=user.subject_id,
                kind=kind,
                metadata_={"source": "registration"},
            )
        )

    raw_token = await _issue_verification_token(db, user, settings)
    await db.commit()

    url = _build_verification_url(raw_token, settings)
    # Dispatch in the background so a slow SMTP relay does not delay the
    # HTTP response. ``send_verification_email`` swallows its own errors.
    background_tasks.add_task(send_verification_email, user.email, url)

    access_token: str | None = None
    if not settings.require_email_verification:
        access_token = issue_access_token(
            subject_id=user.subject_id,
            email=user.email,
            is_admin=user.is_admin,
        )

    await audit.log(
        action="register",
        actor_subject_id=user.subject_id,
        resource_kind="user",
        resource_id=user.subject_id,
    )
    return RegisterOut(
        subject_id=str(user.subject_id),
        email=user.email,
        email_verification_required=settings.require_email_verification,
        access_token=access_token,
    )


@router.post("/login", response_model=TokenOut)
@limiter.limit(LOGIN_LIMIT)
async def login(
    request: Request,
    response: Response,
    body: LoginIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: AuditDep,
) -> TokenOut:
    email = body.email.lower()
    # 423 rather than 401 so clients can distinguish "try again later" from
    # "wrong password" and stop retrying.
    if await is_locked_out(email):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="account temporarily locked due to repeated failed logins",
        )

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if (
        user is None
        or not user.password_hash
        or not verify_password(body.password, user.password_hash)
    ):
        await record_login_failure(email)
        attempted_subject = user.subject_id if user else None
        await audit.log(
            action="login_failed",
            actor_subject_id=attempted_subject,
            resource_kind="user",
            resource_id=attempted_subject,
            metadata={"email_attempted": body.email.lower()},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )
    await clear_login_failures(email)
    if settings.require_email_verification and user.email_verified_at is None:
        # 403 rather than 401 so the client can distinguish "bad
        # password" from "please verify".
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email not verified",
        )
    # MFA gating: active MFA → 401 mfa_required (frontend pivots to
    # /auth/login-mfa); unenrolled admin → 403 when enforcement is on.
    # Full flow lives in bvphoenix.api.mfa / docs/security-mfa.md.
    if user.mfa_enabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="mfa_required",
        )
    if user.is_admin and settings.require_mfa_for_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="mfa_enrolment_required",
        )
    await audit.log(
        action="login_success",
        actor_subject_id=user.subject_id,
        resource_kind="user",
        resource_id=user.subject_id,
    )
    token = issue_access_token(subject_id=user.subject_id, email=user.email, is_admin=user.is_admin)
    _set_session_cookie(response, request, token, max_age=settings.jwt_expires_seconds)
    return TokenOut(access_token=token)


@router.post("/verify-email", response_model=TokenOut)
async def verify_email(
    request: Request,
    response: Response,
    body: VerifyEmailIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenOut:
    token_hash = _hash_token(body.token)
    row = (
        await db.execute(
            select(EmailVerificationToken).where(EmailVerificationToken.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    # Constant-ish error: do not leak whether the token existed but
    # expired vs. never existed — both collapse to "invalid".
    if row is None or row.used_at is not None or row.expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired token",
        )

    user = (
        await db.execute(select(User).where(User.subject_id == row.user_subject_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired token",
        )

    row.used_at = now
    if user.email_verified_at is None:
        user.email_verified_at = now
    await db.commit()

    token = issue_access_token(subject_id=user.subject_id, email=user.email, is_admin=user.is_admin)
    _set_session_cookie(response, request, token, max_age=settings.jwt_expires_seconds)
    return TokenOut(access_token=token)


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(
    body: ResendVerificationIn,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Mint a fresh verification token and mail it.

    Always returns 202 regardless of whether the email is registered —
    account-enumeration avoidance. The concrete outcome is visible only
    via the email inbox of the address provided.
    """
    user = (
        await db.execute(select(User).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    if user is not None and user.email_verified_at is None:
        raw_token = await _issue_verification_token(db, user, settings)
        await db.commit()
        url = _build_verification_url(raw_token, settings)
        background_tasks.add_task(send_verification_email, user.email, url)
    return {"status": "accepted"}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> None:
    """Server-side cookie clear + audit. JWTs are stateless in this
    fallback path, so the token itself remains cryptographically valid
    until it expires; logout drops the HttpOnly cookie so the browser
    cannot replay it on the next request.

    Non-browser callers that authenticated via the legacy ``Authorization:
    Bearer`` header should additionally discard their copy of the token —
    we cannot reach across the wire to invalidate it.
    """
    _clear_session_cookie(response, request)
    await audit.log(
        action="logout",
        actor_subject_id=user.subject_id,
        resource_kind="user",
        resource_id=user.subject_id,
    )


@router.get("/me", response_model=UserOut)
async def me(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    subject = (await db.execute(select(Subject).where(Subject.id == user.subject_id))).scalar_one()
    out = _user_to_out(user, subject)
    # ``share_grant`` is pinned onto request.state by the share-link auth
    # branch (auth.deps); its presence means this is an anonymous guest
    # session, which the SPA surfaces as a "create an account" prompt.
    out.is_anonymous_share = getattr(request.state, "share_grant", None) is not None
    return out


class StorageUsageOut(BaseModel):
    used_bytes: int
    quota_bytes: int
    remaining_bytes: int
    tiers_counted: list[str] = Field(
        default_factory=lambda: ["t1", "t2"],
        description="Tiers that consume the quota (T3/T4 are absorbed by the platform).",
    )


@router.get("/me/storage", response_model=StorageUsageOut)
async def my_storage(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StorageUsageOut:
    """Report the caller's current T1+T2 storage footprint vs. the cap.

    Useful for a settings page or a pre-upload confirmation dialog. A
    401 tells an unauthenticated caller to log in; we do not expose an
    aggregate quota for anonymous callers.
    """
    usage = await get_user_storage_usage(db, user.subject_id)
    return StorageUsageOut(
        used_bytes=usage.used_bytes,
        quota_bytes=STORAGE_FREE_TIER_BYTES,
        remaining_bytes=usage.remaining_bytes,
    )


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(
    body: ForgotPasswordIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Issue a reset token for ``body.email`` and email it.

    Always returns 204 regardless of whether the address has an account —
    leaking existence makes account-discovery trivial. The token itself
    is only persisted as a SHA-256 hash; the plaintext leaves via email
    and is never stored, so a DB snapshot can't be replayed.
    """
    ip = client_ip(request)
    _forgot_limiter.check(ip)

    settings = get_settings()
    email_lc = body.email.lower()
    user = (await db.execute(select(User).where(User.email == email_lc))).scalar_one_or_none()

    # Only provision + send when the account actually exists and uses
    # local password auth (OIDC users have NULL password_hash; emailing
    # them a local-reset link would be misleading).
    if user is not None and user.password_hash is not None:
        plaintext = secrets.token_urlsafe(32)
        token_hash = _hash_reset_token(plaintext)
        expires_at = datetime.now(UTC) + timedelta(minutes=settings.password_reset_ttl_minutes)
        db.add(
            PasswordResetToken(
                user_subject_id=user.subject_id,
                token_hash=token_hash,
                expires_at=expires_at,
                requested_ip=ip,
            )
        )
        await db.commit()

        reset_url = f"{settings.public_frontend_url.rstrip('/')}/reset-password?token={plaintext}"
        message = build_password_reset_email(
            to=user.email,
            reset_url=reset_url,
            ttl_minutes=settings.password_reset_ttl_minutes,
        )
        # Best-effort: email transport failure must not leak existence by
        # flipping the response to 5xx.
        with contextlib.suppress(Exception):
            await send_email(message)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    body: ResetPasswordIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Consume a reset token and set a new password hash.

    Invalid / expired / already-used tokens all surface as the same 400
    error so an attacker can't distinguish "never existed" from
    "already consumed".
    """
    _reset_limiter.check(client_ip(request))

    token_hash = _hash_reset_token(body.token)
    now = datetime.now(UTC)

    row = (
        await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired reset token",
        )

    user = (
        await db.execute(select(User).where(User.subject_id == row.user_subject_id))
    ).scalar_one_or_none()
    if user is None:
        # Token FK is ON DELETE CASCADE so this would normally be
        # unreachable; treat it as the generic failure mode anyway.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired reset token",
        )

    user.password_hash = hash_password(body.new_password)
    row.used_at = now

    # Defence in depth: invalidate every other outstanding reset token
    # for this user. A successful reset should drop any sibling tokens
    # an attacker might also be holding.
    others = (
        (
            await db.execute(
                select(PasswordResetToken).where(
                    PasswordResetToken.user_subject_id == user.subject_id,
                    PasswordResetToken.used_at.is_(None),
                    PasswordResetToken.id != row.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for sibling in others:
        sibling.used_at = now

    await db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----- Token revocation (ADR 0016) -----------------------------------------


class RevokeTokenIn(BaseModel):
    """Body for ``POST /auth/revoke-token``.

    Either ``jti`` (revoke a specific token) or ``token`` (decode and
    revoke whatever JTI it carries). The latter is convenient when the
    caller wants to revoke a token they hold without parsing it
    themselves.
    """

    jti: str | None = None
    token: str | None = None
    reason: str | None = Field(default=None, max_length=512)


class RevokeTokenOut(BaseModel):
    jti: str
    expires_at: datetime
    revoked_at: datetime


@router.post(
    "/revoke-token",
    response_model=RevokeTokenOut,
    status_code=status.HTTP_201_CREATED,
)
async def revoke_token(
    body: RevokeTokenIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> RevokeTokenOut:
    """Revoke a JWT by its ``jti`` claim.

    Authorization rules:

    * Admins can revoke any token by ``jti``.
    * Non-admins can only revoke a token they hold (must supply
      ``token`` so the server can verify the signature and the ``sub``
      matches the caller).

    The row is inserted unconditionally; ``ON CONFLICT DO NOTHING``
    keeps re-revocation idempotent.
    """
    import uuid

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    target_jti: uuid.UUID | None = None
    target_subject_id: uuid.UUID | None = None
    target_typ: str | None = None
    expires_at: datetime | None = None

    if body.token:
        payload = decode_token(body.token)
        if payload is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid token",
            )
        if payload.jti is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="token has no jti claim and cannot be revoked individually",
            )
        target_jti = payload.jti
        target_subject_id = payload.subject_id
        target_typ = payload.typ
        expires_at = datetime.fromtimestamp(payload.exp, tz=UTC)
    elif body.jti:
        try:
            target_jti = uuid.UUID(body.jti)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="jti must be a UUID",
            ) from e
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="provide either token or jti",
        )

    if not user.is_admin and target_subject_id != user.subject_id:
        # Non-admin self-revoke requires us to verify ownership; that
        # only works via the ``token`` path because ``jti`` alone has
        # no associated identity.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "non-admins can only self-revoke; supply your own token "
                "in the body so the server can verify ownership"
            ),
        )

    if expires_at is None:
        # Defensive default: keep the row for 30 days. The cleanup
        # worker will sweep it once expired anyway.
        expires_at = datetime.now(UTC) + timedelta(days=30)

    revoked_at = datetime.now(UTC)
    stmt = (
        pg_insert(RevokedToken)
        .values(
            jti=target_jti,
            revoked_at=revoked_at,
            expires_at=expires_at,
            revoked_by_subject_id=user.subject_id,
            reason=body.reason,
            subject_id=target_subject_id,
            typ=target_typ,
        )
        .on_conflict_do_nothing(index_elements=[RevokedToken.jti])
    )
    await db.execute(stmt)
    await db.commit()

    await audit.log(
        action="token_revoked",
        actor_subject_id=user.subject_id,
        resource_kind="jwt",
        resource_id=target_jti,
        metadata={
            "reason": body.reason,
            "target_subject_id": str(target_subject_id) if target_subject_id else None,
            "typ": target_typ,
        },
    )

    return RevokeTokenOut(
        jti=str(target_jti),
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


# ---------------------------------------------------------------------------
# Single-use download tokens (browser-native single-file downloads)
# ---------------------------------------------------------------------------


class DownloadTokenIn(BaseModel):
    """Body for ``POST /auth/download-token``.

    The frontend mints these so anchor-click downloads of single
    files (PDF / image / multi-GiB DVD ISO) can stream directly
    through the backend without putting the persistent JWT in URLs
    that end up in proxy logs and browser history. The token is
    bound to one specific resource and consumed atomically on the
    first GET of that resource's ``/download`` endpoint.
    """

    resource_kind: str = Field(..., pattern="^(document|document_file|job_result)$")
    resource_id: uuid.UUID
    # Required only for ``document_file`` (a child of a multi-file
    # document). Unused for top-level documents.
    child_id: uuid.UUID | None = None


class DownloadTokenOut(BaseModel):
    token: str
    expires_in: int


@router.post("/download-token", response_model=DownloadTokenOut)
async def issue_download_token_endpoint(
    body: DownloadTokenIn,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DownloadTokenOut:
    """Issue a 5-minute single-use token for one specific download.

    Permission validation runs at consume time on the actual
    ``/download`` endpoint (same path the Bearer flow uses), so we
    don't duplicate the rules here — minting an unscoped token for a
    resource the user can't read still 404s on consume. Keeping the
    issuer thin avoids drift between two parallel permission gates.
    """
    from arq import create_pool

    from bvphoenix.services.arq_redis import redis_settings
    from bvphoenix.services.download_tokens import issue_download_token

    settings = get_settings()
    redis = await create_pool(redis_settings(settings.redis_url))
    try:
        token, ttl = await issue_download_token(
            redis,
            subject_id=user.subject_id,
            resource_kind=body.resource_kind,
            resource_id=body.resource_id,
            child_id=body.child_id,
        )
    finally:
        await redis.close()
    return DownloadTokenOut(token=token, expires_in=ttl)
