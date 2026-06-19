"""``POST /api/registrations`` behaviour beyond the cross-patient guard.

Two contracts the follow-up/contrast viewers depend on:

* **Pre-pack guarantee** — the worker registers fastest from packed
  ``volume_f32`` derivatives. When a series isn't packed yet the endpoint
  must enqueue ``pack_volume`` for it (and only it) before
  ``register_series``, so the alignment hits the fast path instead of
  re-stacking raw DICOM (the slow path that blew the old FE poll window).
* **Cancel** — the viewer's Cancel button flips a queued/running
  registration to ``cancelled`` so the UI stops polling.

The arq pool is stubbed (no Redis); we assert what the endpoint *would*
enqueue, and the resulting DB state, against a real Postgres.
"""

from __future__ import annotations

import arq
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import Derivative, Registration, User
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from bvphoenix.services.volumes import DERIVATIVE_FORMAT, DERIVATIVE_KIND
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


class _FakeArqPool:
    """Records ``enqueue_job`` calls instead of talking to Redis."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, tuple]] = []

    async def enqueue_job(self, name: str, *args, **kwargs):
        self.enqueued.append((name, args))

        class _Handle:
            job_id = f"arq-{name}-{len(args)}"

        return _Handle()

    async def close(self) -> None:
        pass


def _client_as(session: AsyncSession, user: User | None) -> AsyncClient:
    async def _db():
        yield session

    async def _usr():
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _usr
    app.dependency_overrides[optional_user] = _usr
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_create_registration_prepacks_only_unpacked_series(
    db_session, make_user, make_study, monkeypatch
) -> None:
    owner = await make_user()
    # Both series under the SAME patient (cross-study baseline vs follow-up).
    study_a, series_a = await make_study(owner, description="baseline CT")
    # Reuse the minted patient so the same-patient guard passes.
    from bvphoenix.db.models import Patient

    patient = (
        await db_session.execute(select(Patient).where(Patient.id == study_a.patient_id))
    ).scalar_one()
    _study_b, series_b = await make_study(owner, patient=patient, description="follow-up CT")

    # series_a is already packed; series_b is not → only series_b is enqueued.
    db_session.add(
        Derivative(
            series_id=series_a.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="b",
            s3_key="k",
        )
    )
    await db_session.commit()

    fake = _FakeArqPool()

    async def _fake_create_pool(*_a, **_k):
        return fake

    monkeypatch.setattr(arq, "create_pool", _fake_create_pool)

    try:
        async with _client_as(db_session, owner) as client:
            r = await client.post(
                "/api/registrations",
                json={
                    "fixed_series_id": str(series_a.id),
                    "moving_series_id": str(series_b.id),
                    "kind": "rigid",
                },
            )
        assert r.status_code == 202, r.text
        names = [n for n, _ in fake.enqueued]
        # register_series is always enqueued; pack_volume only for the unpacked one.
        assert "register_series" in names
        pack_args = [args for n, args in fake.enqueued if n == "pack_volume"]
        assert pack_args == [(str(series_b.id),)], fake.enqueued
    finally:
        app.dependency_overrides.clear()


async def test_cancel_registration_marks_cancelled(
    db_session, make_user, make_study, monkeypatch
) -> None:
    owner = await make_user()
    study_a, series_a = await make_study(owner, description="baseline CT")
    from bvphoenix.db.models import Patient

    patient = (
        await db_session.execute(select(Patient).where(Patient.id == study_a.patient_id))
    ).scalar_one()
    _study_b, series_b = await make_study(owner, patient=patient, description="follow-up CT")

    fake = _FakeArqPool()

    async def _fake_create_pool(*_a, **_k):
        return fake

    monkeypatch.setattr(arq, "create_pool", _fake_create_pool)

    try:
        async with _client_as(db_session, owner) as client:
            created = await client.post(
                "/api/registrations",
                json={
                    "fixed_series_id": str(series_a.id),
                    "moving_series_id": str(series_b.id),
                    "kind": "rigid",
                },
            )
            assert created.status_code == 202, created.text
            reg_id = created.json()["id"]

            cancelled = await client.post(f"/api/registrations/{reg_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"

        row = (
            await db_session.execute(select(Registration).where(Registration.id == reg_id))
        ).scalar_one()
        assert row.status == "cancelled"
        assert row.finished_at is not None
    finally:
        app.dependency_overrides.clear()
