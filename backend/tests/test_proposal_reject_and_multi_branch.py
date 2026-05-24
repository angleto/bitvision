"""Integration tests for two pieces of step C:

1. ``POST /proposals/{id}/reject`` — owner rejects a submitted
   consultation, the row goes to ``status='rejected'``, the source
   ref is locked, the consultation is marked rejected.
2. ``GET /patients/{id}/history/all`` — multi-branch timeline that
   aggregates commits from main + each consultation branch.

The tests run via FastAPI's ASGITransport with the route handlers
short-circuited to the test session (mirrors the pattern used in
``test_versioning_endpoint_wiring``). They need a real Postgres with
the F12 schema applied — same skip rule as the other versioning tests.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bvphoenix.auth import optional_user, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import Patient, User
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
    open_consultation_branch,
    submit_consultation_proposal,
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with F12 migrations applied",
)


@pytest_asyncio.fixture
async def owner_session() -> AsyncIterator[tuple[AsyncSession, User, uuid.UUID]]:
    """Yield ``(db, owner_user, patient_id)`` with the patient owned by user."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    sid = uuid.uuid4()
    pid = uuid.uuid4()
    try:
        await set_current_subject(db, SERVICE_SUBJECT)
        db.add(Subject(id=sid, kind="user", display_name=f"reject-test-{sid}"))
        await db.flush()
        user = User(
            subject_id=sid,
            email=f"reject-{sid}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user)
        db.add(
            Patient(
                id=pid,
                managed_by_subject_id=sid,
                display_name="Reject test patient",
            )
        )
        await db.commit()
        yield db, user, pid
    finally:
        try:
            await db.rollback()
            await set_current_subject(db, SERVICE_SUBJECT)
            await db.execute(text("DELETE FROM patients WHERE id = :p"), {"p": pid})
            await db.execute(text("DELETE FROM users WHERE subject_id = :s"), {"s": sid})
            await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": sid})
            await db.commit()
        finally:
            await db.close()
            await engine.dispose()


def _client_for(session: AsyncSession, user: User) -> AsyncClient:
    async def _dep_db():
        yield session

    async def _dep_user():
        return user

    app.dependency_overrides[get_db] = _dep_db
    app.dependency_overrides[require_user] = _dep_user
    app.dependency_overrides[optional_user] = _dep_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _setup_open_proposal(
    db: AsyncSession, user: User, patient_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Build a submitted consultation + proposal and return both ids.

    Seeds main with one note, opens a consultation branch, lets the
    branch diverge with a unrelated edit, and submits.
    """
    note_id = uuid.uuid4()
    base_payload = {
        "id": str(note_id),
        "patient_id": str(patient_id),
        "target_kind": "patient",
        "target_id": str(patient_id),
        "body": "base body",
        "author_subject_id": str(user.subject_id),
        "author_kind": "human",
    }
    actor = ActorContext(subject_id=user.subject_id, kind="human")
    await commit_change(
        db,
        patient_id=patient_id,
        branch_ref="main",
        actor=actor,
        message="seed",
        changes=[
            EntityChange(
                entity_kind="clinical_note",
                entity_id=note_id,
                payload=base_payload,
            )
        ],
    )

    consultation_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO consultations "
            "(id, patient_id, author_subject_id, author_kind, "
            " status, title, created_at, updated_at) "
            "VALUES (:id, :pid, :sid, 'human', 'submitted', "
            "  'reject test consultation', now(), now())"
        ),
        {"id": consultation_id, "pid": patient_id, "sid": user.subject_id},
    )
    consult_ref = await open_consultation_branch(
        db,
        patient_id=patient_id,
        consultation_id=consultation_id,
        actor=actor,
    )
    # Source-side edit to the same note; main is also edited so we
    # exercise the divergent branch case.
    await commit_change(
        db,
        patient_id=patient_id,
        branch_ref=consult_ref,
        actor=actor,
        message="consult edit",
        changes=[
            EntityChange(
                entity_kind="clinical_note",
                entity_id=note_id,
                payload={**base_payload, "body": "edited on consult"},
            )
        ],
    )
    await commit_change(
        db,
        patient_id=patient_id,
        branch_ref="main",
        actor=actor,
        message="main edit",
        changes=[
            EntityChange(
                entity_kind="clinical_note",
                entity_id=note_id,
                payload={**base_payload, "body": "edited on main"},
            )
        ],
    )
    proposal_id = await submit_consultation_proposal(
        db,
        patient_id=patient_id,
        consultation_id=consultation_id,
        proposer_subject_id=user.subject_id,
        title="reject me",
    )
    await db.commit()
    return proposal_id, consultation_id


# ---------------------------------------------------------------------------
# /proposals/{id}/reject
# ---------------------------------------------------------------------------


class TestRejectProposal:
    @pytest.mark.asyncio
    async def test_reject_marks_status_rejected_and_locks_branch(self, owner_session) -> None:
        db, user, pid = owner_session
        proposal_id, consultation_id = await _setup_open_proposal(db, user, pid)

        client = _client_for(db, user)
        try:
            r = await client.post(
                f"/api/proposals/{proposal_id}/reject",
                json={"review_notes": "differs from clinical guidance"},
            )
            assert r.status_code == 200, r.text
            payload = r.json()
            assert payload["status"] == "rejected"
            assert payload["review_decision"] == "reject"
            assert payload["review_notes"] == "differs from clinical guidance"
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

        # Source ref is locked.
        is_locked = (
            await db.execute(
                text("SELECT is_locked FROM refs WHERE patient_id = :p AND ref_name = :r"),
                {"p": pid, "r": f"consultation/{consultation_id}"},
            )
        ).scalar_one()
        assert is_locked is True

        # Consultation row marked rejected with the reason.
        cons_row = (
            await db.execute(
                text("SELECT status, rejected_reason FROM consultations WHERE id = :c"),
                {"c": consultation_id},
            )
        ).first()
        assert cons_row is not None
        assert cons_row[0] == "rejected"
        assert cons_row[1] == "differs from clinical guidance"

    @pytest.mark.asyncio
    async def test_reject_requires_non_empty_reason(self, owner_session) -> None:
        db, user, pid = owner_session
        proposal_id, _ = await _setup_open_proposal(db, user, pid)
        client = _client_for(db, user)
        try:
            r = await client.post(
                f"/api/proposals/{proposal_id}/reject",
                json={"review_notes": ""},
            )
            assert r.status_code == 422, r.text
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_reject_409_when_already_closed(self, owner_session) -> None:
        db, user, pid = owner_session
        proposal_id, _ = await _setup_open_proposal(db, user, pid)
        client = _client_for(db, user)
        try:
            r1 = await client.post(
                f"/api/proposals/{proposal_id}/reject",
                json={"review_notes": "first reject"},
            )
            assert r1.status_code == 200
            r2 = await client.post(
                f"/api/proposals/{proposal_id}/reject",
                json={"review_notes": "second reject"},
            )
            assert r2.status_code == 409, r2.text
        finally:
            await client.aclose()
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /history/all
# ---------------------------------------------------------------------------


class TestMultiBranchHistory:
    @pytest.mark.asyncio
    async def test_walk_returns_main_plus_consultation(self, owner_session) -> None:
        db, user, pid = owner_session
        actor = ActorContext(subject_id=user.subject_id, kind="human")

        # Two commits on main.
        note_a = uuid.uuid4()
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=actor,
            message="main: add A",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_a,
                    payload={
                        "id": str(note_a),
                        "patient_id": str(pid),
                        "target_kind": "patient",
                        "target_id": str(pid),
                        "body": "A",
                        "author_subject_id": str(user.subject_id),
                        "author_kind": "human",
                    },
                )
            ],
        )
        note_b = uuid.uuid4()
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=actor,
            message="main: add B",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_b,
                    payload={
                        "id": str(note_b),
                        "patient_id": str(pid),
                        "target_kind": "patient",
                        "target_id": str(pid),
                        "body": "B",
                        "author_subject_id": str(user.subject_id),
                        "author_kind": "human",
                    },
                )
            ],
        )
        # Open a consultation branch and commit one note on it.
        cons_id = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO consultations "
                "(id, patient_id, author_subject_id, author_kind, "
                " status, title, created_at, updated_at) "
                "VALUES (:id, :pid, :sid, 'human', 'draft', "
                "  'multi-branch consultation', now(), now())"
            ),
            {"id": cons_id, "pid": pid, "sid": user.subject_id},
        )
        consult_ref = await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=cons_id,
            actor=actor,
        )
        note_c = uuid.uuid4()
        await commit_change(
            db,
            patient_id=pid,
            branch_ref=consult_ref,
            actor=actor,
            message="consult: add C",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_c,
                    payload={
                        "id": str(note_c),
                        "patient_id": str(pid),
                        "target_kind": "patient",
                        "target_id": str(pid),
                        "body": "C",
                        "author_subject_id": str(user.subject_id),
                        "author_kind": "human",
                    },
                )
            ],
        )
        await db.commit()

        client = _client_for(db, user)
        try:
            r = await client.get(f"/api/patients/{pid}/history/all")
            assert r.status_code == 200, r.text
            payload = r.json()
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

        ref_names = {r["ref_name"] for r in payload["refs"]}
        assert "main" in ref_names
        assert consult_ref in ref_names

        # All three messages are visible. The consultation commit's
        # branch_at_creation is the consultation ref; main's is 'main'.
        messages = [c["message"] for c in payload["commits"]]
        assert "main: add A" in messages
        assert "main: add B" in messages
        assert "consult: add C" in messages
        consult_commit = next(c for c in payload["commits"] if c["message"] == "consult: add C")
        assert consult_commit["branch_at_creation"] == consult_ref

        # Newest-first ordering: the last commit we wrote is the
        # consultation commit, so it should be first.
        assert payload["commits"][0]["message"] == "consult: add C"
