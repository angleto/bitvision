"""Integration tests for the public iCal subscription feed (v3.6).

Invariants pinned (each failure is a real security/behaviour bug):

* mint requires auth + Idempotency-Key; the same key replays.
* the feed URL is fetchable with NO auth and returns the patient's
  calendar as RFC 5545.
* revoke kills the URL immediately (404 thereafter).
* a tampered / random / cross-patient-forged token is 404 — never a
  different patient's calendar (cross-patient inexpressible).
* RBAC: a user with no access to the patient gets 404 on mint/list.
* agent tokens are gated by ``calendar:subscribe`` AND the per-patient
  grant; ``calendar:read`` alone cannot mint.

Requires Postgres with migration 0003 applied (skip_if_no_db).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bvphoenix.auth import optional_user, require_user
from bvphoenix.auth.tokens import issue_agent_token
from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    AgentAssistant,
    AgentAssistantPatient,
    AgentToken,
    CalendarSubscription,
    ClinicalEvent,
    Patient,
    User,
)
from bvphoenix.db.models.principals import Subject
from bvphoenix.db.session import SERVICE_SUBJECT, get_db, set_current_subject
from bvphoenix.main import app
from bvphoenix.services.calendar_subscription_token import sign as sign_token

from .conftest import skip_if_no_db

pytestmark = skip_if_no_db

_EVENT_TITLE = "Visita oncologica subscription-test"


@pytest_asyncio.fixture
async def two_fascicoli() -> AsyncIterator[
    tuple[AsyncSession, tuple[User, Patient], tuple[User, Patient]]
]:
    """Two independent (owner_user, patient) pairs, each patient with one
    clinical event so the feed has a VEVENT to render."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    made: list[tuple[uuid.UUID, uuid.UUID]] = []
    try:
        await set_current_subject(db, SERVICE_SUBJECT)

        async def _pair(tag: str) -> tuple[User, Patient]:
            sid, pid = uuid.uuid4(), uuid.uuid4()
            db.add(Subject(id=sid, kind="user", display_name=f"calsub-{tag}-{sid}"))
            await db.flush()
            user = User(subject_id=sid, email=f"calsub-{tag}-{sid}@example.com")
            db.add(user)
            await db.flush()
            patient = Patient(id=pid, managed_by_subject_id=sid, display_name=f"Patient {tag}")
            db.add(patient)
            await db.flush()
            db.add(
                ClinicalEvent(
                    patient_id=pid,
                    kind="outpatient_visit",
                    title=_EVENT_TITLE,
                    event_date=date.today(),
                )
            )
            made.append((sid, pid))
            return user, patient

        a = await _pair("A")
        b = await _pair("B")
        await db.commit()
        yield db, a, b
    finally:
        try:
            await db.rollback()
            await set_current_subject(db, SERVICE_SUBJECT)
            for _sid, pid in made:
                await db.execute(text("DELETE FROM patients WHERE id = :p"), {"p": pid})
            for sid, _pid in made:
                await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": sid})
            await db.commit()
        finally:
            await db.close()
            await engine.dispose()


def _override_db(session: AsyncSession):
    async def _dep():
        yield session

    return _dep


def _override_user(user: User | None):
    async def _dep():
        return user

    return _dep


def _authed_client(session: AsyncSession, user: User) -> AsyncClient:
    app.dependency_overrides[get_db] = _override_db(session)
    app.dependency_overrides[require_user] = _override_user(user)
    app.dependency_overrides[optional_user] = _override_user(user)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _anon_client(session: AsyncSession) -> AsyncClient:
    """Only the DB is overridden — no auth override, no Authorization
    header: this is exactly how an external calendar app hits us."""
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = _override_db(session)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


@skip_if_no_db
async def test_mint_then_public_fetch_then_revoke(two_fascicoli) -> None:
    db, (user_a, patient_a), _ = two_fascicoli
    base = f"/api/patients/{patient_a.id}/calendar/subscriptions"
    try:
        client = _authed_client(db, user_a)
        async with client:
            r = await client.post(base, json={"label": "Sorella"}, headers=_idem())
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["patient_id"] == str(patient_a.id)
            assert body["author_kind"] == "human"
            assert body["revoked_at"] is None
            feed_path = body["feed_path"]
            assert feed_path.startswith("/api/calendar/feed/")
            assert feed_path.endswith(".ics")
            assert body["feed_url"].endswith(feed_path)
            sub_id = body["id"]

            lst = await client.get(base)
            assert lst.status_code == 200
            assert [s["id"] for s in lst.json()] == [sub_id]
        app.dependency_overrides.clear()

        # Anonymous fetch — the actual subscription path.
        anon = _anon_client(db)
        async with anon:
            feed = await anon.get(feed_path)
            assert feed.status_code == 200, feed.text
            assert feed.headers["content-type"].startswith("text/calendar")
            assert "BEGIN:VCALENDAR" in feed.text
            assert _EVENT_TITLE in feed.text
        app.dependency_overrides.clear()

        # Revoke, then the same URL is dead.
        client = _authed_client(db, user_a)
        async with client:
            rev = await client.delete(f"{base}/{sub_id}")
            assert rev.status_code == 204
        app.dependency_overrides.clear()

        anon = _anon_client(db)
        async with anon:
            assert (await anon.get(feed_path)).status_code == 404
            # revoking again is idempotent
        app.dependency_overrides.clear()
        client = _authed_client(db, user_a)
        async with client:
            assert (await client.delete(f"{base}/{sub_id}")).status_code == 204
    finally:
        app.dependency_overrides.clear()


@skip_if_no_db
async def test_tampered_random_and_cross_patient_tokens_are_404(two_fascicoli) -> None:
    db, (user_a, patient_a), (_user_b, patient_b) = two_fascicoli
    try:
        client = _authed_client(db, user_a)
        async with client:
            r = await client.post(
                f"/api/patients/{patient_a.id}/calendar/subscriptions",
                json={},
                headers=_idem(),
            )
            assert r.status_code == 201
            sub_id = uuid.UUID(r.json()["id"])
            good = r.json()["feed_path"].rsplit("/", 1)[1][: -len(".ics")]
        app.dependency_overrides.clear()

        anon = _anon_client(db)
        async with anon:
            # tampered
            bad = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
            assert (await anon.get(f"/api/calendar/feed/{bad}.ics")).status_code == 404
            # random
            assert (await anon.get(f"/api/calendar/feed/{'z' * 80}.ics")).status_code == 404
            # cross-patient forge: a VALID signature (our key) but the
            # signed patient is B while the row belongs to A. The row's
            # patient_id != signed patient_id -> 404, never B's calendar.
            forged = sign_token(sub_id, patient_b.id)
            resp = await anon.get(f"/api/calendar/feed/{forged}.ics")
            assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


@skip_if_no_db
async def test_rbac_user_without_access_cannot_mint_or_list(two_fascicoli) -> None:
    db, (user_a, _patient_a), (_user_b, patient_b) = two_fascicoli
    try:
        # user_a has no permission over patient_b -> 404 (not 403, so
        # the fascicolo's existence is not leaked).
        client = _authed_client(db, user_a)
        async with client:
            base = f"/api/patients/{patient_b.id}/calendar/subscriptions"
            assert (await client.post(base, json={}, headers=_idem())).status_code == 404
            assert (await client.get(base)).status_code == 404
    finally:
        app.dependency_overrides.clear()


@skip_if_no_db
async def test_idempotency_required_and_replayed(two_fascicoli) -> None:
    db, (user_a, patient_a), _ = two_fascicoli
    base = f"/api/patients/{patient_a.id}/calendar/subscriptions"
    try:
        client = _authed_client(db, user_a)
        async with client:
            # missing key -> 400 (require_idempotency_key)
            assert (await client.post(base, json={})).status_code == 400
            # same key replays the same row, not a second URL
            key = _idem()
            r1 = await client.post(base, json={"label": "x"}, headers=key)
            r2 = await client.post(base, json={"label": "x"}, headers=key)
            assert r1.status_code == 201
            assert r2.json()["id"] == r1.json()["id"]
            lst = await client.get(base)
            assert len(lst.json()) == 1
    finally:
        app.dependency_overrides.clear()


@skip_if_no_db
async def test_dry_run_persists_nothing(two_fascicoli) -> None:
    db, (user_a, patient_a), _ = two_fascicoli
    base = f"/api/patients/{patient_a.id}/calendar/subscriptions"
    try:
        client = _authed_client(db, user_a)
        async with client:
            r = await client.post(
                f"{base}?dry_run=true", json={"label": "preview"}, headers=_idem()
            )
            assert r.status_code == 200
            assert r.json()["patient_id"] == str(patient_a.id)
            assert (await client.get(base)).json() == []
        # nothing in the DB either
        rows = (
            (
                await db.execute(
                    select(CalendarSubscription).where(
                        CalendarSubscription.patient_id == patient_a.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == []
    finally:
        app.dependency_overrides.clear()


async def _mint_agent(
    db: AsyncSession,
    owner: User,
    *,
    scopes: list[str],
    patient_ids: list[uuid.UUID],
) -> str:
    assistant = AgentAssistant(
        id=uuid.uuid4(),
        owner_subject_id=owner.subject_id,
        label="calsub-agent",
        permissions=scopes,
        # client_id is NOT NULL + unique with no server default; the
        # legacy agent-JWT path we exercise does not use the
        # per-assistant secret, so a unique placeholder is enough.
        client_id=f"calsub-{uuid.uuid4().hex}",
    )
    db.add(assistant)
    await db.flush()
    for pid in patient_ids:
        db.add(
            AgentAssistantPatient(
                assistant_id=assistant.id,
                patient_id=pid,
                granted_by_subject_id=owner.subject_id,
            )
        )
    token_id = uuid.uuid4()
    jwt_raw, token_hash = issue_agent_token(
        agent_token_id=token_id,
        owner_subject_id=owner.subject_id,
        scope=scopes,
        ttl_seconds=3600,
    )
    db.add(
        AgentToken(
            id=token_id,
            assistant_id=assistant.id,
            token_hash=token_hash,
            token_tail=jwt_raw[-8:],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db.commit()
    return jwt_raw


@skip_if_no_db
async def test_agent_scope_and_patient_gate(two_fascicoli) -> None:
    db, (user_a, patient_a), (_user_b, patient_b) = two_fascicoli
    base = f"/api/patients/{patient_a.id}/calendar/subscriptions"
    try:
        # Real bearer path: only the DB is overridden so the auth deps
        # resolve the agent from our session.
        app.dependency_overrides.clear()
        app.dependency_overrides[get_db] = _override_db(db)

        # 1. calendar:read only -> cannot mint (needs calendar:subscribe)
        jwt_read = await _mint_agent(
            db, user_a, scopes=["calendar:read"], patient_ids=[patient_a.id]
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                base,
                json={},
                headers={**_idem(), "Authorization": f"Bearer {jwt_read}"},
            )
            assert r.status_code == 403, r.text
            # but it can list (calendar:read)
            assert (
                await c.get(base, headers={"Authorization": f"Bearer {jwt_read}"})
            ).status_code == 200

        # 2. calendar:subscribe but NOT granted patient_a -> patient gate 403
        jwt_nopat = await _mint_agent(
            db, user_a, scopes=["calendar:subscribe"], patient_ids=[patient_b.id]
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                base,
                json={},
                headers={**_idem(), "Authorization": f"Bearer {jwt_nopat}"},
            )
            assert r.status_code == 403, r.text

        # 3. calendar:subscribe + patient grant -> 201, author_kind=agent
        jwt_ok = await _mint_agent(
            db, user_a, scopes=["calendar:subscribe"], patient_ids=[patient_a.id]
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                base,
                json={},
                headers={**_idem(), "Authorization": f"Bearer {jwt_ok}"},
            )
            assert r.status_code == 201, r.text
            assert r.json()["author_kind"] == "agent"
    finally:
        app.dependency_overrides.clear()
