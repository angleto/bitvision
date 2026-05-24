"""Extra adversarial tests: sharing auth path, branch isolation,
owner-as-proposer policy, three-way merge concurrency on the same ref.

Focus: invarianti che proteggono dati condivisi e impediscono
contaminazione fra branch consultation. Complementari a
``test_versioning_security`` (sicurezza dati) e
``test_versioning_authz_concurrency`` (authz endpoint + race base).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from bvphoenix.auth.deps import _resolve_credential
from bvphoenix.auth.tokens import issue_access_token
from bvphoenix.config import get_settings
from bvphoenix.db.models import Grant, Patient, User
from bvphoenix.db.models.principals import Subject
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID
from bvphoenix.db.session import (
    SERVICE_SUBJECT,
    set_current_subject,
)
from bvphoenix.services.versioning import (
    ActorContext,
    EntityChange,
    commit_change,
    open_consultation_branch,
    read_at_commit,
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with F12 migrations applied",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def world() -> AsyncIterator[tuple[AsyncSession, User, Patient]]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    sid = uuid.uuid4()
    pid = uuid.uuid4()
    try:
        await set_current_subject(db, SERVICE_SUBJECT)
        db.add(Subject(id=sid, kind="user", display_name=f"world-{sid}"))
        await db.flush()
        owner = User(
            subject_id=sid,
            email=f"world-{sid}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(owner)
        await db.flush()
        patient = Patient(
            id=pid,
            managed_by_subject_id=sid,
            display_name="Extra World Patient",
        )
        db.add(patient)
        await db.commit()
        yield db, owner, patient
    finally:
        try:
            await db.rollback()
            await set_current_subject(db, SERVICE_SUBJECT)
            # Clean dependent rows before subjects to avoid FK violations
            # (grants for share-link tests, anything referencing the
            # patient).
            await db.execute(
                text("DELETE FROM grants WHERE grantor_subject_id = :s OR grantee_subject_id = :s"),
                {"s": sid},
            )
            await db.execute(text("DELETE FROM patients WHERE id = :p"), {"p": pid})
            await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": sid})
            await db.commit()
        finally:
            await db.close()
            await engine.dispose()


# ---------------------------------------------------------------------------
# 1. Share-link auth chain: expired / revoked grants must NOT authenticate
# ---------------------------------------------------------------------------


class TestShareLinkAuthGrantValidation:
    """``_resolve_credential`` minted a share-link JWT carries a
    ``grant_id`` claim. If the underlying Grant is later revoked or
    expires, the JWT must stop authenticating — even if the JWT's own
    ``exp`` claim hasn't fired yet. Otherwise stale tokens replay."""

    @pytest.mark.asyncio
    async def test_revoked_grant_drops_auth(self, world) -> None:
        db, owner, patient = world
        grant = Grant(
            id=uuid.uuid4(),
            grantor_subject_id=owner.subject_id,
            grantee_subject_id=PUBLIC_SUBJECT_ID,
            resource_kind="patient",
            resource_id=patient.id,
            permissions=["read:metadata"],
            valid_from=datetime.now(UTC) - timedelta(hours=1),
            revoked_at=datetime.now(UTC),  # already revoked
        )
        db.add(grant)
        await db.commit()
        raw = issue_access_token(
            subject_id=PUBLIC_SUBJECT_ID,
            email="shared-link",
            is_admin=False,
            grant_id=grant.id,
        )

        class _StubState:
            pass

        class _StubRequest:
            def __init__(self):
                self.state = _StubState()

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)
        result = await _resolve_credential(_StubRequest(), creds, db)
        assert result is None, "revoked share-link grant must NOT authenticate"

    @pytest.mark.asyncio
    async def test_expired_grant_drops_auth(self, world) -> None:
        db, owner, patient = world
        grant = Grant(
            id=uuid.uuid4(),
            grantor_subject_id=owner.subject_id,
            grantee_subject_id=PUBLIC_SUBJECT_ID,
            resource_kind="patient",
            resource_id=patient.id,
            permissions=["read:metadata"],
            valid_from=datetime.now(UTC) - timedelta(days=2),
            valid_until=datetime.now(UTC) - timedelta(hours=1),  # past
        )
        db.add(grant)
        await db.commit()
        raw = issue_access_token(
            subject_id=PUBLIC_SUBJECT_ID,
            email="shared-link",
            is_admin=False,
            grant_id=grant.id,
        )

        class _StubState:
            pass

        class _StubRequest:
            def __init__(self):
                self.state = _StubState()

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)
        result = await _resolve_credential(_StubRequest(), creds, db)
        assert result is None, "expired share-link grant must NOT authenticate"

    @pytest.mark.asyncio
    async def test_not_yet_valid_grant_drops_auth(self, world) -> None:
        """``valid_from`` in the future: the grant exists but is not yet
        active. Auth must refuse to honor it."""
        db, owner, patient = world
        grant = Grant(
            id=uuid.uuid4(),
            grantor_subject_id=owner.subject_id,
            grantee_subject_id=PUBLIC_SUBJECT_ID,
            resource_kind="patient",
            resource_id=patient.id,
            permissions=["read:metadata"],
            valid_from=datetime.now(UTC) + timedelta(hours=1),  # future
        )
        db.add(grant)
        await db.commit()
        raw = issue_access_token(
            subject_id=PUBLIC_SUBJECT_ID,
            email="shared-link",
            is_admin=False,
            grant_id=grant.id,
        )

        class _StubState:
            pass

        class _StubRequest:
            def __init__(self):
                self.state = _StubState()

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)
        result = await _resolve_credential(_StubRequest(), creds, db)
        assert result is None, "future-dated grant must not authenticate"

    @pytest.mark.asyncio
    async def test_valid_grant_authenticates_as_synthetic_public(self, world) -> None:
        """Positive path: a current, unrevoked grant authenticates the
        bearer as the synthetic public subject AND pins the grant on
        the User instance for downstream visibility filters."""
        db, owner, patient = world
        grant = Grant(
            id=uuid.uuid4(),
            grantor_subject_id=owner.subject_id,
            grantee_subject_id=PUBLIC_SUBJECT_ID,
            resource_kind="patient",
            resource_id=patient.id,
            permissions=["read:metadata"],
            valid_from=datetime.now(UTC) - timedelta(hours=1),
            valid_until=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(grant)
        await db.commit()
        raw = issue_access_token(
            subject_id=PUBLIC_SUBJECT_ID,
            email="shared-link",
            is_admin=False,
            grant_id=grant.id,
        )

        class _StubState:
            pass

        class _StubRequest:
            def __init__(self):
                self.state = _StubState()

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)
        req = _StubRequest()
        result = await _resolve_credential(req, creds, db)
        assert result is not None
        assert result.subject_id == PUBLIC_SUBJECT_ID
        # The resolver must pin the grant on the User instance and on
        # the request state, otherwise a share-link session would see
        # every patient currently shared via *any* link to public —
        # the cross-link leak the docstring at deps.py:166-176 warns
        # about.
        pinned = getattr(result, "_share_grant", None)
        assert pinned is not None
        assert pinned.id == grant.id
        assert getattr(req.state, "share_grant", None) is not None
        assert req.state.share_grant.id == grant.id


# ---------------------------------------------------------------------------
# 2. Branch isolation: two consultations don't contaminate each other
# ---------------------------------------------------------------------------


class TestConsultationBranchIsolation:
    """Two consultations on the same patient run on disjoint branches.
    A note written on one must not be visible from the other, and
    main must remain pristine until a merge happens."""

    @pytest.mark.asyncio
    async def test_two_consultation_branches_disjoint(self, world) -> None:
        db, owner, patient = world
        # Seed main.
        await commit_change(
            db,
            patient_id=patient.id,
            branch_ref="main",
            actor=ActorContext(subject_id=owner.subject_id),
            message="seed",
            changes=[
                EntityChange(
                    entity_kind="patient",
                    entity_id=patient.id,
                    payload={"id": str(patient.id), "_seed": True},
                )
            ],
        )
        await db.commit()

        # Open two consultation branches.
        cons_a = uuid.uuid4()
        cons_b = uuid.uuid4()
        await open_consultation_branch(
            db,
            patient_id=patient.id,
            consultation_id=cons_a,
            actor=ActorContext(subject_id=owner.subject_id),
        )
        await open_consultation_branch(
            db,
            patient_id=patient.id,
            consultation_id=cons_b,
            actor=ActorContext(subject_id=owner.subject_id),
        )
        await db.commit()

        # Write a different note on each branch.
        note_a, note_b = uuid.uuid4(), uuid.uuid4()
        res_a = await commit_change(
            db,
            patient_id=patient.id,
            branch_ref=f"consultation/{cons_a}",
            actor=ActorContext(subject_id=owner.subject_id),
            message="A note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_a,
                    payload={"id": str(note_a), "body": "branch A only"},
                )
            ],
        )
        await db.commit()
        res_b = await commit_change(
            db,
            patient_id=patient.id,
            branch_ref=f"consultation/{cons_b}",
            actor=ActorContext(subject_id=owner.subject_id),
            message="B note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_b,
                    payload={"id": str(note_b), "body": "branch B only"},
                )
            ],
        )
        await db.commit()

        state_a = await read_at_commit(db, commit_hash=res_a.commit_hash)
        state_b = await read_at_commit(db, commit_hash=res_b.commit_hash)
        # A's commit sees note A but not note B.
        assert ("clinical_note", note_a) in state_a
        assert ("clinical_note", note_b) not in state_a
        # B's commit sees note B but not note A.
        assert ("clinical_note", note_b) in state_b
        assert ("clinical_note", note_a) not in state_b

        # main remains pristine: it has the seed only, no clinical_note.
        main_head = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": patient.id},
            )
        ).scalar_one()
        main_state = await read_at_commit(db, commit_hash=main_head)
        kinds_on_main = {kind for (kind, _) in main_state}
        assert "clinical_note" not in kinds_on_main, "consultation writes leaked to main"


# ---------------------------------------------------------------------------
# 3. Owner-is-proposer policy
# ---------------------------------------------------------------------------


class TestOwnerAsProposerPolicy:
    """The owner may open a consultation on their own patient (e.g. to
    capture their own reasoning for review). The proposals endpoints
    only check ``_is_owner_or_admin`` for merge — so the owner can
    self-merge their own proposal. This test pins the policy so a
    future stricter rule (segregation of duties) cannot be added
    without an explicit decision.
    """

    @pytest.mark.asyncio
    async def test_owner_can_self_merge_own_consultation(self, world) -> None:
        from bvphoenix.services.versioning import (
            fast_forward_merge,
            submit_consultation_proposal,
        )

        db, owner, patient = world
        # Seed main and open a consultation as the owner.
        await commit_change(
            db,
            patient_id=patient.id,
            branch_ref="main",
            actor=ActorContext(subject_id=owner.subject_id),
            message="seed",
            changes=[
                EntityChange(
                    entity_kind="patient",
                    entity_id=patient.id,
                    payload={"id": str(patient.id)},
                )
            ],
        )
        await db.commit()

        cons_id = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO consultations "
                "(id, patient_id, author_subject_id, author_kind, status, title) "
                "VALUES (:id, :pid, :as, 'human', 'submitted', :ti)"
            ),
            {
                "id": cons_id,
                "pid": patient.id,
                "as": owner.subject_id,
                "ti": "owner self-consult",
            },
        )
        await open_consultation_branch(
            db,
            patient_id=patient.id,
            consultation_id=cons_id,
            actor=ActorContext(subject_id=owner.subject_id),
        )
        note_id = uuid.uuid4()
        await commit_change(
            db,
            patient_id=patient.id,
            branch_ref=f"consultation/{cons_id}",
            actor=ActorContext(subject_id=owner.subject_id),
            message="propose",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload={"id": str(note_id), "body": "self-proposed"},
                )
            ],
        )
        await db.commit()

        proposal_id = await submit_consultation_proposal(
            db,
            patient_id=patient.id,
            consultation_id=cons_id,
            proposer_subject_id=owner.subject_id,
            title="self review",
        )
        await db.commit()

        # Self-merge succeeds: the policy allows owner == proposer.
        merge_hash = await fast_forward_merge(
            db,
            proposal_id=proposal_id,
            reviewer_subject_id=owner.subject_id,
        )
        await db.commit()
        # main now sees the note.
        state = await read_at_commit(db, commit_hash=merge_hash, entity_kind="clinical_note")
        assert ("clinical_note", note_id) in state


# ---------------------------------------------------------------------------
# 4. Concurrent fast-forward merges to main
# ---------------------------------------------------------------------------


class TestConcurrentMergesToMain:
    """Two distinct proposals trying to fast-forward into main at the
    same time: only one can succeed. The loser must observe that main
    has moved (its base no longer matches target_head) and bail out
    with NotImplementedError (signal: "needs three-way merge")."""

    @pytest.mark.asyncio
    async def test_second_concurrent_ff_merge_detects_divergence(self, world) -> None:
        from bvphoenix.services.versioning import (
            fast_forward_merge,
            submit_consultation_proposal,
        )

        db, owner, patient = world
        # Seed main.
        await commit_change(
            db,
            patient_id=patient.id,
            branch_ref="main",
            actor=ActorContext(subject_id=owner.subject_id),
            message="seed",
            changes=[
                EntityChange(
                    entity_kind="patient",
                    entity_id=patient.id,
                    payload={"id": str(patient.id)},
                )
            ],
        )
        await db.commit()

        # Two consultations from the same main head with disjoint notes.
        async def _open_proposal(label: str) -> uuid.UUID:
            cons_id = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO consultations "
                    "(id, patient_id, author_subject_id, author_kind, "
                    " status, title) VALUES "
                    "(:id, :pid, :as, 'human', 'submitted', :ti)"
                ),
                {
                    "id": cons_id,
                    "pid": patient.id,
                    "as": owner.subject_id,
                    "ti": label,
                },
            )
            await open_consultation_branch(
                db,
                patient_id=patient.id,
                consultation_id=cons_id,
                actor=ActorContext(subject_id=owner.subject_id),
            )
            note = uuid.uuid4()
            await commit_change(
                db,
                patient_id=patient.id,
                branch_ref=f"consultation/{cons_id}",
                actor=ActorContext(subject_id=owner.subject_id),
                message=f"propose {label}",
                changes=[
                    EntityChange(
                        entity_kind="clinical_note",
                        entity_id=note,
                        payload={"id": str(note), "body": label},
                    )
                ],
            )
            await db.commit()
            pid = await submit_consultation_proposal(
                db,
                patient_id=patient.id,
                consultation_id=cons_id,
                proposer_subject_id=owner.subject_id,
                title=label,
            )
            await db.commit()
            return pid

        prop_a = await _open_proposal("A")
        prop_b = await _open_proposal("B")

        # Merge A first (sequential to prove it succeeds), then attempt B.
        await fast_forward_merge(
            db,
            proposal_id=prop_a,
            reviewer_subject_id=owner.subject_id,
        )
        await db.commit()
        # main moved; B's base no longer matches the new target_head.
        with pytest.raises(NotImplementedError):
            await fast_forward_merge(
                db,
                proposal_id=prop_b,
                reviewer_subject_id=owner.subject_id,
            )

    @pytest.mark.asyncio
    async def test_truly_parallel_ff_merges_one_wins_one_loses(self, world) -> None:
        """Fire two ``fast_forward_merge`` calls simultaneously on
        independent connections. Exactly one ``await`` completes
        successfully; the other must raise ``NotImplementedError``
        because its target_head moved while it was waiting on the
        ref lock."""
        from bvphoenix.services.versioning import (
            fast_forward_merge,
            submit_consultation_proposal,
        )

        db, owner, patient = world
        # Seed main + two proposals (sequentially via the test session).
        await commit_change(
            db,
            patient_id=patient.id,
            branch_ref="main",
            actor=ActorContext(subject_id=owner.subject_id),
            message="seed",
            changes=[
                EntityChange(
                    entity_kind="patient",
                    entity_id=patient.id,
                    payload={"id": str(patient.id)},
                )
            ],
        )
        await db.commit()

        async def _make_proposal(label: str) -> uuid.UUID:
            cons_id = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO consultations "
                    "(id, patient_id, author_subject_id, author_kind, "
                    " status, title) VALUES "
                    "(:id, :pid, :as, 'human', 'submitted', :ti)"
                ),
                {
                    "id": cons_id,
                    "pid": patient.id,
                    "as": owner.subject_id,
                    "ti": label,
                },
            )
            await open_consultation_branch(
                db,
                patient_id=patient.id,
                consultation_id=cons_id,
                actor=ActorContext(subject_id=owner.subject_id),
            )
            note = uuid.uuid4()
            await commit_change(
                db,
                patient_id=patient.id,
                branch_ref=f"consultation/{cons_id}",
                actor=ActorContext(subject_id=owner.subject_id),
                message=f"propose {label}",
                changes=[
                    EntityChange(
                        entity_kind="clinical_note",
                        entity_id=note,
                        payload={"id": str(note), "body": label},
                    )
                ],
            )
            await db.commit()
            pid = await submit_consultation_proposal(
                db,
                patient_id=patient.id,
                consultation_id=cons_id,
                proposer_subject_id=owner.subject_id,
                title=label,
            )
            await db.commit()
            return pid

        prop_a = await _make_proposal("A-parallel")
        prop_b = await _make_proposal("B-parallel")

        # Now run two fast_forward_merge calls on separate engines.
        settings = get_settings()
        eng_a = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
        eng_b = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
        sf_a = async_sessionmaker(eng_a, expire_on_commit=False)
        sf_b = async_sessionmaker(eng_b, expire_on_commit=False)

        async def _try_merge(factory, proposal_id: uuid.UUID):
            session = factory()
            try:
                await set_current_subject(session, SERVICE_SUBJECT)
                try:
                    await fast_forward_merge(
                        session,
                        proposal_id=proposal_id,
                        reviewer_subject_id=owner.subject_id,
                    )
                    await session.commit()
                    return "ok"
                except NotImplementedError:
                    await session.rollback()
                    return "diverged"
                except Exception as exc:
                    await session.rollback()
                    return f"error:{type(exc).__name__}"
            finally:
                await session.close()

        results = await asyncio.gather(
            _try_merge(sf_a, prop_a),
            _try_merge(sf_b, prop_b),
        )
        await eng_a.dispose()
        await eng_b.dispose()
        # Exactly one OK and one diverged.
        ok_count = sum(1 for r in results if r == "ok")
        diverged_count = sum(1 for r in results if r == "diverged")
        assert ok_count == 1, f"expected exactly 1 ok, got {results}"
        assert diverged_count == 1, f"expected 1 diverged, got {results}"
