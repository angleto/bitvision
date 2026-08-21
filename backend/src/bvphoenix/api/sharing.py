"""Sharing API — link-based access for radiologists (docs/sharing.md).

Owner creates a share link → gets a URL with a random token. Anyone
with the token (optionally + password) gets the permissions encoded in
the underlying grant. Short-lived JWTs are issued on verify so the
viewer can call normal API endpoints with the granted permissions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api._http import proxy_s3_object
from bvphoenix.auth import (
    enforce_agent_patient_scope,
    hash_password,
    require_user,
    verify_password,
)
from bvphoenix.auth.deps import set_session_cookie
from bvphoenix.auth.tokens import decode_token, issue_access_token
from bvphoenix.config import get_settings
from bvphoenix.db.models import Document, Grant, ImagingStudy, Patient, ShareLink, User
from bvphoenix.db.models.email_delivery import EmailDelivery
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID, SHARE_LINK_MODES
from bvphoenix.db.session import get_db
from bvphoenix.middleware.audit_dependency import AuditDep
from bvphoenix.services.access_levels import (
    level_to_permissions,
    permissions_to_level,
)
from bvphoenix.services.email import (
    EmailMessage,
    build_share_invitation_email,
)
from bvphoenix.services.email import (
    normalize_locale as normalize_email_locale,
)
from bvphoenix.services.email_delivery import (
    attempt as attempt_delivery,
)
from bvphoenix.services.email_delivery import (
    enqueue as enqueue_delivery,
)
from bvphoenix.services.email_delivery import (
    register_builder as register_delivery_builder,
)
from bvphoenix.services.grants import resolve_deidentify_default
from bvphoenix.services.permissions import SHARED_DOWNLOAD
from bvphoenix.services.rate_limit import (
    SHARE_DOWNLOAD_LIMIT,
    SHARE_METADATA_LIMIT,
    SHARE_NOTIFY_LIMIT,
    SHARE_VERIFY_LIMIT,
    limiter,
)
from bvphoenix.storage import get_s3_storage

logger = logging.getLogger(__name__)

# Lifetime of the download token minted for password-protected
# share-link recipients. The shared-cache download endpoint PEEKs
# (no GETDEL) so the same dt is multi-use within this window — a
# clinician who entered the password at 09:00 can re-download
# until the same time the next day without re-typing it. After
# the TTL expires, /verify is required again. 24 hours matches a
# typical "I read the consult, I save the ZIP at home that
# evening" workflow; the dt stays scope-locked to this single
# share link and a leaked dt is useless against any other resource.
SHARE_LINK_DOWNLOAD_TTL_SECONDS = 24 * 60 * 60

router = APIRouter(tags=["sharing"])


async def _prepare_study_share_export(
    db: AsyncSession,
    *,
    link: ShareLink,
    study_id: uuid.UUID,
    deidentify: bool,
    owner_subject_id: uuid.UUID,
    is_admin: bool,
) -> None:
    """Enqueue (or rebind to a dedup-hit) the cached export job for a
    freshly-created study share, and stamp ``link.prepared_job_id``.

    The dedup primitive (``services.jobs.enqueue_or_get``) keys on
    ``(kind, owner, scope_ids, canonical_input)``. Two shares for the
    same ``(study, deidentify, owner)`` triple share the same Job
    row → the same S3 artifact. Two shares that differ on
    ``deidentify`` get distinct artifacts, never confused.

    Best-effort: any failure leaves ``prepared_job_id`` NULL and the
    link falls back to the at-click enqueue path. The caller logs +
    swallows; share creation must not be blocked by a hiccup in the
    job queue.
    """
    from arq import create_pool

    from bvphoenix.api.patient_export import JOB_KIND_STUDY_EXPORT
    from bvphoenix.services import jobs as jobs_service
    from bvphoenix.services.arq_redis import redis_settings

    canonical_input: dict[str, Any] = {"deidentify": bool(deidentify)}

    try:
        result = await jobs_service.enqueue_or_get(
            db,
            kind=JOB_KIND_STUDY_EXPORT,
            owner_subject_id=owner_subject_id,
            canonical_input=canonical_input,
            scope_ids=(study_id,),
            expires_in_hours=48,
            is_admin=is_admin,
        )
    except jobs_service.JobCapExceededError:
        # Per-user cap reached. Don't fail the share — just leave
        # prepared_job_id NULL and let the click-time path enqueue
        # later when slots free up.
        return

    if not result.deduped:
        settings = get_settings()
        redis = await create_pool(redis_settings(settings.redis_url))
        try:
            arq_handle = await redis.enqueue_job(
                "export_study_zip",
                str(result.job.id),
                str(study_id),
                str(owner_subject_id),
                json.dumps(canonical_input),
            )
        finally:
            await redis.close()
        if arq_handle is not None:
            await jobs_service.set_arq_job_id(db, result.job.id, arq_handle.job_id)

    link.prepared_job_id = result.job.id
    await db.commit()


async def _prepare_folder_share_export(
    db: AsyncSession,
    *,
    link: ShareLink,
    folder_id: uuid.UUID,
    patient_id: uuid.UUID,
    study_ids: list[uuid.UUID],
    document_ids: list[uuid.UUID],
    owner_subject_id: uuid.UUID,
    is_admin: bool,
) -> None:
    """Twin of :func:`_prepare_study_share_export` but for folder
    shares. Reuses the ``fascicolo_export`` worker with a
    folder-scoped canonical_input so the recipient lands on a
    pre-warmed ZIP. The job's scope_ids stay on the patient (the
    cap-counting + cross-device recovery primitives are
    patient-scoped today); the folder identity rides in the
    canonical_input dedup hash.

    Best-effort: any failure leaves ``prepared_job_id`` NULL and the
    link falls back to the at-click enqueue path. Share creation
    must not be blocked by a queue hiccup.
    """
    from arq import create_pool

    from bvphoenix.api.patient_export import JOB_KIND_FASCICOLO_EXPORT
    from bvphoenix.services import jobs as jobs_service
    from bvphoenix.services.arq_redis import redis_settings

    canonical_input: dict[str, Any] = {
        "includes": ["studies", "documents", "dicom"],
        "scope_study_ids": sorted(str(x) for x in study_ids),
        "scope_document_ids": sorted(str(x) for x in document_ids),
        "scope_kind": "folder",
        "scope_folder_id": str(folder_id),
    }

    try:
        result = await jobs_service.enqueue_or_get(
            db,
            kind=JOB_KIND_FASCICOLO_EXPORT,
            owner_subject_id=owner_subject_id,
            canonical_input=canonical_input,
            # ``scope_ids`` carries BOTH the patient and the folder
            # so the IDOR check in ``download_via_share_link``
            # (``grant.resource_id in job.scope_ids``) succeeds for
            # folder grants — and so two folders inside the same
            # patient get distinct cached jobs (the patient_id alone
            # would let two different folder shares dedup-hit one
            # archive that's a superset of one and a subset of the
            # other). Including folder_id keeps each cached ZIP
            # bound to exactly the share's stated scope.
            scope_ids=(patient_id, folder_id),
            expires_in_hours=48,
            is_admin=is_admin,
        )
    except jobs_service.JobCapExceededError:
        return

    if not result.deduped:
        settings = get_settings()
        redis = await create_pool(redis_settings(settings.redis_url))
        try:
            arq_handle = await redis.enqueue_job(
                "export_patient_zip",
                str(result.job.id),
                str(patient_id),
                str(owner_subject_id),
                json.dumps(canonical_input),
            )
        finally:
            await redis.close()
        if arq_handle is not None:
            await jobs_service.set_arq_job_id(db, result.job.id, arq_handle.job_id)

    link.prepared_job_id = result.job.id
    await db.commit()


class ShareTarget(BaseModel):
    kind: str = Field(description="link_public | link_org | email | org")
    email: str | None = None
    org_subject_id: str | None = None


class ShareCreateIn(BaseModel):
    access_level: str = Field(default="viewer", description="viewer | editor | manager")
    download: bool = Field(default=False, description="Allow DICOM download")
    target: ShareTarget = Field(default_factory=lambda: ShareTarget(kind="link_public"))
    expires_in_hours: int | None = Field(default=24 * 7)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    # When true the server picks a high-entropy password (24 chars,
    # ~118 bits) and returns it ONCE in ``generated_password`` on
    # create. The plaintext is never stored; the client must capture
    # it to deliver to the recipient out-of-band. Mutually exclusive
    # with the manual ``password`` field.
    autogen_password: bool = Field(default=False)
    label: str | None = Field(default=None, max_length=255)
    max_uses: int | None = Field(default=None, ge=1)
    # Delivery mode. ``claim`` (default) creates a magic-link onto a
    # claimable account; ``anonymous`` makes the link itself the
    # credential (treated as an API key, every write attributed via
    # ActorContext.kind='link'). The frontend must surface a blocking
    # warning before letting the user pick ``anonymous``.
    mode: str = Field(default="claim", pattern="^(claim|anonymous)$")
    recipient_name: str | None = Field(default=None, max_length=255)
    recipient_email: str | None = Field(default=None, max_length=255)
    recipient_phone: str | None = Field(default=None, max_length=64)
    deidentify: bool | None = Field(
        default=None,
        description=(
            "Strip PHI (PS3.15 Basic Profile) from DICOMs served via this "
            "grant. ``None`` (the default) applies the authorization.md §7 "
            "policy: external grants (public links, grantees outside the "
            "grantor's orgs) ship with de-identification ON; internal "
            "grants default to OFF. Pass an explicit boolean to override."
        ),
    )
    ai_sponsorship_cap_cents: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional AI budget the share creator sponsors for the "
            "recipient. When set, claiming the link (anonymous mode) "
            "auto-creates a WalletSponsorship of the creator's wallet "
            "with this cap, scope=patient (or consultation), so the "
            "recipient can run AI on the shared record at the "
            "creator's expense. NULL = recipient pays from their own "
            "wallet (legacy behaviour)."
        ),
    )


class ShareLinkOut(BaseModel):
    id: str
    token: str
    url: str
    label: str | None
    permissions: list[str]
    expires_at: str | None
    revoked: bool
    use_count: int
    max_uses: int | None
    requires_password: bool
    created_at: str
    mode: str = "claim"
    recipient_name: str | None = None
    recipient_email: str | None = None
    recipient_phone: str | None = None
    # Plaintext password returned ONCE on create when ``autogen_password``
    # was requested. Never returned by GET / PATCH / list endpoints.
    generated_password: str | None = None
    # Reflection of the underlying grant for the lista UI:
    # ``deidentify`` decides whether downloads carry PHI;
    # ``received_at`` closes the audit chain when the recipient
    # confirms; ``download_count`` counts complete downloads (200
    # full body, never 206 Partial); ``prepared_*`` are the
    # cached export job's snapshot so a row in the lista renders
    # progress without an extra round-trip.
    deidentify: bool = False
    received_at: str | None = None
    download_count: int = 0
    resource_kind: str = "study"
    resource_id: str = ""
    grantor_subject_id: str | None = None
    prepared_job_id: str | None = None
    prepared_status: str | None = None
    prepared_progress_done: int | None = None
    prepared_progress_total: int | None = None
    revoked_at: str | None = None
    ai_sponsorship_cap_cents: int | None = None
    ai_sponsorship_id: str | None = None


class ShareInfoOut(BaseModel):
    study_title: str | None
    modalities: list[str]
    study_date: str | None
    requires_password: bool
    expires_at: str | None
    permissions: list[str]
    # Exposed so the public info page can show "N uses remaining". Both
    # are ``None`` when the share has no use cap (unlimited).
    max_uses: int | None = None
    uses_remaining: int | None = None
    # Resource the link points to. Frontend uses these to land the user
    # on the right page after /verify: /patients/<id> for fascicolo
    # shares, /studies/<id> for single-study shares.
    resource_kind: str
    resource_id: str
    # Hints the public viewer needs to show / hide the claim CTA.
    # ``mode`` is always set; ``claimable`` is true only when the link
    # is anonymous, not yet claimed, has a recipient_email and the
    # grant is still alive (not revoked / expired).
    mode: str = "claim"
    # ``claimable``: the recipient can turn this link into a NEW account
    # (PUBLIC-held grant, alive, not yet claimed, no account exists yet
    # for the recipient). ``bindable``: an account already exists for the
    # recipient, so they log in and attach the grant via /bind instead of
    # minting a second account. Exactly one of the two is ever true.
    claimable: bool = False
    bindable: bool = False
    # Whether the link itself carries an addressee email. Drives the
    # account-creation form on the landing page: when False the recipient
    # supplies their own email (universal claim); when True the email is
    # server-side and the form omits the field. Only the *presence* is
    # exposed, never the value (see the PII note below).
    recipient_email_known: bool = False
    # recipient_name / recipient_email intentionally NOT exposed here:
    # /shared/{token}/info is an unauthenticated public endpoint. Anyone
    # with the token can hit it, so returning the intended addressee
    # would leak third-party PII (and let a phisher reproduce the
    # personalised landing page verbatim). The recipient learns nothing
    # they did not already know (their own email); attackers learn who
    # the grantor sent the link to. ``claimable`` is enough for the FE
    # to decide whether to show the claim CTA.
    # Surfaced so the public landing page can show "Studio
    # pseudonimizzato (PHI rimossi)" before the recipient clicks
    # download. The server enforces the actual scrubbing at serve
    # time; this is just transparency.
    deidentified: bool = False
    # Pre-flight summary so the recipient sees "12 file, 350 MB"
    # before a multi-GB download. Both are best-effort: missing
    # instance metadata yields ``None`` (the FE renders "—" then).
    total_files: int | None = None
    total_bytes: int | None = None
    # Display name of the grantor (one of the user's Subject rows).
    # Lets the landing page say "Condiviso da Dr. Mario Rossi"
    # instead of "Condiviso da utente anonimo", which is the single
    # biggest deterrent against "this looks like phishing".
    grantor_display: str | None = None
    # Cached pre-export state (study-scoped shares only). Lets the
    # recipient see "ZIP pronto" / "ZIP in preparazione 30%" before
    # they even click open. NULL on legacy rows + on patient/folder
    # shares which don't pre-prep yet.
    prepared_status: str | None = None
    prepared_progress_done: int | None = None
    prepared_progress_total: int | None = None


class VerifyIn(BaseModel):
    password: str | None = None


class VerifyOut(BaseModel):
    access_token: str
    expires_in: int
    # Optional cached-download payload. When the share has a
    # ``prepared_job_id`` whose job is succeeded, /verify mints a
    # 5-minute single-use download token for the cached artifact and
    # returns the ready-to-click URL. The recipient's anchor click
    # then streams from the cache exactly as in the no-password
    # path (Range/resume/storage-isolation preserved). NULL when
    # there is no cache to consume — FE falls back to the legacy
    # JWT-bearing /studies viewer flow that re-enqueues on demand.
    cached_download_url: str | None = None
    cached_download_expires_in: int | None = None


def _resolve_public_base_url(override: str | None = None) -> str:
    """Single source of truth for the absolute URL prefix on share
    links. Callers should never hand-roll
    ``f"https://.../shared/..."``: ops swaps the host (staging vs
    production vs preview) by setting ``BVP_PUBLIC_FRONTEND_URL`` and
    every share that gets emailed / copied / pasted into a chat
    thread is automatically rewritten."""
    if override:
        return override.rstrip("/")
    settings = get_settings()
    return (settings.public_frontend_url or "").rstrip("/")


def _dry_run_share_link_out(
    *,
    body: ShareCreateIn,
    resource_kind: str,
    resource_id: str,
    permissions: list[str],
    grantor_subject_id: str,
    valid_until: datetime | None,
    deidentify: bool,
) -> ShareLinkOut:
    """Synthetic ShareLinkOut returned when ``dry_run=true``.

    Same shape as the committed response so an agent / GUI client can
    branch on the same fields, but with placeholder identifiers and a
    non-clickable URL so a dry-run preview cannot be confused for a
    real share. ``generated_password`` is intentionally NULL on dry
    runs: the high-entropy password is only minted on the actual
    create, never on a preview, so the preview can be replayed safely.
    """
    requires_password = bool(body.password) or bool(body.autogen_password)
    return ShareLinkOut(
        id="dry-run",
        token="dry-run",
        url="(dry-run — no persistence)",
        label=body.label,
        permissions=permissions,
        expires_at=valid_until.isoformat() if valid_until else None,
        revoked=False,
        use_count=0,
        max_uses=body.max_uses,
        requires_password=requires_password,
        created_at=datetime.now(UTC).isoformat(),
        mode=body.mode,
        recipient_name=(body.recipient_name or "").strip() or None,
        recipient_email=(body.recipient_email or "").strip().lower() or None,
        recipient_phone=(body.recipient_phone or "").strip() or None,
        generated_password=None,
        deidentify=deidentify,
        received_at=None,
        download_count=0,
        resource_kind=resource_kind,
        resource_id=resource_id,
        grantor_subject_id=grantor_subject_id,
        prepared_job_id=None,
        prepared_status=None,
        prepared_progress_done=None,
    )


def _link_out(
    link: ShareLink,
    grant: Grant,
    *,
    base_url: str | None = None,
    generated_password: str | None = None,
    prep: tuple[str, int | None, int | None] | None = None,
) -> ShareLinkOut:
    """Project a (link, grant) pair to the API shape.

    ``base_url`` defaults to ``settings.public_frontend_url`` so the
    returned ``url`` is always absolute (``https://host/shared/<token>``).
    Callers that already prefix may pass an empty string explicitly,
    but every existing call site is happier with the absolute form —
    the FE used to receive a path-only URL, copy it to the clipboard
    and end up in a mailto: with a relative href that no mail client
    knew how to render.

    ``prep`` carries the optional cached export-job snapshot
    ``(status, progress_done, progress_total)``; the list endpoint
    pre-loads it via a single LEFT JOIN to avoid an N+1.
    """
    public = _resolve_public_base_url(base_url)
    # Canonical landing is the branded ``/info`` page: it shows the
    # privacy banner, prep-state progress, "Conferma ricezione" CTA
    # and a direct-download anchor for unprotected ready archives.
    # The legacy verify form (``/shared/{token}`` without ``/info``)
    # is still reachable for password entry — the info page links
    # to it via the "Apri studio" button — so legacy URLs in old
    # emails keep working while every newly-issued link points at
    # the richer landing.
    return ShareLinkOut(
        id=str(link.id),
        token=link.token,
        url=f"{public}/shared/{link.token}/info",
        label=link.label,
        permissions=list(grant.permissions),
        expires_at=grant.valid_until.isoformat() if grant.valid_until else None,
        revoked=grant.revoked_at is not None,
        use_count=link.use_count,
        max_uses=link.max_uses,
        requires_password=link.password_hash is not None,
        created_at=link.created_at.isoformat(),
        mode=link.mode,
        recipient_name=link.recipient_name,
        recipient_email=link.recipient_email,
        recipient_phone=link.recipient_phone,
        generated_password=generated_password,
        deidentify=bool(grant.deidentify),
        received_at=link.received_at.isoformat() if link.received_at else None,
        download_count=int(link.download_count or 0),
        resource_kind=grant.resource_kind,
        resource_id=str(grant.resource_id),
        grantor_subject_id=str(grant.grantor_subject_id),
        prepared_job_id=str(link.prepared_job_id) if link.prepared_job_id else None,
        prepared_status=prep[0] if prep else None,
        prepared_progress_done=prep[1] if prep else None,
        prepared_progress_total=prep[2] if prep else None,
        revoked_at=grant.revoked_at.isoformat() if grant.revoked_at else None,
        ai_sponsorship_cap_cents=link.ai_sponsorship_cap_cents,
        ai_sponsorship_id=str(link.ai_sponsorship_id) if link.ai_sponsorship_id else None,
    )


# 30-char alphabet excluding visually-ambiguous characters (I/O/L/0/1).
# 24 picks ≈ log2(30) * 24 ≈ 117.7 bits of entropy, enough to make a
# brute-force attempt against a single share link unfeasible while
# still being readable when dictated by phone.
_PASSWORD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789abcdefghjkmnpqrstuvwxyz"


def _autogen_password(length: int = 24) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def _validate_share_create(body: ShareCreateIn) -> None:
    """Cross-field invariants for the share-create payload."""
    if body.mode not in SHARE_LINK_MODES:
        raise HTTPException(status_code=400, detail=f"invalid mode: {body.mode}")
    if body.autogen_password and body.password:
        raise HTTPException(
            status_code=400,
            detail="autogen_password and password are mutually exclusive",
        )
    if body.mode == "anonymous":
        # The link IS the credential — we need someone to attribute it
        # to and a channel to deliver the password. Server-side guard
        # so a buggy client (or curl) can't bypass the UI warning.
        if not body.recipient_name or not body.recipient_name.strip():
            raise HTTPException(
                status_code=400,
                detail="recipient_name is required for mode='anonymous'",
            )
        has_email = body.recipient_email and body.recipient_email.strip()
        has_phone = body.recipient_phone and body.recipient_phone.strip()
        if not (has_email or has_phone):
            raise HTTPException(
                status_code=400,
                detail=(
                    "at least one of recipient_email / recipient_phone is "
                    "required for mode='anonymous'"
                ),
            )


@router.post(
    "/studies/{study_id}/share",
    response_model=ShareLinkOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_share_link(
    request: Request,
    study_id: uuid.UUID,
    body: ShareCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    dry_run: bool = False,
) -> ShareLinkOut:
    _validate_share_create(body)
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    # Agent token scope: refuse creating share links for studies that
    # belong to a patient outside the token's scope. Runs before the
    # owner check so a leaked token cannot enumerate cross-patient
    # ownership via the 403-vs-404 distinction.
    enforce_agent_patient_scope(request, study.patient_id)
    if not (user.is_admin or study.owner_subject_id == user.subject_id):
        raise HTTPException(status_code=403, detail="only the owner can share")

    valid_until = None
    if body.expires_in_hours is not None:
        valid_until = datetime.now(UTC) + timedelta(hours=body.expires_in_hours)

    perms = level_to_permissions(body.access_level, download=body.download)

    # Resolve grantee based on target
    grantee_id = PUBLIC_SUBJECT_ID
    if body.target.kind == "email" and body.target.email:
        from bvphoenix.db.models import User as UserModel

        target_user = (
            await db.execute(select(UserModel).where(UserModel.email == body.target.email.lower()))
        ).scalar_one_or_none()
        if not target_user:
            raise HTTPException(status_code=404, detail=f"no user with email {body.target.email}")
        grantee_id = target_user.subject_id
    elif body.target.kind in ("org", "link_org") and body.target.org_subject_id:
        grantee_id = uuid.UUID(body.target.org_subject_id)

    # authorization.md §7: external grants get deidentify=True by default;
    # the grantor can override by passing a boolean explicitly.
    deidentify = await resolve_deidentify_default(
        db,
        grantor_subject_id=user.subject_id,
        grantee_subject_id=grantee_id,
        explicit=body.deidentify,
    )

    if dry_run:
        # Validate-only mode: every check above has already run (404 on
        # missing study, 403 on agent patient scope / non-owner, 404 on
        # unknown email target, deidentify resolution). Bail before any
        # row creation, audit emission, or prep-job enqueue. The token /
        # url placeholders make the dry-run visually distinct from a
        # real share.
        return _dry_run_share_link_out(
            body=body,
            resource_kind="study",
            resource_id=str(study.id),
            permissions=perms,
            grantor_subject_id=str(user.subject_id),
            valid_until=valid_until,
            deidentify=deidentify,
        )

    grant = Grant(
        resource_kind="study",
        resource_id=study.id,
        grantor_subject_id=user.subject_id,
        grantee_subject_id=grantee_id,
        permissions=perms,
        conditions={"scope": body.target.kind},
        valid_until=valid_until,
        deidentify=deidentify,
        purpose=body.label or f"{body.access_level} access",
    )
    db.add(grant)
    await db.flush()

    link: ShareLink | None = None
    generated_password: str | None = None
    if body.target.kind in ("link_public", "link_org"):
        plaintext_password = body.password
        if body.autogen_password:
            generated_password = _autogen_password()
            plaintext_password = generated_password
        link = ShareLink(
            grant_id=grant.id,
            token=secrets.token_urlsafe(32),
            password_hash=hash_password(plaintext_password) if plaintext_password else None,
            label=body.label,
            max_uses=body.max_uses,
            mode=body.mode,
            recipient_name=(body.recipient_name or "").strip() or None,
            recipient_email=(body.recipient_email or "").strip().lower() or None,
            recipient_phone=(body.recipient_phone or "").strip() or None,
            ai_sponsorship_cap_cents=body.ai_sponsorship_cap_cents,
        )
        db.add(link)

    await db.commit()
    await db.refresh(grant)
    if link is not None:
        await db.refresh(link)

    # Pre-export the artifact in the background so the recipient's
    # first click hits a ready ZIP. Only for study-scoped link shares
    # today; patient/folder/bulk pre-prep is a separate scope (their
    # archives are bigger and the storage cost vs hit-rate ratio is
    # different). Best-effort: a failed enqueue still ships the
    # share — the link falls back to the at-click path that's been
    # shipping since beta.104.
    if link is not None and grant.resource_kind == "study":
        try:
            await _prepare_study_share_export(
                db,
                link=link,
                study_id=grant.resource_id,
                deidentify=bool(grant.deidentify),
                owner_subject_id=user.subject_id,
                is_admin=bool(user.is_admin),
            )
        except Exception:
            logger.warning("share pre-export enqueue failed", exc_info=True)

    audit_metadata: dict = {
        "grant_id": str(grant.id),
        "target_kind": body.target.kind,
        "access_level": body.access_level,
        "download": body.download,
        "deidentify": grant.deidentify,
        "deidentify_explicit": body.deidentify is not None,
        "expires_at": grant.valid_until.isoformat() if grant.valid_until else None,
        "share_mode": body.mode,
        "password_set": bool(body.password) or body.autogen_password,
        "password_autogen": body.autogen_password,
    }
    if link is not None:
        audit_metadata["link_id"] = str(link.id)
        if body.recipient_name:
            audit_metadata["recipient_name"] = body.recipient_name
    await audit.log(
        action="share_create",
        actor_subject_id=user.subject_id,
        resource_kind="study",
        resource_id=study.id,
        metadata=audit_metadata,
    )

    if link is not None:
        return _link_out(link, grant, generated_password=generated_password)

    # Return a synthetic link out for direct grants (no token)
    return ShareLinkOut(
        id=str(grant.id),
        token="",
        url="",
        label=body.label,
        permissions=list(grant.permissions),
        expires_at=grant.valid_until.isoformat() if grant.valid_until else None,
        revoked=False,
        use_count=0,
        max_uses=None,
        requires_password=False,
        created_at=grant.created_at.isoformat(),
    )


@router.post(
    "/folders/{folder_id}/share-link",
    response_model=ShareLinkOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_folder_share_link(
    request: Request,
    folder_id: uuid.UUID,
    body: ShareCreateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    dry_run: bool = False,
) -> ShareLinkOut:
    """Public-link variant of folder sharing.

    Sibling of ``POST /api/folders/{id}/share`` (which only handles
    known-grantee subject grants — internal collaborators). This
    endpoint creates a token-bearing ``ShareLink`` row plus the same
    folder + cascaded-item Grant fan-out, so an external recipient
    can reach the folder ZIP through the public ``/shared/<token>``
    URL without an account. A folder export Job is enqueued in the
    background so the recipient lands on a pre-warmed archive (same
    pattern as study/patient shares — memory
    ``feedback_long_ops_progress_recovery``).
    """
    _validate_share_create(body)

    from bvphoenix.api.folders import _ITEM_KIND_TO_GRANT_KIND, _load_owned_folder
    from bvphoenix.api.patient_export import _resolve_folder_scope
    from bvphoenix.db.models import FolderItem

    folder = await _load_owned_folder(db, folder_id, user, request)
    patient, study_ids, document_ids = await _resolve_folder_scope(db, folder_id)

    valid_until = None
    if body.expires_in_hours is not None:
        valid_until = datetime.now(UTC) + timedelta(hours=body.expires_in_hours)
    perms = level_to_permissions(body.access_level, download=body.download)

    # Resolve grantee (same matrix as study/patient share).
    grantee_id = PUBLIC_SUBJECT_ID
    if body.target.kind == "email" and body.target.email:
        from bvphoenix.db.models import User as UserModel

        target_user = (
            await db.execute(select(UserModel).where(UserModel.email == body.target.email.lower()))
        ).scalar_one_or_none()
        if not target_user:
            raise HTTPException(status_code=404, detail=f"no user with email {body.target.email}")
        grantee_id = target_user.subject_id
    elif body.target.kind in ("org", "link_org") and body.target.org_subject_id:
        grantee_id = uuid.UUID(body.target.org_subject_id)

    deidentify = await resolve_deidentify_default(
        db,
        grantor_subject_id=user.subject_id,
        grantee_subject_id=grantee_id,
        explicit=body.deidentify,
    )

    if dry_run:
        return _dry_run_share_link_out(
            body=body,
            resource_kind="folder",
            resource_id=str(folder.id),
            permissions=perms,
            grantor_subject_id=str(user.subject_id),
            valid_until=valid_until,
            deidentify=deidentify,
        )

    # 1) Folder-level grant — lets the grantee enumerate the folder.
    folder_grant = Grant(
        resource_kind="folder",
        resource_id=folder.id,
        grantor_subject_id=user.subject_id,
        grantee_subject_id=grantee_id,
        permissions=perms,
        conditions={"scope": "folder"},
        valid_until=valid_until,
        deidentify=deidentify,
        purpose=body.label or f"{body.access_level} access via folder {folder.name}",
    )
    db.add(folder_grant)
    await db.flush()

    # 2) Cascade — one Grant per item that has a first-class grant
    # target (mirrors ``api/folders.py::share_folder``). Sub-folders
    # are intentionally NOT walked recursively so the cascade stays
    # explicit + auditable.
    items = (
        (await db.execute(select(FolderItem).where(FolderItem.folder_id == folder.id)))
        .scalars()
        .all()
    )
    cascaded = [
        Grant(
            resource_kind=grant_kind,
            resource_id=item.resource_id,
            grantor_subject_id=user.subject_id,
            grantee_subject_id=grantee_id,
            parent_grant_id=folder_grant.id,
            permissions=perms,
            conditions={"scope": "folder", "folder_id": str(folder.id)},
            valid_until=valid_until,
            deidentify=deidentify,
            purpose=folder_grant.purpose,
        )
        for item in items
        if (grant_kind := _ITEM_KIND_TO_GRANT_KIND.get(item.resource_kind)) is not None
    ]
    db.add_all(cascaded)

    link: ShareLink | None = None
    generated_password: str | None = None
    if body.target.kind in ("link_public", "link_org"):
        plaintext_password = body.password
        if body.autogen_password:
            generated_password = _autogen_password()
            plaintext_password = generated_password
        link = ShareLink(
            grant_id=folder_grant.id,
            token=secrets.token_urlsafe(32),
            password_hash=hash_password(plaintext_password) if plaintext_password else None,
            label=body.label,
            max_uses=body.max_uses,
            mode=body.mode,
            recipient_name=(body.recipient_name or "").strip() or None,
            recipient_email=(body.recipient_email or "").strip().lower() or None,
            recipient_phone=(body.recipient_phone or "").strip() or None,
            ai_sponsorship_cap_cents=body.ai_sponsorship_cap_cents,
        )
        db.add(link)

    await db.commit()
    await db.refresh(folder_grant)
    if link is not None:
        await db.refresh(link)

    # Pre-warm the folder ZIP in the background (best-effort).
    if link is not None:
        try:
            await _prepare_folder_share_export(
                db,
                link=link,
                folder_id=folder.id,
                patient_id=patient.id,
                study_ids=list(study_ids),
                document_ids=list(document_ids),
                owner_subject_id=user.subject_id,
                is_admin=bool(user.is_admin),
            )
        except Exception:
            logger.warning("folder share pre-export enqueue failed", exc_info=True)

    audit_metadata: dict = {
        "grant_id": str(folder_grant.id),
        "target_kind": body.target.kind,
        "access_level": body.access_level,
        "download": body.download,
        "deidentify": folder_grant.deidentify,
        "expires_at": folder_grant.valid_until.isoformat() if folder_grant.valid_until else None,
        "share_mode": body.mode,
        "password_set": bool(body.password) or body.autogen_password,
        "password_autogen": body.autogen_password,
        "cascaded_count": len(cascaded),
    }
    if link is not None:
        audit_metadata["link_id"] = str(link.id)
    await audit.log(
        action="share_create",
        actor_subject_id=user.subject_id,
        resource_kind="folder",
        resource_id=folder.id,
        metadata=audit_metadata,
    )

    if link is not None:
        return _link_out(link, folder_grant, generated_password=generated_password)

    return ShareLinkOut(
        id=str(folder_grant.id),
        token="",
        url="",
        label=body.label,
        permissions=list(folder_grant.permissions),
        expires_at=folder_grant.valid_until.isoformat() if folder_grant.valid_until else None,
        revoked=False,
        use_count=0,
        max_uses=None,
        requires_password=False,
        created_at=folder_grant.created_at.isoformat(),
    )


@router.get("/studies/{study_id}/shares", response_model=list[ShareLinkOut])
async def list_share_links(
    request: Request,
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> list[ShareLinkOut]:
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    enforce_agent_patient_scope(request, study.patient_id)
    if not (user.is_admin or study.owner_subject_id == user.subject_id):
        raise HTTPException(status_code=403, detail="only the owner can view shares")

    rows = (
        await db.execute(
            select(ShareLink, Grant)
            .join(Grant, Grant.id == ShareLink.grant_id)
            .where(
                Grant.resource_kind == "study",
                Grant.resource_id == study.id,
            )
            .order_by(ShareLink.created_at.desc())
        )
    ).all()
    return [_link_out(link, grant) for link, grant in rows]


class ShareLinkUpdateIn(BaseModel):
    label: str | None = Field(default=None, max_length=255)
    access_level: str | None = Field(default=None, description="viewer | editor | manager")
    download: bool | None = Field(default=None, description="Allow DICOM download")
    expires_in_hours: int | None = Field(
        default=None, ge=0, description="Reset countdown from now; 0 = never expires"
    )
    max_uses: int | None = Field(
        default=None, ge=0, description="0 = unlimited; null = leave unchanged"
    )
    password: str | None = Field(
        default=None,
        min_length=0,
        max_length=256,
        description="Empty string clears password; null leaves unchanged.",
    )
    deidentify: bool | None = Field(
        default=None,
        description=(
            "Flip the PHI scrub flag on the underlying grant. Toggling "
            "invalidates any cached pre-export — the next reader sees "
            "an idle prep state and the FE re-triggers the build (so a "
            "deidentified ZIP is never served from a cache built with "
            "PHI on, or vice versa)."
        ),
    )


@router.patch("/share-links/{link_id}", response_model=ShareLinkOut)
async def update_share_link(
    link_id: uuid.UUID,
    body: ShareLinkUpdateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> ShareLinkOut:
    """Edit a share link in place.

    Useful for: rotating the password (just send a new ``password``),
    extending the validity window, broadening / narrowing access. Only
    the grantor (or an admin) can edit. Revoked links are immutable —
    create a new one rather than reviving a revoked share.
    """
    link = (await db.execute(select(ShareLink).where(ShareLink.id == link_id))).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="share link not found")
    grant = (await db.execute(select(Grant).where(Grant.id == link.grant_id))).scalar_one()
    if not (user.is_admin or grant.grantor_subject_id == user.subject_id):
        raise HTTPException(status_code=403, detail="only the grantor can edit")
    if grant.revoked_at is not None:
        raise HTTPException(
            status_code=409,
            detail="share link is revoked; create a new one instead",
        )

    fields_changed: list[str] = []
    if body.label is not None:
        link.label = body.label or None
        fields_changed.append("label")
    if body.access_level is not None or body.download is not None:
        new_level = body.access_level or permissions_to_level(grant.permissions)
        new_download = (
            body.download if body.download is not None else (SHARED_DOWNLOAD in grant.permissions)
        )
        grant.permissions = level_to_permissions(new_level, download=new_download)
        fields_changed.append("permissions")
    if body.expires_in_hours is not None:
        if body.expires_in_hours == 0:
            grant.valid_until = None
        else:
            grant.valid_until = datetime.now(UTC) + timedelta(hours=body.expires_in_hours)
        fields_changed.append("expires_in")
    if body.max_uses is not None:
        link.max_uses = body.max_uses or None
        fields_changed.append("max_uses")
    if body.password is not None:
        link.password_hash = hash_password(body.password) if body.password else None
        fields_changed.append("password")
    if body.deidentify is not None and body.deidentify != grant.deidentify:
        # Pseudonymization scope must never silently flip on a
        # cached artifact: the bytes on S3 reflect the value at
        # build time. Clear the prep pointer so the next read on
        # /shared/{token}/info returns no cache, the share lista
        # surfaces "preparation pending", and the FE / share-create
        # path can re-enqueue under the new flag (dedup keys
        # include ``deidentify`` so the new job lands on a
        # distinct artifact and never collides with the old).
        grant.deidentify = bool(body.deidentify)
        link.prepared_job_id = None
        fields_changed.append("deidentify")

    await db.commit()
    await db.refresh(link)
    await db.refresh(grant)
    await audit.log(
        action="share_update",
        actor_subject_id=user.subject_id,
        resource_kind=grant.resource_kind,
        resource_id=grant.resource_id,
        metadata={
            "grant_id": str(grant.id),
            "link_id": str(link.id),
            "fields": fields_changed,
        },
    )
    return _link_out(link, grant)


class ShareLinkListOut(BaseModel):
    items: list[ShareLinkOut]


@router.get("/share-links", response_model=ShareLinkListOut)
async def list_my_share_links(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    patient_id: uuid.UUID | None = None,
    include_revoked: bool = False,
    include_expired: bool = False,
    limit: int = 200,
) -> ShareLinkListOut:
    """List share-links the caller created (cross-patient by default).

    Filters: ``patient_id`` narrows to a single fascicolo (matches
    both patient-scoped and study-scoped grants whose study sits
    under that patient); ``include_revoked`` / ``include_expired``
    flip in the dismissed rows. Limit is capped at 500 to prevent
    accidentally loading every legacy share into memory.

    The response shape matches POST: callers can render the lista
    UI directly without massaging fields. ``prepared_status`` /
    ``prepared_progress_*`` come from a single LEFT JOIN to ``jobs``
    so the table doesn't N+1 against the worker's slow path.
    """
    from bvphoenix.db.models.jobs import Job

    capped = max(1, min(int(limit), 500))
    q = (
        select(ShareLink, Grant, Job.status, Job.progress_done, Job.progress_total)
        .join(Grant, Grant.id == ShareLink.grant_id)
        .outerjoin(Job, Job.id == ShareLink.prepared_job_id)
        .where(Grant.grantor_subject_id == user.subject_id)
    )
    if not include_revoked:
        q = q.where(Grant.revoked_at.is_(None))
    if not include_expired:
        q = q.where((Grant.valid_until.is_(None)) | (Grant.valid_until > datetime.now(UTC)))
    if patient_id is not None:
        # Match patient-scoped, study-scoped (study under the
        # patient), and folder-scoped (folder under the patient)
        # grants. The folder arm matters once
        # /api/folders/{id}/share-link starts producing share-link
        # rows: without it the new "Condivisioni" tab on the patient
        # page silently drops folder shares from the list.
        from bvphoenix.db.models import Folder as _Folder

        q = q.where(
            ((Grant.resource_kind == "patient") & (Grant.resource_id == patient_id))
            | (
                (Grant.resource_kind == "study")
                & Grant.resource_id.in_(
                    select(ImagingStudy.id).where(ImagingStudy.patient_id == patient_id)
                )
            )
            | (
                (Grant.resource_kind == "folder")
                & Grant.resource_id.in_(select(_Folder.id).where(_Folder.patient_id == patient_id))
            )
        )
    q = q.order_by(ShareLink.created_at.desc()).limit(capped)
    rows = (await db.execute(q)).all()
    items = []
    for row in rows:
        link, grant, j_status, j_done, j_total = row
        prep = (str(j_status), j_done, j_total) if j_status is not None else None
        items.append(_link_out(link, grant, prep=prep))
    return ShareLinkListOut(items=items)


class ShareLinkExtendIn(BaseModel):
    """Body for ``POST /api/share-links/{id}/extend``.

    The dialog only offers a small set of "humane" extensions — 1
    month / 3 months / 6 months / 1 year — so we constrain the API
    to those values rather than letting any positive integer in.
    Months are interpreted as 30-day blocks (no calendar-edge
    surprises across DST + leap years; the UI label says "circa N
    mesi" so the user expects the heuristic).
    """

    add_months: int = Field(..., ge=1, le=24)


@router.post(
    "/share-links/{link_id}/extend",
    response_model=ShareLinkOut,
)
async def extend_share_link(
    link_id: uuid.UUID,
    body: ShareLinkExtendIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> ShareLinkOut:
    """Roll forward ``grant.valid_until`` by N 30-day blocks.

    If the link had ``valid_until = NULL`` (legacy "no expiry") the
    extend stamps an expiry exactly N months from now — turning a
    permanent link into a bounded one is the safe direction. Only
    the grantor (or admin) can extend, and only on non-revoked
    links; revoked is intentional and stays terminal.
    """
    link = (await db.execute(select(ShareLink).where(ShareLink.id == link_id))).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="share link not found")
    grant = (await db.execute(select(Grant).where(Grant.id == link.grant_id))).scalar_one()
    if not (user.is_admin or grant.grantor_subject_id == user.subject_id):
        raise HTTPException(status_code=403, detail="only the grantor can extend")
    if grant.revoked_at is not None:
        raise HTTPException(
            status_code=409,
            detail="share link is revoked; create a new one instead",
        )
    base = grant.valid_until or datetime.now(UTC)
    grant.valid_until = base + timedelta(days=30 * int(body.add_months))
    await db.commit()
    await db.refresh(link)
    await db.refresh(grant)
    await audit.log(
        action="share_extend",
        actor_subject_id=user.subject_id,
        resource_kind=grant.resource_kind,
        resource_id=grant.resource_id,
        metadata={
            "grant_id": str(grant.id),
            "link_id": str(link.id),
            "add_months": int(body.add_months),
            "new_valid_until": grant.valid_until.isoformat(),
        },
    )
    return _link_out(link, grant)


class NotifyShareLinkIn(BaseModel):
    """Body for ``POST /api/share-links/{id}/notify``.

    The recipient address comes from the share-link row; the body is
    optional (a custom intro line the grantor wants to add) and the
    autogenerated password (if any) is *not* re-sent here — it must
    have been delivered out-of-band when the link was created. Re-
    sending the password by email defeats the purpose of OOB delivery.
    """

    custom_message: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Optional grantor message prepended to the standard body "
            "(e.g. 'as discussed at the consult yesterday'). Plain text; "
            "HTML is rendered as text in the recipient's client."
        ),
    )
    locale: str | None = Field(
        default=None,
        pattern="^[a-zA-Z-]{2,16}$",
        description=(
            "Email body / subject locale ('it' | 'en'). Defaults to the "
            "request's Accept-Language. The grantor's preference is the "
            "best signal we have without a recipient-side preference; if "
            "we ever store a preferred_locale on PatientContact, switch "
            "to that."
        ),
    )


async def _resolve_grantor_name(
    db: AsyncSession,
    subject_id: uuid.UUID | None,
    lang: str,
    *,
    fallback: str | None = None,
) -> str:
    """Human-readable attribution for the invitation body.

    Display name lives on the Subject row (uniform across users and
    service principals); fall back to the email for legacy / partial
    accounts so the message is always attributable to someone.
    """
    from bvphoenix.db.models.principals import Subject

    subj = None
    if subject_id is not None:
        subj = (
            await db.execute(select(Subject).where(Subject.id == subject_id))
        ).scalar_one_or_none()
    generic = "a colleague" if lang == "en" else "un collega"
    return (subj.display_name if subj else None) or fallback or generic


async def _build_share_invitation(
    db: AsyncSession,
    link: ShareLink,
    grant: Grant,
    *,
    locale: str | None,
    custom_message: str | None,
    grantor_name: str,
) -> EmailMessage:
    """Render the invitation for ``link``.

    Extracted from the endpoint so the delivery ledger can rebuild the
    message on a retry instead of storing the rendered body. Rebuilding
    also means a replay reflects the link's *current* state (expiry,
    de-identification flag) rather than a stale snapshot.
    """
    settings = get_settings()
    base_url = (settings.public_frontend_url or "").rstrip("/")
    # Same canonical landing as ``_link_out.url`` — emails the
    # branded ``/info`` page so the recipient sees the privacy
    # banner + cached download CTA instead of the bare verify form.
    landing_url = (
        f"{base_url}/shared/{link.token}/info" if base_url else f"/shared/{link.token}/info"
    )
    lang = normalize_email_locale(locale)

    # Compose study summary. We resolve the resource the grant points
    # at — for studies, study_description + modalities; for fascicolo
    # shares, the patient pseudonym.
    study_summary = "shared study" if lang == "en" else "studio condiviso"
    if grant.resource_kind == "study":
        study = (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == grant.resource_id))
        ).scalar_one_or_none()
        if study is not None:
            modalities = ", ".join(study.modalities or []) or "—"
            label = study.study_description or study.study_instance_uid
            unknown_date = "date unknown" if lang == "en" else "data ignota"
            date = str(study.study_date) if study.study_date else unknown_date
            study_summary = f"{label} · {modalities} · {date}"
    elif grant.resource_kind == "patient":
        # Patient-scoped (fascicolo) shares: don't leak the real name.
        # The display_name already respects the share's deidentify
        # flag at the persistence layer for shared grantees.
        study_summary = "patient health record" if lang == "en" else "fascicolo paziente"

    if grant.valid_until:
        expires_label = grant.valid_until.strftime("%Y-%m-%d %H:%M UTC")
    else:
        expires_label = "no expiry" if lang == "en" else "nessuna scadenza"

    return build_share_invitation_email(
        to=link.recipient_email or "",
        recipient_name=link.recipient_name,
        grantor_name=grantor_name,
        study_summary=study_summary,
        landing_url=landing_url,
        expires_label=expires_label,
        deidentified=bool(grant.deidentify),
        autogen_password=None,  # never re-emit; OOB only
        custom_message=custom_message,
        locale=locale,
    )


async def _rebuild_share_invitation(db: AsyncSession, row: EmailDelivery) -> EmailMessage | None:
    """Ledger builder for ``purpose='share_invitation'``.

    Returns ``None`` when the link is gone or revoked, which parks the
    row as ``dead_letter`` instead of retrying a dead invitation.

    Known limitation, deliberate: the grantor's optional ``custom_message``
    is NOT reproduced. It is free text that may carry clinical context,
    and the ledger's no-PHI invariant is worth more than reproducing an
    intro line on the rare retry path. The standard invitation is
    complete and actionable without it.
    """
    if row.share_link_id is None:
        return None
    link = (
        await db.execute(select(ShareLink).where(ShareLink.id == row.share_link_id))
    ).scalar_one_or_none()
    if link is None or not link.recipient_email:
        return None
    grant = (await db.execute(select(Grant).where(Grant.id == link.grant_id))).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None:
        return None
    lang = normalize_email_locale(row.locale)
    grantor_name = await _resolve_grantor_name(db, grant.grantor_subject_id, lang)
    return await _build_share_invitation(
        db,
        link,
        grant,
        locale=row.locale,
        custom_message=None,
        grantor_name=grantor_name,
    )


register_delivery_builder("share_invitation", _rebuild_share_invitation)


class NotifyShareLinkOut(BaseModel):
    """Truthful outcome of a notify attempt.

    ``sent`` used to be hard-coded ``True``: the field existed but no
    code path could set it to ``False``, so a grantor was told the
    consultant had been emailed even when nothing left the pod. It is
    now an observation, and ``status`` distinguishes the two ways of
    not-being-sent, which need different responses from the caller.
    """

    sent: bool
    to: str
    # Ledger row id — quotable at support time, and the handle for a
    # manual requeue.
    delivery_id: str
    # "sent"   — the relay accepted it
    # "queued" — failed on a retriable error; the ledger will retry
    # "failed" — refused in a way no retry will fix
    status: Literal["sent", "queued", "failed"]
    # Discriminated transport code (services/email.py ERROR_*), so the
    # UI can explain a bad address differently from a dead relay.
    error_code: str | None = None


@router.post(
    "/share-links/{link_id}/notify",
    response_model=NotifyShareLinkOut,
    status_code=200,
)
@limiter.limit(SHARE_NOTIFY_LIMIT)
async def notify_share_link(
    request: Request,
    response: Response,
    link_id: uuid.UUID,
    body: NotifyShareLinkIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> NotifyShareLinkOut:
    """Email the share-link recipient with a templated invitation.

    The link must already be created (this endpoint does not create
    one) and must carry a ``recipient_email``. Only the grantor (or
    an admin) can trigger the send. Audit row ``share_email_sent``
    closes the chain: created → emailed → claimed/accessed.

    The body deliberately does *not* include the password; if the
    link is password-protected the user is reminded to deliver the
    password out-of-band (SMS, voice, in-person), matching how
    transactional credentials are handled in any clinical workflow.
    """
    link = (await db.execute(select(ShareLink).where(ShareLink.id == link_id))).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="share link not found")
    grant = (await db.execute(select(Grant).where(Grant.id == link.grant_id))).scalar_one()
    if not (user.is_admin or grant.grantor_subject_id == user.subject_id):
        raise HTTPException(status_code=403, detail="only the grantor can notify")
    if grant.revoked_at is not None:
        raise HTTPException(status_code=409, detail="share link is revoked")
    if not link.recipient_email:
        raise HTTPException(
            status_code=422,
            detail="share link has no recipient_email; set it via PATCH first",
        )

    # The locale drives the study summary and the expiry label as well
    # as the template, so it is resolved once and threaded through.
    cookie_locale = request.cookies.get("BVP_LOCALE")
    accept_lang = request.headers.get("accept-language")
    chosen_locale = body.locale or cookie_locale or accept_lang
    lang = normalize_email_locale(chosen_locale)
    grantor_name = await _resolve_grantor_name(db, user.subject_id, lang, fallback=user.email)
    msg = await _build_share_invitation(
        db,
        link,
        grant,
        locale=chosen_locale,
        custom_message=body.custom_message,
        grantor_name=grantor_name,
    )
    # Persist the intent BEFORE attempting delivery, so a failure is a
    # queued row an operator can inspect and replay rather than a log
    # line in a pod that rotates.
    delivery = await enqueue_delivery(
        db,
        purpose="share_invitation",
        recipient_email=link.recipient_email,
        subject_line=msg.subject,
        locale=lang,
        share_link_id=link.id,
    )
    outcome = await attempt_delivery(db, delivery.id, message=msg)

    if outcome.ok:
        delivery_status: Literal["sent", "queued", "failed"] = "sent"
    elif outcome.retriable:
        delivery_status = "queued"
    else:
        delivery_status = "failed"

    # The audit row records what happened, not what we hoped. Writing
    # ``share_email_sent`` unconditionally is how three undelivered
    # messages on 2026-07-31 acquired durable attestations of delivery;
    # the chain documented below (share_create -> share_email_sent ->
    # share_access) is only evidence if the middle link is earned.
    await audit.log(
        action="share_email_sent" if outcome.ok else "share_email_failed",
        actor_subject_id=user.subject_id,
        resource_kind=grant.resource_kind,
        resource_id=grant.resource_id,
        metadata={
            "grant_id": str(grant.id),
            "link_id": str(link.id),
            "to": link.recipient_email,
            "deidentified": bool(grant.deidentify),
            "delivery_id": str(delivery.id),
            "delivery_status": delivery_status,
            # error_code only; error_detail names hosts and ports and
            # stays server-side.
            "error_code": outcome.error_code,
        },
    )

    # 202 for "we own it and will retry", 502 for "the relay refused it
    # and retrying will not help". Both carry the same body so the
    # client renders one branch off ``status``.
    if delivery_status == "queued":
        response.status_code = status.HTTP_202_ACCEPTED
    elif delivery_status == "failed":
        response.status_code = status.HTTP_502_BAD_GATEWAY

    return NotifyShareLinkOut(
        sent=outcome.ok,
        to=link.recipient_email,
        delivery_id=str(delivery.id),
        status=delivery_status,
        error_code=outcome.error_code,
    )


class ClaimShareLinkIn(BaseModel):
    """Body for ``POST /api/share-links/{token}/claim``.

    The recipient of an anonymous share link converts their token-only
    access into a real account by setting a password (and optionally a
    display name). The link's ``recipient_email`` is reused as the new
    account's email.
    """

    password: str = Field(min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=255)
    # Recipient-provided email, honoured ONLY when the link itself carries
    # no ``recipient_email`` (a bare anonymous link not addressed to a
    # specific person). When the link IS addressed, that email wins and a
    # mismatching value here is refused — a forwarded, person-addressed
    # link must not be redirected onto a different identity.
    email: EmailStr | None = None


class ClaimShareLinkOut(BaseModel):
    """Response of a successful claim — pretty close to the registration
    response shape so the frontend can drop the same logic in.
    """

    subject_id: str
    email: str
    access_token: str
    expires_in: int


async def _perform_claim(
    db: AsyncSession,
    link: ShareLink,
    *,
    password: str,
    display_name: str | None,
    email_override: str | None,
) -> tuple[User, Grant]:
    """Core of "turn a share link into a real account", shared by the
    token route (``/share-links/{token}/claim``) and the in-app session
    route (``/share-sessions/claim``).

    Validates the link, creates the Subject + User, repoints the grant
    (and any contact delegation) off ``PUBLIC`` onto the new account,
    marks the link claimed, materialises the wallet sponsorship
    (best-effort), commits, and returns ``(user, grant)``. Raises
    ``HTTPException`` on every precondition so the UI shows a precise
    message. The caller owns audit logging + token/cookie minting.
    """
    if link.mode not in ("anonymous", "claim"):
        raise HTTPException(status_code=400, detail="this link cannot be converted to an account")
    if link.claimed_at is not None:
        raise HTTPException(status_code=409, detail="this link has already been claimed")

    grant = (await db.execute(select(Grant).where(Grant.id == link.grant_id))).scalar_one_or_none()
    now = datetime.now(UTC)
    if grant is None or grant.revoked_at is not None:
        raise HTTPException(status_code=410, detail="grant has been revoked")
    if grant.valid_until is not None and grant.valid_until < now:
        raise HTTPException(status_code=410, detail="share link has expired")

    # Email resolution. An *addressed* link (created with a
    # recipient_email) pins the identity: a forwarded link must not be
    # redirected onto a different account, so a mismatching override is
    # refused. A *bare* anonymous link carries no addressee, so the
    # recipient supplies their own email here.
    addressed = (link.recipient_email or "").strip().lower()
    override = (email_override or "").strip().lower()
    if addressed:
        if override and override != addressed:
            raise HTTPException(
                status_code=403, detail="this link was addressed to a different recipient"
            )
        email = addressed
    else:
        if not override:
            raise HTTPException(
                status_code=400, detail="an email is required to create an account for this link"
            )
        email = override

    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        # The recipient already has an account; we can't mint a second one
        # for the same email. They attach the grant via bind instead (the
        # FE flips to "log in and connect" on this 409).
        raise HTTPException(
            status_code=409,
            detail=(
                "an account already exists for this email; log in and open "
                "this link again to attach it to your account"
            ),
        )

    # Lazy import keeps the sharing module light (Subject lives in
    # db.models.principals).
    from bvphoenix.db.models.principals import Subject

    dn = (display_name or link.recipient_name or email).strip()
    subject = Subject(kind="user", display_name=dn)
    db.add(subject)
    await db.flush()
    user = User(subject_id=subject.id, email=email, password_hash=hash_password(password))
    db.add(user)
    await db.flush()

    grant.grantee_subject_id = user.subject_id
    link.claimed_by_subject_id = user.subject_id
    link.claimed_at = now

    from bvphoenix.db.models import PatientContact

    contact = (
        await db.execute(
            select(PatientContact).where(PatientContact.delegation_share_link_id == link.id)
        )
    ).scalar_one_or_none()
    if contact is not None:
        contact.delegation_subject_id = user.subject_id

    if link.ai_sponsorship_cap_cents and grant.resource_kind == "patient":
        try:
            from bvphoenix.services.sponsorship import create_sponsorship

            sp = await create_sponsorship(
                db,
                sponsor_subject_id=grant.grantor_subject_id,
                sponsored_subject_id=user.subject_id,
                scope_kind="patient",
                scope_id=grant.resource_id,
                cap_cents=int(link.ai_sponsorship_cap_cents),
                purpose=f"share-link claim ({link.label or link.recipient_name or 'unnamed'})",
            )
            link.ai_sponsorship_id = sp.id
        except Exception as exc:
            logger.warning("share_link claim sponsorship materialisation failed: %s", exc)

    await db.commit()
    await db.refresh(user)
    return user, grant


async def _perform_bind(db: AsyncSession, link: ShareLink, user: User) -> Grant:
    """Core of "attach a PUBLIC-held link grant to the current account",
    shared by the token route (``/share-links/{token}/bind``) and the
    session route (``/share-sessions/bind``). Idempotent. Enforces the
    addressee email-match so a forwarded link can't be redirected onto a
    different account. Caller owns audit logging."""
    if link.mode not in ("anonymous", "claim"):
        raise HTTPException(status_code=400, detail="this link cannot be bound to an account")

    grant = (await db.execute(select(Grant).where(Grant.id == link.grant_id))).scalar_one_or_none()
    now = datetime.now(UTC)
    if grant is None or grant.revoked_at is not None:
        raise HTTPException(status_code=410, detail="grant has been revoked")
    if grant.valid_until is not None and grant.valid_until < now:
        raise HTTPException(status_code=410, detail="share link has expired")

    if grant.grantee_subject_id == user.subject_id:
        return grant  # idempotent
    if grant.grantee_subject_id != PUBLIC_SUBJECT_ID:
        raise HTTPException(status_code=409, detail="this link is already attached to an account")

    recipient_email = (link.recipient_email or "").strip().lower()
    if not recipient_email or recipient_email != user.email.lower():
        raise HTTPException(
            status_code=403, detail="this link was addressed to a different recipient"
        )

    grant.grantee_subject_id = user.subject_id
    if link.claimed_at is None:
        link.claimed_by_subject_id = user.subject_id
        link.claimed_at = now

    from bvphoenix.db.models import PatientContact

    contact = (
        await db.execute(
            select(PatientContact).where(PatientContact.delegation_share_link_id == link.id)
        )
    ).scalar_one_or_none()
    if contact is not None:
        contact.delegation_subject_id = user.subject_id

    await db.commit()
    return grant


@router.post("/share-links/{token}/claim", response_model=ClaimShareLinkOut)
async def claim_share_link(
    request: Request,
    response: Response,
    token: str,
    body: ClaimShareLinkIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> ClaimShareLinkOut:
    """Convert an anonymous share link into a real user account.

    Preconditions enforced explicitly (no silent failures so the UI can
    surface the right message):

    * The link exists and is not revoked.
    * The link was created in ``mode='anonymous'`` (claim from a
      regular ``mode='claim'`` link is a separate flow because that
      link already targets a known user).
    * It has not been claimed yet (``claimed_at IS NULL``).
    * It carries a ``recipient_email`` (the one captured at create
      time, used as the new account's primary email).
    * The email is not already taken by another account — we don't
      try to merge identities here.

    On success:

    * A new ``Subject`` (kind='user') and ``User`` row are created
      with the supplied password.
    * The link's ``Grant`` is rewritten so ``grantee_subject_id``
      points at the new user instead of ``PUBLIC_SUBJECT_ID``. The
      grant stays valid; the user keeps the same access scope.
    * The link is marked as claimed (``claimed_by_subject_id`` +
      ``claimed_at``); historical
      ``commits.share_link_id`` rows stay attributed to the link
      so the "modality A" badge survives the conversion.
    * A regular access JWT is minted and returned.
    """
    link = (
        await db.execute(select(ShareLink).where(ShareLink.token == token))
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="share link not found")

    user, grant = await _perform_claim(
        db,
        link,
        password=body.password,
        display_name=body.display_name,
        email_override=body.email,
    )

    settings = get_settings()
    access_token = issue_access_token(
        subject_id=user.subject_id,
        email=user.email,
        is_admin=False,
    )

    await audit.log(
        action="share_link_claimed",
        actor_subject_id=user.subject_id,
        resource_kind="share_link",
        resource_id=link.id,
        metadata={
            "grant_id": str(grant.id),
            "share_token": link.token,
            "recipient_name": link.recipient_name,
        },
    )

    # Same browser-session handoff as password login: the freshly
    # created account is logged in via the HttpOnly cookie so the
    # destination page authenticates without the (now no-op) localStorage
    # bearer token.
    set_session_cookie(response, request, access_token, max_age=settings.jwt_expires_seconds)

    return ClaimShareLinkOut(
        subject_id=str(user.subject_id),
        email=user.email,
        access_token=access_token,
        expires_in=settings.jwt_expires_seconds,
    )


class BindShareLinkOut(BaseModel):
    """Result of attaching a claim/anonymous link's grant to the
    already-authenticated caller."""

    grant_id: str
    resource_kind: str
    resource_id: str
    permissions: list[str]


@router.post("/share-links/{token}/bind", response_model=BindShareLinkOut)
async def bind_share_link(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> BindShareLinkOut:
    """Attach a magic-link's grant to the CURRENT logged-in account.

    Companion to ``/claim`` for the case where the recipient already has
    an account (so a new one can't be minted) — the path a delegate hits
    when they registered separately and then open the delegation link.

    Security: the caller proves possession of the link (the token) AND is
    authenticated, and we still require ``user.email == recipient_email``
    so a forwarded link can't be redirected onto a different account than
    the grantor addressed. On a PHI platform the grantor picked a specific
    recipient; possession alone must not let an arbitrary account inherit
    access. Only PUBLIC-held grants are bindable, so a grant already on a
    real subject can't be hijacked.

    Idempotent: re-binding a link already attached to the same user is a
    no-op success.
    """
    link = (
        await db.execute(select(ShareLink).where(ShareLink.token == token))
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="share link not found")

    grant = await _perform_bind(db, link, user)

    await audit.log(
        action="share_link_bound",
        actor_subject_id=user.subject_id,
        resource_kind="share_link",
        resource_id=link.id,
        metadata={"grant_id": str(grant.id), "share_token": link.token},
    )
    return BindShareLinkOut(
        grant_id=str(grant.id),
        resource_kind=grant.resource_kind,
        resource_id=str(grant.resource_id),
        permissions=list(grant.permissions),
    )


# ---------------------------------------------------------------------------
# In-app "keep this access — create an account" (token-less, session-based).
#
# After ``verify`` the recipient browses as an anonymous share session whose
# HttpOnly JWT carries the originating ``share_link_id`` (pinned onto
# ``request.state`` by ``auth.deps``). These three endpoints let the in-app
# banner reconcile the share to a real account WITHOUT the share token ever
# touching JS — the token-based ``/share-links/{token}/claim|bind`` routes
# above stay for the on-landing (/info) flow where the token is in the URL.
# ---------------------------------------------------------------------------


class ShareSessionOut(BaseModel):
    """What the current browser session can reconcile. Returned only for an
    anonymous share-link session; ``null`` for a normal logged-in account."""

    share_link_id: str
    resource_kind: str
    resource_id: str
    claimable: bool
    bindable: bool
    recipient_email_known: bool


class BindSessionIn(BaseModel):
    share_link_id: uuid.UUID


@router.get("/share-sessions/current", response_model=ShareSessionOut | None)
async def current_share_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> ShareSessionOut | None:
    """Describe the current anonymous share session so the in-app banner can
    offer "keep this access — create an account". ``null`` for a normal
    account (no ``share_link_id`` in the session JWT)."""
    sid = getattr(request.state, "share_link_id", None)
    if sid is None:
        return None
    link = (await db.execute(select(ShareLink).where(ShareLink.id == sid))).scalar_one_or_none()
    if link is None:
        return None
    grant = (await db.execute(select(Grant).where(Grant.id == link.grant_id))).scalar_one_or_none()
    if grant is None:
        return None
    now = datetime.now(UTC)
    grant_alive = grant.revoked_at is None and (
        grant.valid_until is None or grant.valid_until > now
    )
    recipient_email_known = bool(link.recipient_email)
    recipient_has_account = False
    if link.recipient_email:
        recipient_has_account = (
            await db.execute(
                select(User.subject_id).where(User.email == link.recipient_email.lower())
            )
        ).first() is not None
    attachable = (
        link.mode in ("anonymous", "claim")
        and link.claimed_at is None
        and grant_alive
        and grant.grantee_subject_id == PUBLIC_SUBJECT_ID
    )
    return ShareSessionOut(
        share_link_id=str(link.id),
        resource_kind=grant.resource_kind,
        resource_id=str(grant.resource_id),
        claimable=attachable and not recipient_has_account,
        bindable=attachable and recipient_has_account and recipient_email_known,
        recipient_email_known=recipient_email_known,
    )


@router.post("/share-sessions/claim", response_model=ClaimShareLinkOut)
async def claim_current_share_session(
    request: Request,
    response: Response,
    body: ClaimShareLinkIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> ClaimShareLinkOut:
    """In-app account creation for an anonymous share session: reads the
    originating link from the session JWT (no token in JS), creates the
    account, reconciles the grant, and logs the new account in via the
    session cookie."""
    sid = getattr(request.state, "share_link_id", None)
    if sid is None:
        raise HTTPException(status_code=403, detail="not an anonymous share session")
    link = (await db.execute(select(ShareLink).where(ShareLink.id == sid))).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="share link not found")

    new_user, grant = await _perform_claim(
        db,
        link,
        password=body.password,
        display_name=body.display_name,
        email_override=body.email,
    )
    await audit.log(
        action="share_link_claimed",
        actor_subject_id=new_user.subject_id,
        resource_kind="share_link",
        resource_id=link.id,
        metadata={"grant_id": str(grant.id), "share_token": link.token, "via": "session"},
    )
    settings = get_settings()
    access_token = issue_access_token(
        subject_id=new_user.subject_id, email=new_user.email, is_admin=False
    )
    set_session_cookie(response, request, access_token, max_age=settings.jwt_expires_seconds)
    return ClaimShareLinkOut(
        subject_id=str(new_user.subject_id),
        email=new_user.email,
        access_token=access_token,
        expires_in=settings.jwt_expires_seconds,
    )


@router.post("/share-sessions/bind", response_model=BindShareLinkOut)
async def bind_share_session(
    body: BindSessionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
) -> BindShareLinkOut:
    """Attach a share link's grant to the CURRENT logged-in account,
    addressed by ``share_link_id`` (the banner passes the id from
    ``/share-sessions/current`` through the login redirect, so no token
    round-trips through JS). Same email-match guard as the token route."""
    link = (
        await db.execute(select(ShareLink).where(ShareLink.id == body.share_link_id))
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="share link not found")
    grant = await _perform_bind(db, link, user)
    await audit.log(
        action="share_link_bound",
        actor_subject_id=user.subject_id,
        resource_kind="share_link",
        resource_id=link.id,
        metadata={"grant_id": str(grant.id), "share_token": link.token, "via": "session"},
    )
    return BindShareLinkOut(
        grant_id=str(grant.id),
        resource_kind=grant.resource_kind,
        resource_id=str(grant.resource_id),
        permissions=list(grant.permissions),
    )


@router.delete("/share-links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_share_link(
    link_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    audit: AuditDep,
    purge: bool = False,
) -> None:
    """Soft-revoke or hard-delete a share link.

    Default behaviour (``DELETE`` without query) is **revoke**: the
    grant gets ``revoked_at`` stamped, the row stays in the listing as
    a "revoked" entry so the audit trail and use_count are preserved.

    With ``?purge=true`` the row is hard-deleted along with its grant
    — only allowed on already-revoked links so the clinician must
    explicitly retire-then-purge in two steps. Audit log gets a
    ``share_purge`` entry that survives the row deletion.
    """
    link = (await db.execute(select(ShareLink).where(ShareLink.id == link_id))).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="share link not found")
    grant = (await db.execute(select(Grant).where(Grant.id == link.grant_id))).scalar_one()
    if not (user.is_admin or grant.grantor_subject_id == user.subject_id):
        raise HTTPException(status_code=403, detail="only the grantor can revoke")

    if purge:
        if grant.revoked_at is None:
            raise HTTPException(
                status_code=409,
                detail="revoke the link before purging — keeps audit trail clean",
            )
        await audit.log(
            action="share_purge",
            actor_subject_id=user.subject_id,
            resource_kind=grant.resource_kind,
            resource_id=grant.resource_id,
            metadata={"grant_id": str(grant.id), "link_id": str(link.id)},
        )
        # The grant cascades into the share_link row via FK; deleting
        # the grant is enough.
        await db.delete(grant)
        await db.commit()
        return

    grant.revoked_at = datetime.now(UTC)
    grant.revoked_by_subject_id = user.subject_id
    await db.commit()

    # Cancel the prep job too — but only if no other still-active
    # share-link is using the same cached artifact. The dedup
    # primitive (kind, owner, scope, canonical_input) collapses
    # identical shares onto a single job, so a different recipient
    # may still be waiting on the same archive. Only orphaning the
    # job releases it from the JobsTray badge so the operator's
    # "operazioni in corso" count drops to 0 right after revoke.
    cancelled_prep_job = False
    if link.prepared_job_id is not None:
        from bvphoenix.db.models.jobs import Job as _JobModel

        peers = (
            await db.execute(
                select(ShareLink.id)
                .join(Grant, Grant.id == ShareLink.grant_id)
                .where(
                    ShareLink.prepared_job_id == link.prepared_job_id,
                    ShareLink.id != link.id,
                    Grant.revoked_at.is_(None),
                )
            )
        ).first()
        if peers is None:
            # No other live share leans on this cached job — safe
            # to cancel. Use the same flag the worker honours via
            # ExportCancelledError so an in-flight build aborts
            # cleanly.
            job = (
                await db.execute(select(_JobModel).where(_JobModel.id == link.prepared_job_id))
            ).scalar_one_or_none()
            if job is not None and job.status in ("queued", "running"):
                job.status = "cancelled"
                job.finished_at = datetime.now(UTC)
                await db.commit()
                cancelled_prep_job = True
    await audit.log(
        action="share_revoke",
        actor_subject_id=user.subject_id,
        resource_kind=grant.resource_kind,
        resource_id=grant.resource_id,
        metadata={
            "grant_id": str(grant.id),
            "link_id": str(link.id),
            "cancelled_prep_job": cancelled_prep_job,
        },
    )


@router.get("/shared/{token}/info", response_model=ShareInfoOut)
@limiter.limit(SHARE_METADATA_LIMIT)
async def share_link_info(
    request: Request,
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ShareInfoOut:
    # Granular error reasons. The FE renders distinct copy per code so
    # the recipient knows whether to ask for a fresh link
    # (revoked / max-uses) vs whether to retry the URL (transient
    # not_found). Bare "not found" used to conflate "I mistyped" with
    # "the grantor revoked the share" which are different fixes for
    # the recipient. Status codes follow RFC 9110: 404 for tokens
    # that never existed, 410 Gone for tokens whose backing share
    # was deliberately retired.
    row = (
        await db.execute(
            select(ShareLink, Grant)
            .join(Grant, Grant.id == ShareLink.grant_id)
            .where(ShareLink.token == token)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    link, grant = row
    if grant.revoked_at is not None:
        raise HTTPException(status_code=410, detail={"reason": "revoked"})
    if grant.valid_until and grant.valid_until < datetime.now(UTC):
        raise HTTPException(status_code=410, detail={"reason": "expired"})
    if link.max_uses and link.use_count >= link.max_uses:
        raise HTTPException(status_code=410, detail={"reason": "max_uses_reached"})

    # Resolve the resource based on grant kind. ImagingStudy-scoped shares show
    # the study description / modalities / date. Patient-scoped shares
    # (the "Drive-style" fascicolo share) show the patient name and
    # aggregate modalities from every study filed under the patient — no
    # single study_date is meaningful so we leave that null.
    title: str | None = None
    modalities: list[str] = []
    study_date: str | None = None

    if grant.resource_kind == "study":
        study = (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == grant.resource_id))
        ).scalar_one_or_none()
        if study is None:
            raise HTTPException(status_code=410, detail={"reason": "resource_deleted"})
        title = study.study_description
        modalities = list(study.modalities or [])
        study_date = str(study.study_date) if study.study_date else None
    elif grant.resource_kind == "patient":
        patient = (
            await db.execute(select(Patient).where(Patient.id == grant.resource_id))
        ).scalar_one_or_none()
        if patient is None:
            raise HTTPException(status_code=410, detail={"reason": "resource_deleted"})
        title = f"Fascicolo: {patient.display_name}"
        # Union of modality arrays across all studies linked to the patient.
        rows = (
            await db.execute(
                select(ImagingStudy.modalities).where(ImagingStudy.patient_id == patient.id)
            )
        ).all()
        seen: set[str] = set()
        for (mods,) in rows:
            for m in mods or []:
                if m not in seen:
                    seen.add(m)
                    modalities.append(m)
        modalities.sort()
    elif grant.resource_kind == "folder":
        from bvphoenix.db.models import Folder as _Folder
        from bvphoenix.db.models import FolderItem as _FolderItem

        folder = (
            await db.execute(select(_Folder).where(_Folder.id == grant.resource_id))
        ).scalar_one_or_none()
        if folder is None:
            raise HTTPException(status_code=410, detail={"reason": "resource_deleted"})
        title = f"Cartella: {folder.name}"
        # Modalities = union over the in-folder studies. Walks
        # ``FolderItem`` rows directly to avoid the recursive scope
        # resolver (which 422s on empty folders); a folder share
        # with zero items is a valid state for the landing page,
        # we just render an empty modality list.
        item_rows = (
            await db.execute(
                select(_FolderItem.resource_kind, _FolderItem.resource_id).where(
                    _FolderItem.folder_id == folder.id
                )
            )
        ).all()
        in_scope_studies = [rid for kind, rid in item_rows if kind == "study"]
        if in_scope_studies:
            mod_rows = (
                await db.execute(
                    select(ImagingStudy.modalities).where(ImagingStudy.id.in_(in_scope_studies))
                )
            ).all()
            seen: set[str] = set()
            for (mods,) in mod_rows:
                for m in mods or []:
                    if m not in seen:
                        seen.add(m)
                        modalities.append(m)
            modalities.sort()
    else:
        # Other grant kinds (dataset, series, ...) aren't reachable
        # via the sharing UX yet. 422 is the right code: the link
        # itself is fine, but its resource_kind isn't supported by
        # the public landing page yet — distinct from "the link is
        # broken" (404/410).
        raise HTTPException(
            status_code=422,
            detail={"reason": "unsupported_kind", "resource_kind": grant.resource_kind},
        )

    uses_remaining = max(link.max_uses - link.use_count, 0) if link.max_uses is not None else None
    now = datetime.now(UTC)
    grant_alive = grant.revoked_at is None and (
        grant.valid_until is None or grant.valid_until > now
    )
    # A PUBLIC-held grant on an un-claimed link is attachable to a real
    # account. ``claim`` (delegation / fascicolo share) links qualify too,
    # not just ``anonymous`` ones — the old anonymous-only gate is exactly
    # why a delegate could never turn their magic link into working access
    # and kept landing on "patient not found". If an account already
    # exists for the recipient we can't mint a second one, so the link is
    # *bindable* (log in + /bind) rather than *claimable* (create account).
    recipient_has_account = False
    if link.recipient_email:
        recipient_has_account = (
            await db.execute(
                select(User.subject_id).where(User.email == link.recipient_email.lower())
            )
        ).first() is not None
    # A PUBLIC-held grant on an un-claimed anonymous/claim link is
    # attachable to a real account. We no longer require ``recipient_email``
    # on the link: a bare anonymous link is still claimable because the
    # recipient supplies their own email at account-creation time
    # (universal "register once inside" — see _perform_claim). ``bindable``
    # still needs the addressee email so the email-match guard can run.
    attachable = (
        link.mode in ("anonymous", "claim")
        and link.claimed_at is None
        and grant_alive
        and grant.grantee_subject_id == PUBLIC_SUBJECT_ID
    )
    claimable = attachable and not recipient_has_account
    bindable = attachable and recipient_has_account and bool(link.recipient_email)

    # Pre-flight payload size for the landing page. A study share
    # aggregates over its instances; a patient share over every
    # instance under every study filed for the patient. Both queries
    # are bounded SUMs against indexed FKs.
    #
    # Schema reminder: ``Instance`` has no direct ``study_id``
    # column — instances link to series, series link to
    # imaging_studies. Earlier revisions of this handler joined on
    # ``Instance.study_id`` directly, which raised AttributeError at
    # query compile time and 500'd every /info call for shares
    # whose token actually existed (caught when a freshly issued
    # link blew up beta.127). Two-hop join via Series is the
    # correct shape and matches the worker's own SUM aggregation.
    from sqlalchemy import func as _func

    from bvphoenix.db.models import Instance as _Instance
    from bvphoenix.db.models.dicom import Series as _Series
    from bvphoenix.db.models.principals import Subject as _Subject

    total_files: int | None = None
    total_bytes: int | None = None
    if grant.resource_kind == "study":
        agg = (
            await db.execute(
                select(
                    _func.count(_Instance.id),
                    _func.coalesce(_func.sum(_Instance.size_bytes), 0),
                )
                .join(_Series, _Series.id == _Instance.series_id)
                .where(_Series.study_id == grant.resource_id)
            )
        ).first()
        if agg is not None:
            total_files = int(agg[0]) if agg[0] is not None else None
            total_bytes = int(agg[1]) if agg[1] is not None else None
    elif grant.resource_kind == "patient":
        agg = (
            await db.execute(
                select(
                    _func.count(_Instance.id),
                    _func.coalesce(_func.sum(_Instance.size_bytes), 0),
                )
                .join(_Series, _Series.id == _Instance.series_id)
                .join(ImagingStudy, ImagingStudy.id == _Series.study_id)
                .where(ImagingStudy.patient_id == grant.resource_id)
            )
        ).first()
        if agg is not None:
            total_files = int(agg[0]) if agg[0] is not None else None
            total_bytes = int(agg[1]) if agg[1] is not None else None
    elif grant.resource_kind == "folder":
        # Folder pre-flight: walk FolderItem to find in-scope studies
        # and SUM their instances. Bounded by the cardinality of the
        # folder, not of the patient (a folder is by design a curated
        # subset). Documents are not counted in ``total_files`` —
        # the landing page tile is meant for "DICOM volume" not
        # "every artifact" — keeping it consistent with study/patient
        # variants.
        from bvphoenix.db.models import FolderItem as _FolderItem

        item_rows = (
            await db.execute(
                select(_FolderItem.resource_kind, _FolderItem.resource_id).where(
                    _FolderItem.folder_id == grant.resource_id
                )
            )
        ).all()
        in_scope_studies = [rid for kind, rid in item_rows if kind == "study"]
        if in_scope_studies:
            agg = (
                await db.execute(
                    select(
                        _func.count(_Instance.id),
                        _func.coalesce(_func.sum(_Instance.size_bytes), 0),
                    )
                    .join(_Series, _Series.id == _Instance.series_id)
                    .where(_Series.study_id.in_(in_scope_studies))
                )
            ).first()
            if agg is not None:
                total_files = int(agg[0]) if agg[0] is not None else None
                total_bytes = int(agg[1]) if agg[1] is not None else None
        else:
            total_files = 0
            total_bytes = 0

    grantor_display: str | None = None
    grantor_subj = (
        await db.execute(
            select(_Subject.display_name).where(_Subject.id == grant.grantor_subject_id)
        )
    ).scalar_one_or_none()
    if grantor_subj:
        grantor_display = str(grantor_subj)

    # Pre-export status snapshot (study scope only). Lets the
    # public landing page render "Studio pronto" or "Preparazione
    # 30%" before the recipient clicks Download.
    prepared_status: str | None = None
    prepared_progress_done: int | None = None
    prepared_progress_total: int | None = None
    if link.prepared_job_id is not None:
        from bvphoenix.db.models.jobs import Job as _JobModel

        job_row = (
            await db.execute(
                select(
                    _JobModel.status,
                    _JobModel.progress_done,
                    _JobModel.progress_total,
                ).where(_JobModel.id == link.prepared_job_id)
            )
        ).first()
        if job_row is not None:
            prepared_status = str(job_row[0])
            prepared_progress_done = job_row[1]
            prepared_progress_total = job_row[2]

    return ShareInfoOut(
        study_title=title,
        modalities=modalities,
        study_date=study_date,
        requires_password=link.password_hash is not None,
        expires_at=grant.valid_until.isoformat() if grant.valid_until else None,
        permissions=list(grant.permissions),
        max_uses=link.max_uses,
        uses_remaining=uses_remaining,
        resource_kind=grant.resource_kind,
        resource_id=str(grant.resource_id),
        mode=link.mode,
        claimable=claimable,
        bindable=bindable,
        recipient_email_known=bool(link.recipient_email),
        deidentified=bool(grant.deidentify),
        total_files=total_files,
        total_bytes=total_bytes,
        grantor_display=grantor_display,
        prepared_status=prepared_status,
        prepared_progress_done=prepared_progress_done,
        prepared_progress_total=prepared_progress_total,
    )


@router.post("/shared/{token}/verify", response_model=VerifyOut)
@limiter.limit(SHARE_VERIFY_LIMIT)
async def verify_share_link(
    request: Request,
    response: Response,
    token: str,
    body: VerifyIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> VerifyOut:
    row = (
        await db.execute(
            select(ShareLink, Grant)
            .join(Grant, Grant.id == ShareLink.grant_id)
            .where(ShareLink.token == token)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    link, grant = row
    if grant.revoked_at is not None:
        raise HTTPException(status_code=410, detail={"reason": "revoked"})
    if grant.valid_until and grant.valid_until < datetime.now(UTC):
        # 410 Gone — the resource existed but the grant expired.
        raise HTTPException(status_code=410, detail={"reason": "expired"})

    # Password must be verified BEFORE the counter bump, otherwise a
    # brute-force attacker would burn legitimate uses with failed guesses.
    if link.password_hash and (
        not body.password or not verify_password(body.password, link.password_hash)
    ):
        raise HTTPException(status_code=401, detail={"reason": "invalid_password"})

    # Atomic check-and-increment. Without this, two concurrent requests
    # can both read use_count < max_uses and both commit +1, exceeding
    # the cap. The WHERE clause is re-evaluated per updater under row
    # lock, so the (max_uses + 1)th caller sees zero rows returned.
    result = await db.execute(
        text(
            "UPDATE share_links SET use_count = use_count + 1 "
            "WHERE id = :id AND (max_uses IS NULL OR use_count < max_uses) "
            "RETURNING use_count"
        ),
        {"id": link.id},
    )
    updated = result.first()
    if updated is None:
        # Row exists but predicate failed → max_uses reached. 429 is a
        # closer semantic match than 404 because the link was valid and
        # the caller is being told "quota exhausted".
        await db.rollback()
        raise HTTPException(status_code=429, detail={"reason": "max_uses_reached"})
    await db.commit()

    await audit.log(
        action="share_access",
        actor_subject_id=None,
        resource_kind=grant.resource_kind,
        resource_id=grant.resource_id,
        metadata={
            "grant_id": str(grant.id),
            "link_id": str(link.id),
            "use_count": link.use_count,
        },
    )
    expires_in = 3600
    access_token = issue_access_token(
        subject_id=PUBLIC_SUBJECT_ID,
        email="shared-link",
        is_admin=False,
        grant_id=grant.id,
        # Pass the share_link id through so writes done with this JWT
        # can be attributed in the versioning DAG via
        # ``ActorContext.kind='link'``. Always set, even for B-mode
        # links — only the editor commit attribution differs by mode
        # (claim links still record the link id; the human is
        # whoever the link was claimed by once that flow lands).
        share_link_id=link.id,
    )

    # Cache hand-off for password-protected shares. When the share
    # has a ready-to-stream cached export, mint a 5-minute
    # single-use download token bound to ``(share_link_download,
    # link.id)`` and hand the recipient a direct
    # ``/api/shared/{token}/download?dt=<token>`` URL. The download
    # endpoint accepts the dt as a one-time bypass of the password
    # gate, then streams from the cached artifact through the
    # uniform proxy_s3_object pipeline (Range/resume/storage-isolation).
    #
    # Why a new ``share_link_download`` resource_kind instead of
    # reusing ``job_result``: the existing dt validator sets
    # ``subject_id`` to a real User row, but share recipients
    # authenticate as PUBLIC_SUBJECT_ID which doesn't exist in the
    # users table. Scoping the dt to the share link itself keeps
    # the authentication contract clean (the share token is the
    # principal; the dt is just a CSRF-style one-shot capability).
    cached_url: str | None = None
    cached_ttl: int | None = None
    if link.prepared_job_id is not None:
        from bvphoenix.db.models.jobs import Job as _JobModel

        cached_job = (
            await db.execute(select(_JobModel).where(_JobModel.id == link.prepared_job_id))
        ).scalar_one_or_none()
        if (
            cached_job is not None
            and cached_job.status == "succeeded"
            and cached_job.result_uri
            and cached_job.result_uri.startswith("s3://")
            # Defensive scope check (mirrors the same guard on
            # /shared/{token}/download): a tampered FK cannot hand
            # the recipient a dt for a different study's bytes.
            and grant.resource_id in (cached_job.scope_ids or [])
        ):
            from arq import create_pool

            from bvphoenix.services.arq_redis import redis_settings
            from bvphoenix.services.download_tokens import issue_download_token

            settings = get_settings()
            try:
                redis = await create_pool(redis_settings(settings.redis_url))
                try:
                    dt_token, dt_ttl = await issue_download_token(
                        redis,
                        subject_id=PUBLIC_SUBJECT_ID,
                        resource_kind="share_link_download",
                        resource_id=link.id,
                        ttl_seconds=SHARE_LINK_DOWNLOAD_TTL_SECONDS,
                    )
                finally:
                    await redis.close()
                # Absolute URL so the FE can hand it to a synthetic
                # anchor click without piecing together the host.
                # Same convention as ``ShareLink.url``: a path-only
                # value would break a mailto: forward or a chat-paste.
                base_public = _resolve_public_base_url()
                cached_url = f"{base_public}/api/shared/{token}/download?dt={dt_token}"
                cached_ttl = dt_ttl
            except Exception:
                # Token issuance is best-effort: a Redis hiccup
                # leaves cached_url=None and the FE falls back to
                # the standard JWT viewer flow.
                logger.warning("verify cached-download token mint failed", exc_info=True)

    # Mirror the password-login flow: hand the recipient the session as
    # an HttpOnly ``bvp_session`` cookie so the SPA destination page
    # (which authenticates via the cookie since the 2026-05-21 move off
    # localStorage bearer tokens) carries the grant-scoped JWT
    # automatically. Without this the recipient lands on /patients or
    # /studies with no credential and ``require_user`` answers 401
    # "authentication required". The JSON ``access_token`` stays for
    # non-browser callers (curl, integration tests).
    set_session_cookie(response, request, access_token, max_age=expires_in)

    return VerifyOut(
        access_token=access_token,
        expires_in=expires_in,
        cached_download_url=cached_url,
        cached_download_expires_in=cached_ttl,
    )


@router.get("/shared/{token}/download")
@limiter.limit(SHARE_DOWNLOAD_LIMIT)
async def download_via_share_link(
    request: Request,
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    dt: str | None = None,
) -> StreamingResponse:
    """Stream the cached study export ZIP through the share-link.

    This is the recipient-facing companion of the grantor's
    pre-export. It:

    1. Resolves ``token`` to ``(ShareLink, Grant)``.
    2. Bails 404 on revoked / expired / quota-exhausted.
    3. Bails 425 (Too Early) when ``prepared_job_id`` is queued /
       running so the FE can keep polling instead of erroring.
    4. Validates that the cached job's ``scope_ids`` actually
       contains ``grant.resource_id`` — defensive against a
       tampered/raced ``prepared_job_id`` FK.
    5. Streams the S3 artifact through ``proxy_s3_object`` (Range /
       206 supported, storage isolation preserved).
    6. On a 200 full-body completion, bumps ``download_count`` —
       206 Partial Content does *not* count, since a chunked
       resume would otherwise inflate the figure.

    Permission gating: the share token is the credential.

    * Anonymous-mode shares + no password → direct download. The
      threat surface is the same as a public presigned URL, with
      our audit + rate-limit on top.
    * Password-protected shares → require ``?dt=<token>`` minted
      by ``POST /shared/{token}/verify`` (the recipient already
      proved password possession to obtain it). Without dt this
      path 401s and routes the FE to /verify.

    The dt is single-use, atomically consumed via Redis ``GETDEL``,
    and scoped to ``(share_link_download, link.id)`` so a leaked dt
    can be replayed exactly zero times against this share and is
    useless against any other.
    """
    row = (
        await db.execute(
            select(ShareLink, Grant)
            .join(Grant, Grant.id == ShareLink.grant_id)
            .where(ShareLink.token == token)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    link, grant = row
    if grant.revoked_at is not None:
        raise HTTPException(status_code=404, detail="not found")
    if grant.valid_until and grant.valid_until < datetime.now(UTC):
        raise HTTPException(status_code=410, detail="link expired")
    if link.max_uses is not None and link.use_count >= link.max_uses:
        raise HTTPException(status_code=429, detail="link usage limit reached")
    # Password-protected links: the recipient went through /verify,
    # which mints a dt token bound to ``(share_link_download,
    # link.id)``. We PEEK (no GETDEL) so the same dt allows N
    # downloads within its TTL (``SHARE_LINK_DOWNLOAD_TTL_SECONDS``,
    # currently 24h). A consultant who clicks Scarica multiple
    # times — same morning, that evening at home, the next day
    # before the TTL expires — doesn't have to re-enter the
    # password each time. The token is still bounded: scope-locked
    # to this exact share link, useless against any other resource.
    if link.password_hash is not None:
        if not dt:
            raise HTTPException(
                status_code=401,
                detail="password required; use /shared/{token}/verify first",
            )
        from arq import create_pool

        from bvphoenix.services.arq_redis import redis_settings
        from bvphoenix.services.download_tokens import peek_download_token

        settings = get_settings()
        redis = await create_pool(redis_settings(settings.redis_url))
        try:
            peeked_subject = await peek_download_token(
                redis,
                dt,
                resource_kind="share_link_download",
                resource_id=link.id,
            )
        finally:
            await redis.close()
        if peeked_subject is None:
            raise HTTPException(
                status_code=401,
                detail="invalid or expired download token; re-verify to refresh",
            )
    if grant.resource_kind not in ("study", "folder"):
        # Patient/bulk shares don't pre-prep yet; gate so a confused
        # FE doesn't 500 on us. ``study`` and ``folder`` both go
        # through the prep-job pipeline below; other kinds keep
        # using the legacy at-click paths until they opt in.
        raise HTTPException(
            status_code=409,
            detail="this share kind does not support direct download yet",
        )

    from bvphoenix.db.models.jobs import Job as _JobModel

    if link.prepared_job_id is None:
        raise HTTPException(
            status_code=425,
            detail="archive is being prepared; retry shortly",
            headers={"Retry-After": "10"},
        )
    job = (
        await db.execute(select(_JobModel).where(_JobModel.id == link.prepared_job_id))
    ).scalar_one_or_none()
    if job is None or not job.result_uri or not job.result_uri.startswith("s3://"):
        # Cache rotated out (cleanup cron) before the recipient
        # got here. The grantor will see the row needs re-prep in
        # /settings/shares.
        raise HTTPException(status_code=404, detail="archive no longer available")
    # Defensive scope check: the dedup primitive guarantees identical
    # ``(kind, owner, scope_ids, canonical_input)`` collide on one job
    # row, but a tampered prepared_job_id could in principle point to
    # a job for a different study. Verifying ``grant.resource_id in
    # job.scope_ids`` makes this auth class IDOR-proof: the bytes the
    # recipient actually receives are bound to the resource the share
    # explicitly grants, not whatever job_id the FK currently holds.
    job_scope_ids = list(job.scope_ids or [])
    if grant.resource_id not in job_scope_ids:
        logger.warning(
            "share-link prepared_job_id scope mismatch link=%s job=%s grant_resource=%s job_scope=%s",
            link.id,
            job.id,
            grant.resource_id,
            job_scope_ids,
        )
        raise HTTPException(status_code=404, detail="archive scope mismatch")
    if job.status != "succeeded":
        # Not yet ready. 425 Too Early is the closest semantic to
        # "the resource exists but isn't usable yet"; HEAD it again
        # in 10 seconds.
        raise HTTPException(
            status_code=425,
            detail={
                "error": "archive_not_ready",
                "status": job.status,
                "progress_done": job.progress_done,
                "progress_total": job.progress_total,
            },
            headers={"Retry-After": "10"},
        )

    rest = job.result_uri[len("s3://") :]
    if "/" not in rest:
        raise HTTPException(status_code=404, detail="not found")
    bucket, key = rest.split("/", 1)
    filename = key.rsplit("/", 1)[-1] or "download"

    link_id = link.id  # capture for the BackgroundTask closure

    async def _bump_download_count() -> None:
        """Atomic UPDATE to bump the link's download_count after a
        successful 200 full-body download. The proxy_s3_object helper
        suppresses this task on 206 Partial Content so a chunked
        resume cannot inflate the count."""
        from bvphoenix.db.session import get_db as _gdb

        async for ps in _gdb():
            await ps.execute(
                text("UPDATE share_links SET download_count = download_count + 1 WHERE id = :id"),
                {"id": link_id},
            )
            await ps.commit()
            break

    from starlette.background import BackgroundTask

    await audit.log(
        action="share_download",
        actor_subject_id=None,
        resource_kind=grant.resource_kind,
        resource_id=grant.resource_id,
        metadata={
            "link_id": str(link.id),
            "grant_id": str(grant.id),
        },
    )

    return await proxy_s3_object(
        request=request,
        bucket=bucket,
        key=key,
        filename=filename,
        background=BackgroundTask(_bump_download_count),
    )


class ConfirmReceiptOut(BaseModel):
    received_at: str


@router.post(
    "/shared/{token}/confirm-receipt",
    response_model=ConfirmReceiptOut,
    status_code=200,
)
async def confirm_share_receipt(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
) -> ConfirmReceiptOut:
    """Recipient acknowledges they received the shared resource.

    Public endpoint (no JWT required: the recipient's only credential
    is the share token itself). Idempotent — the timestamp is set on
    the first successful call and stays constant thereafter, which
    keeps the audit signal "the consultant did open this" stable
    even if they click the button twice.

    Closes the audit chain for the grantor:

        share_create → share_email_sent → share_access → share_receipt_confirmed

    The grantor sees ``received_at`` on the share-link row in their
    own UI, so "did the primary actually get the study?" stops being
    an out-of-band question.
    """
    row = (
        await db.execute(
            select(ShareLink, Grant)
            .join(Grant, Grant.id == ShareLink.grant_id)
            .where(ShareLink.token == token)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    link, grant = row
    if grant.revoked_at is not None:
        raise HTTPException(status_code=404, detail="not found")
    if grant.valid_until and grant.valid_until < datetime.now(UTC):
        raise HTTPException(status_code=410, detail="link expired")

    if link.received_at is None:
        link.received_at = datetime.now(UTC)
        await db.commit()
        await audit.log(
            action="share_receipt_confirmed",
            actor_subject_id=None,
            resource_kind=grant.resource_kind,
            resource_id=grant.resource_id,
            metadata={
                "grant_id": str(grant.id),
                "link_id": str(link.id),
                "recipient_email": link.recipient_email,
            },
        )
    return ConfirmReceiptOut(received_at=link.received_at.isoformat())


@router.post("/studies/{study_id}/publish", status_code=status.HTTP_200_OK)
async def publish_study(
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> dict[str, bool]:
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    if not (user.is_admin or study.owner_subject_id == user.subject_id):
        raise HTTPException(status_code=403, detail="only the owner can publish")
    study.is_public = True
    await db.commit()
    return {"is_public": True}


@router.post("/studies/{study_id}/unpublish", status_code=status.HTTP_200_OK)
async def unpublish_study(
    study_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> dict[str, bool]:
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status_code=404, detail="study not found")
    if not (user.is_admin or study.owner_subject_id == user.subject_id):
        raise HTTPException(status_code=403, detail="only the owner can unpublish")
    study.is_public = False
    await db.commit()
    return {"is_public": False}


class SharedReportOut(BaseModel):
    id: str
    study_id: str
    version: int
    title: str
    file_content_type: str | None
    created_at: str


class SharedDocumentOut(BaseModel):
    id: str
    document_type: str
    title: str
    file_content_type: str | None
    document_date: str | None
    created_at: str


class SharedArtifactsOut(BaseModel):
    """Downloadable artifacts the shared link can read.

    Empty lists when the grant is study-scoped (no patient docs) or when
    a resource has no files attached. The ``can_download`` flag mirrors
    the server-side permission check so the frontend can style buttons
    disabled / hidden without probing the download endpoints.
    """

    can_download: bool
    reports: list[SharedReportOut]
    documents: list[SharedDocumentOut]


@router.get("/shared/{token}/artifacts", response_model=SharedArtifactsOut)
async def list_shared_artifacts(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SharedArtifactsOut:
    """Enumerate reports (and, for patient grants, documents) exposed by
    a valid share link. Does not bump ``use_count`` — this is a passive
    read to populate the shared landing page's download section.
    """
    row = (
        await db.execute(
            select(ShareLink, Grant)
            .join(Grant, Grant.id == ShareLink.grant_id)
            .where(ShareLink.token == token)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    link, grant = row
    if grant.revoked_at is not None:
        raise HTTPException(status_code=404, detail="not found")
    if grant.valid_until and grant.valid_until < datetime.now(UTC):
        raise HTTPException(status_code=404, detail="not found")
    if link.max_uses and link.use_count >= link.max_uses:
        raise HTTPException(status_code=404, detail="not found")

    can_download = SHARED_DOWNLOAD in grant.permissions

    # Resolve the list of study ids the grant covers.
    if grant.resource_kind == "study":
        pass
    elif grant.resource_kind == "patient":
        [
            r[0]
            for r in (
                await db.execute(
                    select(ImagingStudy.id).where(ImagingStudy.patient_id == grant.resource_id)
                )
            ).all()
        ]
    else:
        pass

    # v3 phase 3b: legacy Study-attached Report sharing is dropped.
    # Sharing of report_contents (the Expression layer) goes through a
    # dedicated grant kind in phase 4 once the share-link UI is
    # rebuilt around the new model. Returning an empty list keeps the
    # share-link rendering working for the document + study slices.
    reports_out: list[SharedReportOut] = []

    documents_out: list[SharedDocumentOut] = []
    if grant.resource_kind == "patient":
        doc_rows = (
            (
                await db.execute(
                    select(Document)
                    .where(
                        Document.patient_id == grant.resource_id,
                        Document.file_s3_key.is_not(None),
                    )
                    .order_by(Document.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        documents_out = [
            SharedDocumentOut(
                id=str(d.id),
                document_type=d.kind_id,
                title=d.title,
                file_content_type=d.file_content_type,
                document_date=str(d.document_date) if d.document_date else None,
                created_at=d.created_at.isoformat(),
            )
            for d in doc_rows
        ]

    return SharedArtifactsOut(
        can_download=can_download, reports=reports_out, documents=documents_out
    )


# ---- Shared-link downloads (reports + patient documents) ----


async def _load_valid_link(db: AsyncSession, token: str) -> tuple[ShareLink, Grant]:
    """Resolve a share token to its (link, grant) pair.

    Locks the ``share_links`` row FOR UPDATE so callers can atomically
    bump ``use_count`` without racing concurrent downloads. Raises 404
    in all "go away" cases (unknown / revoked / expired / capped) to
    avoid distinguishing them to attackers — same posture as
    ``/api/shared/{token}/info``.
    """
    row = (
        await db.execute(
            select(ShareLink, Grant)
            .join(Grant, Grant.id == ShareLink.grant_id)
            .where(ShareLink.token == token)
            .with_for_update(of=ShareLink)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    link, grant = row
    if grant.revoked_at is not None:
        raise HTTPException(status_code=404, detail="not found")
    if grant.valid_until and grant.valid_until < datetime.now(UTC):
        raise HTTPException(status_code=404, detail="not found")
    if link.max_uses and link.use_count >= link.max_uses:
        raise HTTPException(status_code=404, detail="not found")
    return link, grant


def _require_session_token(link: ShareLink, authorization: str | None) -> None:
    """If the link is password-protected, require a verified session JWT.

    Passwordless links are downloadable without any extra session — anyone
    who holds the URL already has what the link gates. For protected
    links we insist on a valid JWT in the ``Authorization`` header; it
    is either the short-lived token returned by ``POST .../verify``
    (whose ``sub`` is the public subject) or a normal user JWT from a
    separately authenticated session. Either one proves the caller has
    passed a server-side credential check, so the password gate is
    honored across the session.
    """
    if link.password_hash is None:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="session required")
    token = authorization.split(" ", 1)[1].strip()
    if decode_token(token) is None:
        raise HTTPException(status_code=401, detail="invalid session")


def _safe_filename(name: str, fallback: str) -> str:
    """Sanitize a user-controlled string for ``Content-Disposition``.

    Strips path separators and characters that confuse RFC 5987 parsers,
    collapses whitespace, and falls back to ``fallback`` when the result
    is empty. Keeps the file extension if one is provided.
    """
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip("._"))
    cleaned = cleaned.strip("_")
    return cleaned or fallback


@router.get("/shared/{token}/reports/{report_id}/download")
async def download_shared_report(
    token: str,
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """v3 phase 3b: legacy Study-attached Report download is retired.

    The v3 successor exposes report_contents (Expression layer) for
    download-via-share once phase 4 ships the new share-link UI; until
    then this endpoint returns 410 Gone so existing share links degrade
    visibly rather than silently."""
    raise HTTPException(
        status_code=410,
        detail="legacy report sharing retired in v3; use the report_contents share-link surface (phase 4)",
    )


@router.get("/shared/{token}/documents/{doc_id}/download")
@limiter.limit(SHARE_DOWNLOAD_LIMIT)
async def download_shared_document(
    request: Request,
    token: str,
    doc_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    audit: AuditDep,
    authorization: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Stream a patient-share-link's document attachment.

    Hardened in the round that introduced the cached study export:
    * atomic ``use_count`` UPDATE (P0 — was read-modify-write, racy
      under concurrent downloads),
    * audit row ``shared_document_download`` (P1 — was silent),
    * Range / 206 Partial Content support via :func:`proxy_s3_object`
      (P1 — recipients on shaky tethers can now resume),
    * rate limit shared with the study download path so a leaked link
      cannot pin our outbound S3 bandwidth.
    """
    link, grant = await _load_valid_link(db, token)
    _require_session_token(link, authorization)
    if SHARED_DOWNLOAD not in grant.permissions:
        raise HTTPException(status_code=403, detail="download not permitted")

    # Patient documents only ride on patient-level grants; a study-scoped
    # share link never exposes them.
    if grant.resource_kind != "patient":
        raise HTTPException(status_code=404, detail="not found")

    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == grant.resource_id,
            )
        )
    ).scalar_one_or_none()
    if doc is None or doc.file_s3_key is None:
        raise HTTPException(status_code=404, detail="document file not found")

    # Atomic INCR. The previous read-modify-write lost ticks under
    # concurrent downloads from two recipients hammering the same
    # link — the new query is a single round-trip and matches the
    # pattern used by ``verify_share_link`` and the study download.
    await db.execute(
        text("UPDATE share_links SET use_count = use_count + 1 WHERE id = :id"),
        {"id": link.id},
    )
    await db.commit()

    await audit.log(
        action="shared_document_download",
        actor_subject_id=None,
        resource_kind=grant.resource_kind,
        resource_id=grant.resource_id,
        metadata={
            "link_id": str(link.id),
            "grant_id": str(grant.id),
            "document_id": str(doc.id),
        },
    )

    ext_map = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "text/plain": ".txt",
        "text/markdown": ".md",
    }
    ext = ext_map.get((doc.file_content_type or "").lower(), "")
    base = _safe_filename(doc.title, f"document-{doc.id}")
    settings = get_settings()
    return await proxy_s3_object(
        request=request,
        bucket=settings.s3_bucket_raw,
        key=doc.file_s3_key,
        filename=f"{base}{ext}",
        fallback_content_type=doc.file_content_type or "application/octet-stream",
    )
