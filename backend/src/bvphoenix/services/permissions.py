"""Permission resolution mirroring authorization.md §5.

**Trust boundary (2026-04-17)**: as of migration ``0009_rls_policies``
the authoritative access check lives in PostgreSQL Row-Level Security.
The functions below (``can``, ``visible_studies_filter``, …) are kept
as *belt-and-braces* — they run the same predicates at the application
layer so:

* routes can return meaningful ``403`` responses (RLS just returns
  empty result sets or raises opaque errors on ``WITH CHECK`` failures),
* the app-level query still filters the working set efficiently using
  indices it controls, even when the DB planner can't push the RLS
  predicate down cleanly,
* a regression that disables RLS — accidentally or in test tooling —
  does not immediately become a data leak.

If you change a predicate here, mirror it in ``0009_rls_policies`` (or
a follow-up migration). If you change RLS, mirror it here. Divergence
is a bug: fail closed in both places.

See ``docs/security-rls.md`` for the bypass strategy and how to add
RLS to new tables.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from bvphoenix.auth.deps import enforce_agent_patient_scope
from bvphoenix.config import get_settings
from bvphoenix.db.models import Folder, Grant, ImagingStudy, Membership, Patient, Subject, User


def platform_owner_subject_id() -> uuid.UUID:
    """Return the UUID of the OpenData PLATFORM_OWNER subject.

    Configurable via ``BVP_PLATFORM_OWNER_SUBJECT_ID``; defaults to a
    well-known sentinel UUID that migration ``0036_platform_owner_subject``
    seeds. Resources owned by this subject are the OpenData public
    dataset: read-only for all authenticated users, write-only for the
    platform identity itself.
    """
    return uuid.UUID(get_settings().platform_owner_subject_id)


def get_or_create_platform_owner_subject(session: Session) -> Subject:
    """Return the Subject row for the platform-owner sentinel.

    The row is seeded by the bootstrap migration ``0036``; if it is
    missing (fresh dev DB) it is created. Real prod always has it. Shared
    by both the radiology (``services.public_dataset``) and pathology
    (``services.public_pathology``) public-dataset importers so the
    get-or-create logic lives in exactly one place. Sync-only — the
    importers build on the sync ingest path.
    """
    owner_id = platform_owner_subject_id()
    row = session.execute(select(Subject).where(Subject.id == owner_id)).scalar_one_or_none()
    if row is not None:
        return row
    row = Subject(id=owner_id, kind="system")
    session.add(row)
    session.flush()
    return row


def is_platform_owner(user: User | None) -> bool:
    """True iff ``user`` is the synthetic platform-owner subject.

    The platform-owner subject never logs in via the normal auth path
    (it has no email / password / OIDC binding). This check exists so
    that internal services that act on behalf of OpenData can produce a
    User-shaped object with ``subject_id == platform_owner_subject_id``
    and have ``can()`` recognise their authority.
    """
    if user is None:
        return False
    return user.subject_id == platform_owner_subject_id()


def _share_scope(user: User | None) -> Grant | None:
    """Return the share-link grant pinned on ``user``, if any.

    Synthetic users minted by ``optional_user`` for share-link sessions
    carry the originating grant on a transient ``_share_grant`` attribute
    so visibility / permission checks can narrow strictly to that grant.
    Without this narrowing every share-link session — all of which share
    the synthetic ``PUBLIC_SUBJECT_ID`` — would see resources granted by
    any active link, a cross-patient leak.
    """
    if user is None:
        return None
    return getattr(user, "_share_grant", None)


# Permission verbs (authorization.md §1).
READ_METADATA = "read:metadata"
READ_PIXELS = "read:pixels"
READ_ANNOTATIONS = "read:annotations"
WRITE_ANNOTATIONS = "write:annotations"
WRITE_REPORT = "write:report"
RUN_LLM = "run:llm"
DOWNLOAD_DICOM = "download:dicom"
DOWNLOAD_DERIVATIVE = "download:derivative"
# Shared-link download of non-DICOM artifacts (reports, patient documents).
# Granted when a share link is created with ``download=True``; checked by
# the shared-link download endpoints in ``api/sharing.py``.
SHARED_DOWNLOAD = "download:derivative"
SHARE = "share"
SHARE_DELEGATE = "share:delegate"
# Patient inbound inbox (api/inbox.py): manage the capability addresses
# and accept/reject staged items into the fascicolo. Owner-level by
# construction (member of ALL_PERMS, never of PUBLIC_READ_PERMS); the
# inbox endpoints additionally refuse share-link sessions outright —
# a delegated viewer must not triage what enters the record.
REVIEW_INBOX = "review:inbox"
PUBLISH = "publish"
LIST_FOR_SALE = "list:for_sale"
TRANSFER_OWNERSHIP = "transfer:ownership"
DELETE = "delete"
COMMERCIAL_USE = "commercial:use"

PUBLIC_READ_PERMS: frozenset[str] = frozenset({READ_METADATA, READ_PIXELS, READ_ANNOTATIONS})
ALL_PERMS: frozenset[str] = frozenset(
    {
        READ_METADATA,
        READ_PIXELS,
        READ_ANNOTATIONS,
        WRITE_ANNOTATIONS,
        WRITE_REPORT,
        RUN_LLM,
        DOWNLOAD_DICOM,
        DOWNLOAD_DERIVATIVE,
        SHARE,
        SHARE_DELEGATE,
        REVIEW_INBOX,
        PUBLISH,
        LIST_FOR_SALE,
        TRANSFER_OWNERSHIP,
        DELETE,
    }
)


async def principal_set(db: AsyncSession, user: User | None) -> set[uuid.UUID]:
    """Subject ids that grants matched against ``user`` can target —
    self plus every org / group they belong to. Empty for anonymous."""
    if user is None:
        return set()
    rows = await db.execute(
        select(Membership.parent_subject_id).where(Membership.subject_id == user.subject_id)
    )
    return {user.subject_id, *(r[0] for r in rows.all())}


async def effective_permissions_on_study(
    db: AsyncSession, *, user: User | None, study: ImagingStudy
) -> frozenset[str]:
    perms: set[str] = set()
    if study.is_public:
        perms |= PUBLIC_READ_PERMS
    # OpenData ownership: PLATFORM_OWNER-owned studies are read-only for
    # every authenticated reader. Same effect as is_public for read; write
    # actions are blocked unless the caller IS the platform owner (below).
    is_opendata = study.owner_subject_id == platform_owner_subject_id()
    if user is None:
        return frozenset(perms)
    if is_opendata and not is_platform_owner(user):
        # Read-only access to OpenData for all authenticated users.
        perms |= PUBLIC_READ_PERMS
    if user.is_admin or study.owner_subject_id == user.subject_id:
        # is_admin keeps full power (incl. on OpenData) so a designated
        # human curator can run the platform; PLATFORM_OWNER acting as
        # itself also enters this branch.
        return ALL_PERMS
    share = _share_scope(user)
    if share is not None:
        # Share session: only honor the grant that minted the JWT, and
        # only when the study is in scope (direct study grant, or a
        # patient grant whose patient owns this study).
        if (share.resource_kind == "study" and share.resource_id == study.id) or (
            share.resource_kind == "patient"
            and study.patient_id is not None
            and study.patient_id == share.resource_id
        ):
            perms.update(share.permissions)
        return frozenset(perms)
    principals = await principal_set(db, user)
    if not principals:
        return frozenset(perms)
    now = datetime.now(UTC)
    # Direct study grants
    rows = await db.execute(
        select(Grant.permissions).where(
            Grant.resource_kind == "study",
            Grant.resource_id == study.id,
            Grant.grantee_subject_id.in_(principals),
            Grant.revoked_at.is_(None),
            Grant.valid_from <= now,
            or_(Grant.valid_until.is_(None), Grant.valid_until >= now),
        )
    )
    for (granted,) in rows.all():
        perms.update(granted)
    # Patient-level grants cascade to studies
    if study.patient_id is not None:
        patient_rows = await db.execute(
            select(Grant.permissions).where(
                Grant.resource_kind == "patient",
                Grant.resource_id == study.patient_id,
                Grant.grantee_subject_id.in_(principals),
                Grant.revoked_at.is_(None),
                Grant.valid_from <= now,
                or_(Grant.valid_until.is_(None), Grant.valid_until >= now),
            )
        )
        for (granted,) in patient_rows.all():
            perms.update(granted)
    return frozenset(perms)


async def can(db: AsyncSession, *, user: User | None, action: str, study: ImagingStudy) -> bool:
    return action in await effective_permissions_on_study(db, user=user, study=study)


async def visible_studies_filter(db: AsyncSession, user: User | None) -> Select:
    """Build a SELECT on ImagingStudy filtered to rows ``user`` can read.

    Listing endpoints must base their query on the return value of this
    function; never query ImagingStudy directly.
    """
    base = select(ImagingStudy)
    if user is None:
        return base.where(ImagingStudy.is_public.is_(True))
    if user.is_admin:
        return base
    # Share-link session: strictly scope to the resource of the grant
    # that minted the JWT — not every active grant pointing at PUBLIC.
    share = _share_scope(user)
    if share is not None:
        if share.resource_kind == "patient":
            return base.where(ImagingStudy.patient_id == share.resource_id)
        if share.resource_kind == "study":
            return base.where(ImagingStudy.id == share.resource_id)
        # Unknown share kind for studies — fail closed.
        return base.where(ImagingStudy.id.is_(None))
    principals = await principal_set(db, user)
    now = datetime.now(UTC)
    grant_subq = select(Grant.resource_id).where(
        Grant.resource_kind == "study",
        Grant.revoked_at.is_(None),
        Grant.valid_from <= now,
        or_(Grant.valid_until.is_(None), Grant.valid_until >= now),
        Grant.grantee_subject_id.in_(principals),
    )
    # Studies visible via patient-level grants
    patient_grant_subq = select(Grant.resource_id).where(
        Grant.resource_kind == "patient",
        Grant.revoked_at.is_(None),
        Grant.valid_from <= now,
        or_(Grant.valid_until.is_(None), Grant.valid_until >= now),
        Grant.grantee_subject_id.in_(principals),
    )
    return base.where(
        or_(
            ImagingStudy.is_public.is_(True),
            # OpenData: every authenticated reader sees PLATFORM_OWNER-owned
            # studies regardless of grant. Read-only access enforced in
            # effective_permissions_on_study + can_*.
            ImagingStudy.owner_subject_id == platform_owner_subject_id(),
            ImagingStudy.owner_subject_id == user.subject_id,
            ImagingStudy.id.in_(grant_subq),
            ImagingStudy.patient_id.in_(patient_grant_subq),
        )
    )


def apply_scope_filter(query: Select, scope: str | None, user: User | None) -> Select:
    """AND-restrict ``query`` to the user-selected scope.

    Sits *on top of* ``visible_studies_filter`` (which is the auth
    boundary). This helper is purely a UX filter — it can only
    *narrow* the auth-allowed set, never widen it. Unknown / None /
    'all' is a no-op.

    Scopes:
    * ``all`` (default): no additional filter; same set as
      ``visible_studies_filter``.
    * ``public``: studies marked is_public=True OR owned by the
      platform-owner (OpenData library). Useful when the user wants
      to explore the curated public dataset without their own private
      uploads.
    * ``mine``: studies owned by the calling user. Anonymous / share
      -link sessions match nothing under this scope.
    """
    if scope in (None, "all"):
        return query
    if scope == "public":
        return query.where(
            or_(
                ImagingStudy.is_public.is_(True),
                ImagingStudy.owner_subject_id == platform_owner_subject_id(),
            )
        )
    if scope == "mine":
        if user is None or user.subject_id is None:
            # Anonymous or system caller has no "own" studies — return
            # a clause that matches nothing rather than erroring.
            return query.where(ImagingStudy.id.is_(None))
        return query.where(ImagingStudy.owner_subject_id == user.subject_id)
    # Unknown scope value: degrade to no-op rather than 400 so a stale
    # FE deploy does not break the page. The OpenAPI Literal type is
    # the enforcement boundary for new callers.
    return query


# ---- Patient-level permissions ----


async def effective_permissions_on_patient(
    db: AsyncSession, *, user: User | None, patient: Patient
) -> frozenset[str]:
    if user is None:
        return frozenset()
    if user.is_admin:
        return ALL_PERMS
    # OpenData patient: read-only for any authenticated user; full
    # power only for the PLATFORM_OWNER identity.
    if patient.managed_by_subject_id == platform_owner_subject_id():
        if is_platform_owner(user):
            return ALL_PERMS
        return PUBLIC_READ_PERMS
    # Share-link session: only the grant that minted the JWT applies,
    # and only when its resource matches the patient being checked.
    share = _share_scope(user)
    if share is not None:
        if share.resource_kind == "patient" and share.resource_id == patient.id:
            return frozenset(share.permissions)
        return frozenset()
    if patient.managed_by_subject_id == user.subject_id:
        return ALL_PERMS
    if patient.self_user_subject_id == user.subject_id:
        return ALL_PERMS
    principals = await principal_set(db, user)
    if not principals:
        return frozenset()
    now = datetime.now(UTC)
    rows = await db.execute(
        select(Grant.permissions).where(
            Grant.resource_kind == "patient",
            Grant.resource_id == patient.id,
            Grant.grantee_subject_id.in_(principals),
            Grant.revoked_at.is_(None),
            Grant.valid_from <= now,
            or_(Grant.valid_until.is_(None), Grant.valid_until >= now),
        )
    )
    perms: set[str] = set()
    for (granted,) in rows.all():
        perms.update(granted)
    return frozenset(perms)


async def can_patient(
    db: AsyncSession, *, user: User | None, action: str, patient: Patient
) -> bool:
    return action in await effective_permissions_on_patient(db, user=user, patient=patient)


async def get_patient_or_404(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    user: User | None,
    request: Request,
    action: str = READ_METADATA,
) -> Patient:
    """Load a patient row + run the layered access checks.

    Returns the row when the caller is authorised to ``action`` on
    that patient. Raises ``HTTPException(404)`` for any of:

    * patient does not exist;
    * caller is an agent token whose scope does not include this
      patient (``enforce_agent_patient_scope`` raises 403, which we
      convert via the layered ordering — but we deliberately keep
      the agent-403 path so a leaked token cannot enumerate patient
      ids via 404 timing);
    * human caller without ``action`` on the patient.

    Provides the same shape as the private ``_get_patient_or_404`` in
    ``api.patients`` so any new endpoint that wants the same gate has
    a single shared entry point.
    """
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    enforce_agent_patient_scope(request, patient.id)
    if not await can_patient(db, user=user, action=action, patient=patient):
        raise HTTPException(status_code=404, detail="patient not found")
    return patient


async def visible_patients_filter(db: AsyncSession, user: User | None) -> Select:
    """Build a SELECT on Patient filtered to rows ``user`` can see."""
    base = select(Patient)
    if user is None:
        # Patients are never anonymous-accessible
        return base.where(Patient.id.is_(None))
    if user.is_admin:
        return base
    share = _share_scope(user)
    if share is not None:
        if share.resource_kind == "patient":
            return base.where(Patient.id == share.resource_id)
        # ImagingStudy-level share doesn't surface a patient on the list page.
        return base.where(Patient.id.is_(None))
    principals = await principal_set(db, user)
    now = datetime.now(UTC)
    grant_subq = select(Grant.resource_id).where(
        Grant.resource_kind == "patient",
        Grant.revoked_at.is_(None),
        Grant.valid_from <= now,
        or_(Grant.valid_until.is_(None), Grant.valid_until >= now),
        Grant.grantee_subject_id.in_(principals),
    )
    return base.where(
        or_(
            Patient.managed_by_subject_id == user.subject_id,
            Patient.self_user_subject_id == user.subject_id,
            Patient.id.in_(grant_subq),
            # OpenData patients are visible to every authenticated user.
            Patient.managed_by_subject_id == platform_owner_subject_id(),
        )
    )


# ---- Folder-level permissions ----


async def can_access_folder(db: AsyncSession, *, user: User | None, folder_id: uuid.UUID) -> bool:
    """True iff ``user`` can read ``folder_id``.

    Cascade rules (matches the grants materialised on
    ``POST /folders/{id}/share``):

    * admin → yes;
    * owner of the folder → yes;
    * direct grant on the folder (``resource_kind='folder'``) → yes;
    * folder is patient-scoped and user has a grant on the patient
      (or manages / is the patient) → yes.
    """
    if user is None:
        return False
    folder = (await db.execute(select(Folder).where(Folder.id == folder_id))).scalar_one_or_none()
    if folder is None:
        return False
    if user.is_admin or folder.owner_subject_id == user.subject_id:
        return True
    principals = await principal_set(db, user)
    if not principals:
        return False
    now = datetime.now(UTC)
    # Direct grant on the folder
    direct = await db.execute(
        select(Grant.id).where(
            Grant.resource_kind == "folder",
            Grant.resource_id == folder.id,
            Grant.grantee_subject_id.in_(principals),
            Grant.revoked_at.is_(None),
            Grant.valid_from <= now,
            or_(Grant.valid_until.is_(None), Grant.valid_until >= now),
        )
    )
    if direct.first() is not None:
        return True
    # Patient-scoped folder: patient-level grant cascades in.
    if folder.patient_id is not None:
        patient = (
            await db.execute(select(Patient).where(Patient.id == folder.patient_id))
        ).scalar_one_or_none()
        if patient is None:
            return False
        if patient.managed_by_subject_id == user.subject_id:
            return True
        if patient.self_user_subject_id == user.subject_id:
            return True
        patient_grant = await db.execute(
            select(Grant.id).where(
                Grant.resource_kind == "patient",
                Grant.resource_id == folder.patient_id,
                Grant.grantee_subject_id.in_(principals),
                Grant.revoked_at.is_(None),
                Grant.valid_from <= now,
                or_(Grant.valid_until.is_(None), Grant.valid_until >= now),
            )
        )
        if patient_grant.first() is not None:
            return True
    return False
