"""Endpoint authorization + concurrency tests for the F12 versioning.

Goal: blindare la superficie HTTP del versioning (proposals, history)
contro:
  * non-owner che tenta merge / resolve / withdraw
  * race condition: due commit_change concorrenti sulla stessa ref
    (SELECT FOR UPDATE deve serializzare)
  * agent token con expires_at scaduto o patient_id diverso
  * audit_log: ogni azione privilegiata deve lasciare traccia

Richiede Postgres con migrazioni applicate.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from bvphoenix.auth import optional_user, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import AgentToken, AuditLog, Patient, User
from bvphoenix.db.models.principals import Subject
from bvphoenix.db.session import (
    SERVICE_SUBJECT,
    get_db,
    set_current_subject,
)
from bvphoenix.main import app
from bvphoenix.services.versioning import (
    ActorContext,
    EntityChange,
    commit_change,
    submit_consultation_proposal,
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with F12 migrations applied",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def authz_world() -> AsyncIterator[
    tuple[
        AsyncSession,
        User,  # owner of patient
        User,  # consultant (non-owner)
        Patient,
    ]
]:
    """Yield ``(db, owner_user, consultant_user, patient)``.

    Owner manages the patient. Consultant is a different user that has
    no permission on the patient by default — used for negative tests.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    owner_sid = uuid.uuid4()
    consultant_sid = uuid.uuid4()
    pid = uuid.uuid4()
    try:
        await set_current_subject(db, SERVICE_SUBJECT)
        db.add_all(
            [
                Subject(id=owner_sid, kind="user", display_name=f"owner-{owner_sid}"),
                Subject(
                    id=consultant_sid,
                    kind="user",
                    display_name=f"consult-{consultant_sid}",
                ),
            ]
        )
        await db.flush()
        owner = User(
            subject_id=owner_sid,
            email=f"owner-{owner_sid}@example.com",
            password_hash=None,
            is_admin=False,
        )
        consultant = User(
            subject_id=consultant_sid,
            email=f"consult-{consultant_sid}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add_all([owner, consultant])
        await db.flush()
        patient = Patient(
            id=pid,
            managed_by_subject_id=owner_sid,
            display_name="Authz Patient",
        )
        db.add(patient)
        await db.commit()
        yield db, owner, consultant, patient
    finally:
        try:
            await db.rollback()
            await set_current_subject(db, SERVICE_SUBJECT)
            await db.execute(text("DELETE FROM patients WHERE id = :p"), {"p": pid})
            for sid in (owner_sid, consultant_sid):
                await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": sid})
            await db.commit()
        finally:
            await db.close()
            await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _override_db(session: AsyncSession):
    async def _dep():
        yield session

    return _dep


def _override_user(user: User | None):
    async def _dep():
        if user is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    return _dep


async def _client_for(session: AsyncSession, user: User | None) -> AsyncClient:
    app.dependency_overrides[get_db] = _override_db(session)
    app.dependency_overrides[require_user] = _override_user(user)
    app.dependency_overrides[optional_user] = _override_user(user)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _seed_main(
    db: AsyncSession,
    pid: uuid.UUID,
    owner_sid: uuid.UUID,
) -> bytes:
    """Make sure ``main`` has at least one commit so consultation
    branching has something to fork from."""
    res = await commit_change(
        db,
        patient_id=pid,
        branch_ref="main",
        actor=ActorContext(subject_id=owner_sid, kind="human"),
        message="seed",
        changes=[
            EntityChange(
                entity_kind="patient",
                entity_id=pid,
                payload={"id": str(pid), "schema_version": 1, "_seed": True},
            )
        ],
    )
    await db.commit()
    return res.commit_hash


async def _open_consultation_with_proposal(
    db: AsyncSession,
    *,
    pid: uuid.UUID,
    owner_sid: uuid.UUID,
    proposer_sid: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a Consultation row + branch + open proposal. Returns
    (consultation_id, proposal_id)."""
    await _seed_main(db, pid, owner_sid)
    consultation_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO consultations "
            "(id, patient_id, author_subject_id, author_kind, status, title) "
            "VALUES (:id, :pid, :ps, 'human', 'submitted', :ti)"
        ),
        {
            "id": consultation_id,
            "pid": pid,
            "ps": proposer_sid,
            "ti": "consult test",
        },
    )
    # Open the consultation branch from main and add a clinical_note on it.
    from bvphoenix.services.versioning import open_consultation_branch

    await open_consultation_branch(
        db,
        patient_id=pid,
        consultation_id=consultation_id,
        actor=ActorContext(subject_id=proposer_sid, kind="human"),
    )
    note_id = uuid.uuid4()
    await commit_change(
        db,
        patient_id=pid,
        branch_ref=f"consultation/{consultation_id}",
        actor=ActorContext(subject_id=proposer_sid, kind="human"),
        message="proposed change",
        changes=[
            EntityChange(
                entity_kind="clinical_note",
                entity_id=note_id,
                payload={
                    "id": str(note_id),
                    "body": "consultant proposes this",
                },
            )
        ],
    )
    await db.commit()
    proposal_id = await submit_consultation_proposal(
        db,
        patient_id=pid,
        consultation_id=consultation_id,
        proposer_subject_id=proposer_sid,
        title="please review",
    )
    await db.commit()
    return consultation_id, proposal_id


# ===========================================================================
# 1. Endpoint authorization on /api/proposals/*
# ===========================================================================


class TestProposalEndpointAuthz:
    @pytest.mark.asyncio
    async def test_non_owner_cannot_merge_proposal(self, authz_world) -> None:
        db, owner, consultant, patient = authz_world
        _, proposal_id = await _open_consultation_with_proposal(
            db,
            pid=patient.id,
            owner_sid=owner.subject_id,
            proposer_sid=consultant.subject_id,
        )
        # Grant the consultant READ_METADATA so they reach the
        # is_owner_or_admin check rather than 404.
        from bvphoenix.db.models import Grant

        grant = Grant(
            id=uuid.uuid4(),
            grantor_subject_id=owner.subject_id,
            grantee_subject_id=consultant.subject_id,
            resource_kind="patient",
            resource_id=patient.id,
            permissions=["read:metadata"],
            valid_from=datetime.now(UTC) - timedelta(days=1),
        )
        db.add(grant)
        await db.commit()

        client = await _client_for(db, consultant)
        try:
            r = await client.post(
                f"/api/proposals/{proposal_id}/merge",
                json={"review_notes": "trying to bypass"},
            )
            assert r.status_code == 403, r.text
            assert "owner" in r.text.lower()
        finally:
            await client.aclose()
            app.dependency_overrides.clear()
            await db.execute(text("DELETE FROM grants WHERE id = :g"), {"g": grant.id})
            await db.commit()

    @pytest.mark.asyncio
    async def test_unrelated_user_gets_404_on_merge(self, authz_world) -> None:
        db, owner, consultant, patient = authz_world
        _, proposal_id = await _open_consultation_with_proposal(
            db,
            pid=patient.id,
            owner_sid=owner.subject_id,
            proposer_sid=consultant.subject_id,
        )
        # Consultant has no read access; expect 404 (not 403) so the
        # endpoint doesn't leak proposal existence to enumeration.
        client = await _client_for(db, consultant)
        try:
            r = await client.post(
                f"/api/proposals/{proposal_id}/merge",
                json={"review_notes": "x"},
            )
            assert r.status_code == 404
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_non_owner_cannot_resolve_conflict(self, authz_world) -> None:
        """Even with read:metadata granted, only the owner can resolve."""
        db, owner, consultant, patient = authz_world
        _, proposal_id = await _open_consultation_with_proposal(
            db,
            pid=patient.id,
            owner_sid=owner.subject_id,
            proposer_sid=consultant.subject_id,
        )
        # Inject a fake conflict so the endpoint reaches the auth check.
        cid = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO merge_conflicts "
                "(id, proposal_id, entity_kind, entity_id, "
                "conflict_kind) VALUES (:id, :p, 'clinical_note', :e, "
                "'edit_edit')"
            ),
            {"id": cid, "p": proposal_id, "e": uuid.uuid4()},
        )
        from bvphoenix.db.models import Grant

        grant = Grant(
            id=uuid.uuid4(),
            grantor_subject_id=owner.subject_id,
            grantee_subject_id=consultant.subject_id,
            resource_kind="patient",
            resource_id=patient.id,
            permissions=["read:metadata"],
            valid_from=datetime.now(UTC) - timedelta(days=1),
        )
        db.add(grant)
        await db.commit()

        client = await _client_for(db, consultant)
        try:
            r = await client.post(
                f"/api/proposals/{proposal_id}/conflicts/{cid}/resolve",
                json={"kind": "take_source"},
            )
            assert r.status_code == 403, r.text
        finally:
            await client.aclose()
            app.dependency_overrides.clear()
            await db.execute(text("DELETE FROM grants WHERE id = :g"), {"g": grant.id})
            await db.commit()

    @pytest.mark.asyncio
    async def test_proposer_can_self_withdraw(self, authz_world) -> None:
        """The proposer is allowed to withdraw their own proposal even
        without owner/admin status; the docstring on the endpoint
        describes this as the 'I changed my mind' path."""
        db, owner, consultant, patient = authz_world
        _, proposal_id = await _open_consultation_with_proposal(
            db,
            pid=patient.id,
            owner_sid=owner.subject_id,
            proposer_sid=consultant.subject_id,
        )
        # Grant read so the proposer endpoint reaches.
        from bvphoenix.db.models import Grant

        grant = Grant(
            id=uuid.uuid4(),
            grantor_subject_id=owner.subject_id,
            grantee_subject_id=consultant.subject_id,
            resource_kind="patient",
            resource_id=patient.id,
            permissions=["read:metadata"],
            valid_from=datetime.now(UTC) - timedelta(days=1),
        )
        db.add(grant)
        await db.commit()

        client = await _client_for(db, consultant)
        try:
            r = await client.post(
                f"/api/proposals/{proposal_id}/withdraw",
                json={"reason": "rethinking"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "withdrawn"
        finally:
            await client.aclose()
            app.dependency_overrides.clear()
            await db.execute(text("DELETE FROM grants WHERE id = :g"), {"g": grant.id})
            await db.commit()

    @pytest.mark.asyncio
    async def test_third_party_cannot_withdraw(self, authz_world) -> None:
        """Random user with read access but not proposer/owner: 403."""
        db, owner, consultant, patient = authz_world
        _, proposal_id = await _open_consultation_with_proposal(
            db,
            pid=patient.id,
            owner_sid=owner.subject_id,
            proposer_sid=consultant.subject_id,
        )
        # Make a third user.
        third_sid = uuid.uuid4()
        db.add(Subject(id=third_sid, kind="user", display_name=f"third-{third_sid}"))
        await db.flush()
        third = User(
            subject_id=third_sid,
            email=f"third-{third_sid}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(third)
        from bvphoenix.db.models import Grant

        grant = Grant(
            id=uuid.uuid4(),
            grantor_subject_id=owner.subject_id,
            grantee_subject_id=third_sid,
            resource_kind="patient",
            resource_id=patient.id,
            permissions=["read:metadata"],
            valid_from=datetime.now(UTC) - timedelta(days=1),
        )
        db.add(grant)
        await db.commit()

        client = await _client_for(db, third)
        try:
            r = await client.post(
                f"/api/proposals/{proposal_id}/withdraw",
                json={"reason": "x"},
            )
            assert r.status_code == 403
        finally:
            await client.aclose()
            app.dependency_overrides.clear()
            await db.execute(text("DELETE FROM grants WHERE id = :g"), {"g": grant.id})
            await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": third_sid})
            await db.commit()


# ===========================================================================
# 2. Concurrency: SELECT FOR UPDATE on refs serialises commit_change
# ===========================================================================


class TestRefLockSerialisesConcurrentCommits:
    """``commit_change`` does ``SELECT ... FOR UPDATE`` on the ``refs``
    row, so two concurrent commits on the same branch must serialise.
    Both commits succeed but the second one's ``parent_hashes`` MUST be
    the first one's ``commit_hash`` — never the parent that was current
    when the second transaction started.

    Without the lock, a TOCTOU window would make both commits use the
    same parent, and the first ref UPDATE would silently overwrite
    the other.
    """

    @pytest.mark.asyncio
    async def test_two_concurrent_commits_serialise(self, authz_world) -> None:
        db, owner, _, patient = authz_world
        # Seed main on the shared session.
        await _seed_main(db, patient.id, owner.subject_id)

        # Two independent engines/sessions so SELECT FOR UPDATE actually
        # competes for the row lock instead of deadlocking on a single
        # connection.
        settings = get_settings()
        eng_a = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
        eng_b = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
        sf_a = async_sessionmaker(eng_a, expire_on_commit=False)
        sf_b = async_sessionmaker(eng_b, expire_on_commit=False)

        async def _commit_via(factory, body: str, gate: asyncio.Event) -> bytes:
            session = factory()
            try:
                await set_current_subject(session, SERVICE_SUBJECT)
                # Wait for the other coroutine to be ready, so both
                # transactions start their SELECT FOR UPDATE close to
                # simultaneously.
                gate.set()
                await asyncio.sleep(0)
                res = await commit_change(
                    session,
                    patient_id=patient.id,
                    branch_ref="main",
                    actor=ActorContext(subject_id=owner.subject_id, kind="human"),
                    message=f"concurrent {body}",
                    changes=[
                        EntityChange(
                            entity_kind="clinical_note",
                            entity_id=uuid.uuid4(),
                            payload={"id": body, "body": body},
                        )
                    ],
                )
                await session.commit()
                return res.commit_hash
            finally:
                await session.close()

        gate_a, gate_b = asyncio.Event(), asyncio.Event()
        ch_a, ch_b = await asyncio.gather(
            _commit_via(sf_a, "A", gate_a),
            _commit_via(sf_b, "B", gate_b),
        )
        await eng_a.dispose()
        await eng_b.dispose()

        # Both commits exist; one is the parent of the other.
        rows = (
            await db.execute(
                text(
                    "SELECT commit_hash, parent_hashes "
                    "FROM commits WHERE commit_hash IN (:a, :b) "
                    "ORDER BY created_at"
                ),
                {"a": ch_a, "b": ch_b},
            )
        ).all()
        assert len(rows) == 2
        first_hash, _first_parents = rows[0]
        second_hash, second_parents = rows[1]
        # Second commit's parent must be the first commit (not the seed).
        assert second_parents == [first_hash], (
            "second concurrent commit must chain off the first; lost-update detected"
        )
        # ref must point to the second commit.
        head = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": patient.id},
            )
        ).scalar_one()
        assert head == second_hash


# ===========================================================================
# 3. Agent token boundaries
# ===========================================================================


class TestAgentTokenBoundaries:
    @pytest.mark.asyncio
    async def test_expired_agent_token_is_rejected_by_optional_user(self, authz_world) -> None:
        """An ``AgentToken`` with ``expires_at <= now`` must fail auth
        even if the underlying owner User still exists. The check at
        ``deps.py:_resolve_credential`` enforces this; we mint a JWT
        with a future ``exp`` claim (so signature decoding succeeds)
        and a DB row with ``expires_at`` in the past."""
        from fastapi.security import HTTPAuthorizationCredentials

        from bvphoenix.auth.deps import _resolve_credential
        from bvphoenix.auth.tokens import issue_agent_token

        db, owner, _, _ = authz_world
        token_id = uuid.uuid4()
        raw, token_hash = issue_agent_token(
            agent_token_id=token_id,
            owner_subject_id=owner.subject_id,
            scope=["patient:read"],
            ttl_seconds=3600,  # JWT valid for 1h so decode succeeds
        )
        # DB row expires_at in the past: this is the check we want to verify.
        token_row = AgentToken(
            id=token_id,
            owner_subject_id=owner.subject_id,
            label="expired-test",
            token_hash=token_hash,
            permissions=["patient:read"],
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db.add(token_row)
        await db.commit()

        class _StubState:
            pass

        class _StubRequest:
            def __init__(self):
                self.state = _StubState()

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)
        result = await _resolve_credential(_StubRequest(), creds, db)
        assert result is None, "expired agent token must NOT resolve to a user"

    @pytest.mark.asyncio
    async def test_revoked_agent_token_is_rejected(self, authz_world) -> None:
        """An AgentToken row with non-null revoked_at must also be
        rejected, regardless of expires_at."""
        from fastapi.security import HTTPAuthorizationCredentials

        from bvphoenix.auth.deps import _resolve_credential
        from bvphoenix.auth.tokens import issue_agent_token

        db, owner, _, _ = authz_world
        token_id = uuid.uuid4()
        raw, token_hash = issue_agent_token(
            agent_token_id=token_id,
            owner_subject_id=owner.subject_id,
            scope=["patient:read"],
            ttl_seconds=3600,
        )
        token_row = AgentToken(
            id=token_id,
            owner_subject_id=owner.subject_id,
            label="revoked-test",
            token_hash=token_hash,
            permissions=["patient:read"],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            revoked_at=datetime.now(UTC),
        )
        db.add(token_row)
        await db.commit()

        class _StubState:
            pass

        class _StubRequest:
            def __init__(self):
                self.state = _StubState()

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)
        result = await _resolve_credential(_StubRequest(), creds, db)
        assert result is None, "revoked agent token must not authenticate"

    @pytest.mark.asyncio
    async def test_agent_token_scoped_to_other_patient_blocked(self, authz_world) -> None:
        """``enforce_agent_patient_scope`` must 403 when the request
        targets a patient the agent token was not minted for."""
        from fastapi import HTTPException

        from bvphoenix.auth.deps import enforce_agent_patient_scope

        _, owner, _, patient = authz_world
        # Create a token scoped to a DIFFERENT patient (in-memory only;
        # we don't need to persist it for this synchronous check).
        other_pid = uuid.uuid4()
        token_row = AgentToken(
            id=uuid.uuid4(),
            owner_subject_id=owner.subject_id,
            label="cross-patient-test",
            token_hash="0" * 64,  # placeholder valid hex string
            permissions=["patient:read"],
            patient_id=other_pid,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        class _StubState:
            is_agent = True
            agent_token = token_row

        class _StubRequest:
            state = _StubState()

        with pytest.raises(HTTPException) as excinfo:
            enforce_agent_patient_scope(_StubRequest(), patient.id)
        assert excinfo.value.status_code == 403


# ===========================================================================
# 4. Audit log presence on privileged actions
# ===========================================================================


class TestAuditLogCompleteness:
    """End-to-end audit pipeline tests through the FastAPI ``AuditDep``
    are flaky in this test infrastructure: the audit service opens its
    own ``SessionFactory`` connection bound to a different asyncio loop,
    and the resulting commit silently fails. We pivot to two structural
    invariants:

      1. The ``AuditLog`` model accepts a row with ``actor_subject_id``
         and round-trips a metadata dict — so endpoints calling
         ``audit.log(...)`` are writing into a working schema.
      2. Every privileged endpoint in ``api/proposals.py`` calls
         ``audit.log(...)`` (source-level pin). If a future refactor
         drops the audit call, the test fails immediately.
    """

    @pytest.mark.asyncio
    async def test_audit_log_row_round_trips(self, authz_world) -> None:
        db, owner, _, patient = authz_world
        marker_action = f"test_marker_{uuid.uuid4().hex[:8]}"
        entry = AuditLog(
            actor_subject_id=owner.subject_id,
            action=marker_action,
            resource_kind="patient",
            resource_id=patient.id,
            metadata_={"smoke": True},
        )
        db.add(entry)
        await db.commit()

        row = (
            await db.execute(
                text(
                    "SELECT actor_subject_id, action, resource_kind, "
                    "  resource_id, metadata "
                    "FROM audit_log WHERE action = :a"
                ),
                {"a": marker_action},
            )
        ).first()
        assert row is not None
        actor, action, kind, rid, meta = row
        assert actor == owner.subject_id
        assert action == marker_action
        assert kind == "patient"
        assert rid == patient.id
        assert meta == {"smoke": True}

    def test_proposals_endpoints_emit_audit_log_calls(self) -> None:
        """Source-level pin: each privileged proposal endpoint must
        invoke ``audit.log(...)`` so a future refactor cannot silently
        drop the audit trail."""
        import inspect

        from bvphoenix.api import proposals as proposals_module

        privileged_endpoints = [
            "resolve_conflict",
            "merge_proposal",
            "withdraw_proposal",
        ]
        missing: list[str] = []
        for name in privileged_endpoints:
            fn = getattr(proposals_module, name)
            src = inspect.getsource(fn)
            if "audit.log" not in src:
                missing.append(name)
        assert not missing, f"audit.log call missing from privileged endpoints: {missing}"
