"""Severe security tests for the F12 versioning + erasure + publish path.

Goal: blindare le invarianti che non devono mai rompersi anche sotto
input adversarial. Ogni test codifica una invariante; se fallisce, c'e'
un bug reale, non un dettaglio cosmetico.

Aree:
  * Cross-patient leak via /at, /history, /diff, /ref-log
  * Tombstoning + delta-chain integrity
  * GDPR erasure scrubs entity_objects content
  * Publish: source patient never mutated, no source-id propagation
  * ref_log immutability (RLS append-only)

Richiede Postgres con migrazioni F12 applicate, identico a test_versioning.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bvphoenix.auth import optional_user, require_user
from bvphoenix.config import get_settings
from bvphoenix.db.models import DataErasureRequest, Patient, User
from bvphoenix.db.models.principals import Subject
from bvphoenix.db.session import (
    SERVICE_SUBJECT,
    get_db,
    set_current_subject,
)
from bvphoenix.main import app
from bvphoenix.services.erasure import execute_erasure
from bvphoenix.services.publish import publish_patient_to_opendata
from bvphoenix.services.versioning import (
    ActorContext,
    EntityChange,
    commit_change,
    pack_entity_objects,
    read_at_commit,
    read_object,
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with F12 migrations applied",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_fascicoli() -> AsyncIterator[
    tuple[
        AsyncSession,
        tuple[uuid.UUID, uuid.UUID],
        tuple[uuid.UUID, uuid.UUID],
    ]
]:
    """Yield ``(db, (sid_a, pid_a), (sid_b, pid_b))``.

    Two independent fascicoli so cross-patient invariants can be probed.
    Service-bypass session: tests must explicitly switch RLS context if
    they want to assert RLS-level isolation.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    sid_a, pid_a = uuid.uuid4(), uuid.uuid4()
    sid_b, pid_b = uuid.uuid4(), uuid.uuid4()
    try:
        await set_current_subject(db, SERVICE_SUBJECT)
        db.add_all(
            [
                Subject(id=sid_a, kind="user", display_name=f"sec-A-{sid_a}"),
                Subject(id=sid_b, kind="user", display_name=f"sec-B-{sid_b}"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Patient(
                    id=pid_a,
                    managed_by_subject_id=sid_a,
                    display_name="Fascicolo A",
                ),
                Patient(
                    id=pid_b,
                    managed_by_subject_id=sid_b,
                    display_name="Fascicolo B",
                ),
            ]
        )
        await db.commit()
        yield db, (sid_a, pid_a), (sid_b, pid_b)
    finally:
        try:
            await db.rollback()
            await set_current_subject(db, SERVICE_SUBJECT)
            for pid in (pid_a, pid_b):
                await db.execute(text("DELETE FROM patients WHERE id = :p"), {"p": pid})
            for sid in (sid_a, sid_b):
                await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": sid})
            await db.commit()
        finally:
            await db.close()
            await engine.dispose()


@pytest_asyncio.fixture
async def fascicolo_with_user() -> AsyncIterator[tuple[AsyncSession, User, Patient]]:
    """Yield ``(db, owner_user, patient)``: a User row and a managed Patient.

    Used by endpoint-level tests that need to inject ``require_user``.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    sid = uuid.uuid4()
    pid = uuid.uuid4()
    try:
        await set_current_subject(db, SERVICE_SUBJECT)
        db.add(Subject(id=sid, kind="user", display_name=f"sec-user-{sid}"))
        await db.flush()
        user = User(
            subject_id=sid,
            email=f"sec-{sid}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user)
        await db.flush()
        patient = Patient(
            id=pid,
            managed_by_subject_id=sid,
            display_name="Sec Test Patient",
        )
        db.add(patient)
        await db.commit()
        yield db, user, patient
    finally:
        try:
            await db.rollback()
            await set_current_subject(db, SERVICE_SUBJECT)
            await db.execute(text("DELETE FROM patients WHERE id = :p"), {"p": pid})
            await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": sid})
            await db.commit()
        finally:
            await db.close()
            await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _commit_note(
    db: AsyncSession,
    *,
    pid: uuid.UUID,
    sid: uuid.UUID,
    body: str,
    note_id: uuid.UUID | None = None,
    branch_ref: str = "main",
    message: str = "test commit",
) -> tuple[bytes, uuid.UUID]:
    """Helper: commit a single clinical_note on ``main``. Returns
    (commit_hash, note_id)."""
    note_id = note_id or uuid.uuid4()
    payload = {
        "id": str(note_id),
        "patient_id": str(pid),
        "target_kind": "patient",
        "target_id": str(pid),
        "body": body,
        "author_subject_id": str(sid),
        "author_kind": "human",
    }
    result = await commit_change(
        db,
        patient_id=pid,
        branch_ref=branch_ref,
        actor=ActorContext(subject_id=sid, kind="human"),
        message=message,
        changes=[
            EntityChange(
                entity_kind="clinical_note",
                entity_id=note_id,
                payload=payload,
            )
        ],
    )
    await db.commit()
    return result.commit_hash, note_id


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


# ===========================================================================
# 1. Cross-patient leak via history/at/diff/ref-log endpoints
# ===========================================================================


class TestCrossPatientLeak:
    """Defense in depth: every history endpoint must refuse to leak data
    about a patient the caller cannot read, even if the caller knows
    (or guesses) commit_hashes that belong to that patient."""

    @pytest.mark.asyncio
    async def test_at_commit_404_when_commit_belongs_to_other_patient(self, two_fascicoli) -> None:
        """User A asks /at/<B's commit_hash> against patient A's URL.

        Even though A is authorised on patient A and the commit is a
        valid hex string, the endpoint must respond 404 because the
        commit does not belong to patient A.
        """
        db, (sid_a, pid_a), (sid_b, pid_b) = two_fascicoli
        commit_b, _ = await _commit_note(db, pid=pid_b, sid=sid_b, body="patient B confidential")
        # User A is the owner of patient A; build a User row inline.
        user_a = User(
            subject_id=sid_a,
            email=f"a-{sid_a}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user_a)
        await db.commit()

        client = await _client_for(db, user_a)
        try:
            r = await client.get(f"/api/patients/{pid_a}/at/{commit_b.hex()}")
            assert r.status_code == 404
            # Body must NOT echo any field of patient B.
            assert "confidential" not in r.text
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_at_commit_404_when_caller_cannot_read_target_patient(
        self, two_fascicoli
    ) -> None:
        """User A asks /at against patient B's URL with B's own commit.

        The caller is unauthorised on patient B; the endpoint must
        refuse with 404 (not 403) so it doesn't leak the patient's
        existence to enumeration probes.
        """
        db, (sid_a, _pid_a), (sid_b, pid_b) = two_fascicoli
        commit_b, _ = await _commit_note(db, pid=pid_b, sid=sid_b, body="patient B confidential")
        user_a = User(
            subject_id=sid_a,
            email=f"a-{sid_a}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user_a)
        await db.commit()

        client = await _client_for(db, user_a)
        try:
            r = await client.get(f"/api/patients/{pid_b}/at/{commit_b.hex()}")
            assert r.status_code == 404
            assert "confidential" not in r.text
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_at_commit_400_on_malformed_hex(self, fascicolo_with_user) -> None:
        db, owner, patient = fascicolo_with_user
        client = await _client_for(db, owner)
        try:
            r = await client.get(f"/api/patients/{patient.id}/at/not-hex-at-all")
            assert r.status_code == 400
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_history_404_for_unauthorized_patient(self, two_fascicoli) -> None:
        db, (sid_a, _pid_a), (sid_b, pid_b) = two_fascicoli
        await _commit_note(db, pid=pid_b, sid=sid_b, body="b-secret")
        user_a = User(
            subject_id=sid_a,
            email=f"a-{sid_a}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user_a)
        await db.commit()
        client = await _client_for(db, user_a)
        try:
            r = await client.get(f"/api/patients/{pid_b}/history")
            assert r.status_code == 404
            assert "b-secret" not in r.text
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_diff_404_when_commits_belong_to_other_patient(self, two_fascicoli) -> None:
        """Diff endpoint cross-checks BOTH commits belong to the URL
        patient. Asking for /diff between two B commits via patient A's
        URL must 404 (not silently return an empty diff)."""
        db, (sid_a, pid_a), (sid_b, pid_b) = two_fascicoli
        c1, _ = await _commit_note(db, pid=pid_b, sid=sid_b, body="v1")
        c2, _ = await _commit_note(db, pid=pid_b, sid=sid_b, body="v2")
        user_a = User(
            subject_id=sid_a,
            email=f"a-{sid_a}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user_a)
        await db.commit()
        client = await _client_for(db, user_a)
        try:
            r = await client.get(
                f"/api/patients/{pid_a}/diff",
                params={"from": c1.hex(), "to": c2.hex()},
            )
            assert r.status_code == 404
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_diff_404_when_only_one_commit_is_foreign(self, two_fascicoli) -> None:
        """Mixing one's own commit with another patient's must still 404.

        Without this check the response would leak whether the foreign
        commit exists (added/removed entries would point at it).
        """
        db, (sid_a, pid_a), (sid_b, pid_b) = two_fascicoli
        c_a, _ = await _commit_note(db, pid=pid_a, sid=sid_a, body="a")
        c_b, _ = await _commit_note(db, pid=pid_b, sid=sid_b, body="b")
        user_a = User(
            subject_id=sid_a,
            email=f"a-{sid_a}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user_a)
        await db.commit()
        client = await _client_for(db, user_a)
        try:
            r = await client.get(
                f"/api/patients/{pid_a}/diff",
                params={"from": c_a.hex(), "to": c_b.hex()},
            )
            assert r.status_code == 404
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_ref_log_404_for_unauthorized_patient(self, two_fascicoli) -> None:
        db, (sid_a, _pid_a), (sid_b, pid_b) = two_fascicoli
        await _commit_note(db, pid=pid_b, sid=sid_b, body="b-only")
        user_a = User(
            subject_id=sid_a,
            email=f"a-{sid_a}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user_a)
        await db.commit()
        client = await _client_for(db, user_a)
        try:
            r = await client.get(f"/api/patients/{pid_b}/ref-log")
            assert r.status_code == 404
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_404_response_shape_does_not_leak_existence(self, two_fascicoli) -> None:
        """Two probes that both result in 404 must produce the SAME
        response body so an attacker cannot distinguish "you don't
        have access" from "the resource doesn't exist".
        """
        db, (sid_a, pid_a), (sid_b, pid_b) = two_fascicoli
        commit_b, _ = await _commit_note(db, pid=pid_b, sid=sid_b, body="b")
        bogus_commit = bytes(32)  # all zeros, valid hex but never exists
        user_a = User(
            subject_id=sid_a,
            email=f"a-{sid_a}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user_a)
        await db.commit()
        client = await _client_for(db, user_a)
        try:
            # Probe 1: /at on patient A with B's real commit.
            r_real = await client.get(f"/api/patients/{pid_a}/at/{commit_b.hex()}")
            # Probe 2: /at on patient A with a non-existent commit.
            r_bogus = await client.get(f"/api/patients/{pid_a}/at/{bogus_commit.hex()}")
            assert r_real.status_code == r_bogus.status_code == 404
            # Same detail: "commit not found for this patient". The RFC
            # 7807 problem-details middleware adds an ``instance`` field
            # equal to the REQUESTED path — it necessarily differs between
            # the two probes and carries zero information the attacker did
            # not already supply, so it is excluded from the comparison.
            body_real = r_real.json()
            body_bogus = r_bogus.json()
            body_real.pop("instance", None)
            body_bogus.pop("instance", None)
            assert body_real == body_bogus
        finally:
            await client.aclose()
            app.dependency_overrides.clear()


# ===========================================================================
# 2. Tombstone + delta-chain integrity
# ===========================================================================


class TestTombstoneAndDeltaIntegrity:
    """Two contracts:
    * a tombstoned entity_object must surface as ``{"_tombstoned": true}``
      from BOTH ``read_object`` and ``read_at_commit``.
    * the pack worker may convert rows to ``storage_kind='delta'``;
      after that, ``read_at_commit`` must still resolve them. Before
      the F12.6 fix it raised ``NotImplementedError``.
    """

    @pytest.mark.asyncio
    async def test_read_object_returns_tombstone_marker(self, two_fascicoli) -> None:
        db, (sid_a, pid_a), _ = two_fascicoli
        commit_a, _note_id = await _commit_note(db, pid=pid_a, sid=sid_a, body="will be erased")
        # Tombstone every entity_object referenced by this commit's
        # manifest entries (excluding the _tree_ blob).
        await db.execute(
            text(
                "UPDATE entity_objects SET payload = '{}'::jsonb, "
                "  is_tombstoned = true, tombstoned_at = now(), "
                "  tombstoned_reason = 'test.tombstone' "
                "WHERE object_hash IN ("
                "  SELECT object_hash FROM manifest_entries "
                "  WHERE commit_hash = :c AND entity_kind = 'clinical_note'"
                ")"
            ),
            {"c": commit_a},
        )
        await db.commit()

        # Look up the note's object_hash and call read_object.
        oh = (
            await db.execute(
                text(
                    "SELECT object_hash FROM manifest_entries "
                    "WHERE commit_hash = :c AND entity_kind = 'clinical_note'"
                ),
                {"c": commit_a},
            )
        ).scalar_one()
        out = await read_object(db, oh)
        assert out == {"_tombstoned": True}

    @pytest.mark.asyncio
    async def test_read_at_commit_returns_tombstone_marker_inline(self, two_fascicoli) -> None:
        db, (sid_a, pid_a), _ = two_fascicoli
        commit_a, _ = await _commit_note(db, pid=pid_a, sid=sid_a, body="will be erased")
        await db.execute(
            text(
                "UPDATE entity_objects SET payload = '{}'::jsonb, "
                "  is_tombstoned = true, tombstoned_at = now() "
                "WHERE object_hash IN ("
                "  SELECT object_hash FROM manifest_entries "
                "  WHERE commit_hash = :c AND entity_kind = 'clinical_note'"
                ")"
            ),
            {"c": commit_a},
        )
        await db.commit()
        state = await read_at_commit(db, commit_hash=commit_a, entity_kind="clinical_note")
        assert len(state) == 1
        ((_, payload),) = state.items()
        assert payload == {"_tombstoned": True}

    @pytest.mark.asyncio
    async def test_read_at_commit_resolves_packed_delta_objects(self, two_fascicoli) -> None:
        """Reproduce the F12.6 read-after-pack bug.

        Write 11 versions of the same note (so the pack worker has
        something to compress). After packing, ``read_at_commit`` on an
        intermediate commit must return the original payload, not raise
        ``NotImplementedError``.
        """
        db, (sid_a, pid_a), _ = two_fascicoli
        note_id = uuid.uuid4()
        commit_hashes: list[bytes] = []
        for i in range(11):
            payload = {
                "id": str(note_id),
                "body": "baseline body content " * 30 + f" v{i}",
            }
            res = await commit_change(
                db,
                patient_id=pid_a,
                branch_ref="main",
                actor=ActorContext(subject_id=sid_a, kind="human"),
                message=f"v{i}",
                changes=[
                    EntityChange(
                        entity_kind="clinical_note",
                        entity_id=note_id,
                        payload=payload,
                    )
                ],
            )
            await db.commit()
            commit_hashes.append(res.commit_hash)

        converted = await pack_entity_objects(
            db,
            entity_kind="clinical_note",
            entity_id=note_id,
            snapshot_every=10,
            delta_threshold=0.95,  # generous so most rows pack
        )
        await db.commit()
        assert converted >= 1, "expected pack worker to delta-encode at least one version"

        # Read every prior commit. Before the fix, intermediate ones
        # whose entity_objects became 'delta' raise NotImplementedError.
        for i, ch in enumerate(commit_hashes):
            state = await read_at_commit(db, commit_hash=ch, entity_kind="clinical_note")
            assert len(state) == 1, f"missing note at v{i}"
            ((_, payload),) = state.items()
            assert payload.get("body", "").endswith(f"v{i}"), (
                f"version {i} returned wrong payload: {payload}"
            )


# ===========================================================================
# 3. GDPR erasure scrubs entity_objects content
# ===========================================================================


class TestErasureScrubsVersioningContent:
    """When a self-user requests GDPR erasure, the textual payloads in
    ``entity_objects`` referenced by manifests of that user's patients
    must be tombstoned. Otherwise the historical clinical_note bodies
    remain readable via ``read_object`` / ``read_at_commit``.

    Critically: only entity_objects exclusively referenced by the
    erased user's patients are tombstoned — content-addressed dedup
    means the same object_hash may be shared with patients owned by
    other users (e.g. identical short notes), and we must not delete
    those. The erasure service uses ``EXCEPT`` on the cross-reference
    set to enforce this.
    """

    @pytest.mark.asyncio
    async def test_erasure_tombstones_self_patient_entity_objects(self, two_fascicoli) -> None:
        db, (sid_a, pid_a), (_sid_b, _pid_b) = two_fascicoli
        # Make patient A a "self-user" patient: bind it to subject A.
        # Subject A also has a User row to satisfy execute_erasure.
        user_a = User(
            subject_id=sid_a,
            email=f"a-{sid_a}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user_a)
        await db.flush()
        await db.execute(
            text("UPDATE patients SET self_user_subject_id = :s WHERE id = :p"),
            {"s": sid_a, "p": pid_a},
        )
        await db.commit()

        commit_a, _ = await _commit_note(
            db, pid=pid_a, sid=sid_a, body="confidential PHI for user A"
        )

        # Erase user A.
        req = DataErasureRequest(
            id=uuid.uuid4(),
            user_subject_id=sid_a,
            scope="self",
            requested_at=datetime.now(UTC),
            status="approved",
        )
        db.add(req)
        await db.flush()
        await execute_erasure(db, request=req)
        await db.commit()

        # Lookup the note's object_hash for that commit and verify
        # the entity_object is tombstoned + payload scrubbed.
        oh = (
            await db.execute(
                text(
                    "SELECT object_hash FROM manifest_entries "
                    "WHERE commit_hash = :c AND entity_kind = 'clinical_note'"
                ),
                {"c": commit_a},
            )
        ).scalar_one()
        row = (
            await db.execute(
                text(
                    "SELECT is_tombstoned, payload, tombstoned_reason "
                    "FROM entity_objects WHERE object_hash = :h"
                ),
                {"h": oh},
            )
        ).first()
        assert row is not None
        is_tombstoned, payload, reason = row
        assert is_tombstoned is True, "erasure must tombstone entity_objects of erased self-patient"
        # Payload must NOT contain the PHI string anymore.
        assert payload in (None, {}), payload
        assert reason and "gdpr.erasure" in reason

    @pytest.mark.asyncio
    async def test_erasure_does_not_tombstone_objects_shared_with_other_patients(
        self, two_fascicoli
    ) -> None:
        """If user A and user B both happened to write the same canonical
        clinical_note body, the entity_object is content-addressed and
        shared. Erasing A must NOT tombstone the object: B still relies
        on it for their own fascicolo."""
        db, (sid_a, pid_a), (sid_b, pid_b) = two_fascicoli
        user_a = User(
            subject_id=sid_a,
            email=f"a-{sid_a}@example.com",
            password_hash=None,
            is_admin=False,
        )
        db.add(user_a)
        await db.flush()
        await db.execute(
            text("UPDATE patients SET self_user_subject_id = :s WHERE id = :p"),
            {"s": sid_a, "p": pid_a},
        )
        await db.commit()

        # Same canonical payload on both patients (same body, target_kind='patient').
        # NOTE: payload includes patient_id, target_id, author_subject_id.
        # To make hashes collide we need an identical payload — write a
        # minimal payload without patient/author fields so both notes
        # canonicalise to the same bytes.
        shared_id = uuid.uuid4()
        shared_payload = {"id": str(shared_id), "body": "shared body"}
        ra = await commit_change(
            db,
            patient_id=pid_a,
            branch_ref="main",
            actor=ActorContext(subject_id=sid_a, kind="human"),
            message="A note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=shared_id,
                    payload=shared_payload,
                )
            ],
        )
        await db.commit()
        rb = await commit_change(
            db,
            patient_id=pid_b,
            branch_ref="main",
            actor=ActorContext(subject_id=sid_b, kind="human"),
            message="B note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=shared_id,
                    payload=shared_payload,
                )
            ],
        )
        await db.commit()
        assert (
            ra.entity_object_hashes[("clinical_note", shared_id)]
            == (rb.entity_object_hashes[("clinical_note", shared_id)])
        )

        # Erase user A.
        req = DataErasureRequest(
            id=uuid.uuid4(),
            user_subject_id=sid_a,
            scope="self",
            requested_at=datetime.now(UTC),
            status="approved",
        )
        db.add(req)
        await db.flush()
        await execute_erasure(db, request=req)
        await db.commit()

        # The shared entity_object must remain non-tombstoned.
        shared_hash = ra.entity_object_hashes[("clinical_note", shared_id)]
        row = (
            await db.execute(
                text("SELECT is_tombstoned, payload FROM entity_objects WHERE object_hash = :h"),
                {"h": shared_hash},
            )
        ).first()
        assert row is not None
        is_tombstoned, payload = row
        assert is_tombstoned is False
        assert payload == shared_payload


# ===========================================================================
# 4. Publish: source patient never mutated, no cross-references
# ===========================================================================


class TestPublishIsolation:
    """Cloning a private fascicolo to OpenData must not mutate the source
    patient row, must not move the source's main ref, and must not embed
    the source patient_id (or any identifying field) in the new public
    fascicolo's payload."""

    @pytest.mark.asyncio
    async def test_publish_does_not_mutate_source_patient(self, two_fascicoli) -> None:
        db, (sid_a, pid_a), _ = two_fascicoli
        await _commit_note(db, pid=pid_a, sid=sid_a, body="some PHI Mario Rossi 333.123.4567")
        # Snapshot source state before publish.
        src_before = (await db.execute(select(Patient).where(Patient.id == pid_a))).scalar_one()
        src_main_before = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": pid_a},
            )
        ).scalar_one()
        # Capture mutable demographics for comparison (identifiers live
        # in the external_identifiers JSONB since v3).
        snap_before = (
            src_before.display_name,
            src_before.email,
            src_before.phone,
            list(src_before.external_identifiers or []),
            src_before.notes,
        )

        result = await publish_patient_to_opendata(
            db,
            source_patient=src_before,
            actor=ActorContext(subject_id=sid_a, kind="human"),
            pseudonym="OpenData Test",
        )
        await db.commit()

        # Source patient row must be unchanged.
        src_after = (await db.execute(select(Patient).where(Patient.id == pid_a))).scalar_one()
        snap_after = (
            src_after.display_name,
            src_after.email,
            src_after.phone,
            list(src_after.external_identifiers or []),
            src_after.notes,
        )
        assert snap_after == snap_before
        # main ref unchanged.
        src_main_after = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": pid_a},
            )
        ).scalar_one()
        assert src_main_after == src_main_before

        # Cleanup the public clone (it lives in patients table).
        await db.execute(
            text("DELETE FROM patients WHERE id = :p"),
            {"p": result.public_patient_id},
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_publish_clone_does_not_reference_source_patient_id(self, two_fascicoli) -> None:
        """The public clone's payloads must not embed the source
        patient_id anywhere (it would re-link the clone to the
        original individual)."""
        db, (sid_a, pid_a), _ = two_fascicoli
        await _commit_note(db, pid=pid_a, sid=sid_a, body="Clinical body, no PHI tokens here.")
        src = (await db.execute(select(Patient).where(Patient.id == pid_a))).scalar_one()
        result = await publish_patient_to_opendata(
            db,
            source_patient=src,
            actor=ActorContext(subject_id=sid_a, kind="human"),
            pseudonym="OpenData Iso",
        )
        await db.commit()

        # Walk the clone's main commit manifest; no payload may contain
        # the source patient_id as a substring.
        rows = (
            await db.execute(
                text(
                    "SELECT eo.payload "
                    "FROM manifest_entries me "
                    "JOIN entity_objects eo "
                    "  ON eo.object_hash = me.object_hash "
                    "WHERE me.commit_hash = :c"
                ),
                {"c": result.public_main_commit},
            )
        ).all()
        src_pid_str = str(pid_a)
        for (payload,) in rows:
            if payload is None:
                continue
            assert src_pid_str not in str(payload), (
                f"source patient_id leaked into clone payload: {payload}"
            )
        # Cleanup.
        await db.execute(
            text("DELETE FROM patients WHERE id = :p"),
            {"p": result.public_patient_id},
        )
        await db.commit()


# ===========================================================================
# 5. RLS structural enforcement on audit-critical tables
# ===========================================================================


class TestRlsStructuralEnforcement:
    """The migration ``0009_rls_policies`` declares row-level security
    policies on ownership tables, plus 0035 adds policies for the F12
    versioning tables. Postgres bypasses RLS for the table OWNER unless
    ``FORCE ROW LEVEL SECURITY`` is enabled. If the production deploy
    uses the same DB role as the table owner, every policy is decorative
    and a compromised query path can still leak data.

    This test pins the invariant: every sensitive table must either be
    FORCE-protected, or owned by a role distinct from the connected
    role. If both fail, the deploy must move the app to a non-owner
    role (recommended) or add ``ALTER TABLE ... FORCE ROW LEVEL SECURITY``
    to the migration.
    """

    @pytest.mark.xfail(
        reason=(
            "Production hardening required: dev DB has the app role as "
            "table owner, so RLS is bypassed. Either FORCE RLS in a new "
            "migration (with care for migration-time writes) or use a "
            "non-owner app role in production."
        ),
        strict=False,
    )
    @pytest.mark.asyncio
    async def test_audit_tables_protected_against_owner_bypass(self, two_fascicoli) -> None:
        db, *_ = two_fascicoli
        sensitive = [
            "patients",
            "studies",
            "reports",
            "annotations",
            "grants",
            "refs",
            "ref_log",
            "commits",
            "manifest_entries",
            "entity_objects",
            "proposals",
            "merge_conflicts",
            "audit_log",
        ]
        rows = (
            await db.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity, "
                    "  (SELECT rolname FROM pg_roles WHERE oid = relowner) "
                    "FROM pg_class "
                    "WHERE relkind = 'r' AND relname = ANY(:t)"
                ),
                {"t": sensitive},
            )
        ).all()
        current_role = (await db.execute(text("SELECT current_user"))).scalar_one()

        weak: list[str] = []
        for name, rls_on, forced, owner in rows:
            if not rls_on:
                weak.append(f"{name}: RLS disabled")
            elif not forced and owner == current_role:
                weak.append(f"{name}: owner == app role ({current_role}) and FORCE RLS is off")
        assert not weak, (
            "RLS bypass exposure on sensitive tables: "
            + "; ".join(weak)
            + ". Fix: enable FORCE ROW LEVEL SECURITY in a new migration, "
            "OR ensure production runs the app as a non-owner role."
        )
