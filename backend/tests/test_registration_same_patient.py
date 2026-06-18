"""Security: a registration may not span two patients.

Cross-STUDY registration of one patient's baseline vs follow-up is the
intended use; registering two DIFFERENT patients' series is forbidden — a
cross-patient spatial transform is exactly where a measurement could be
mis-attributed. The guard lives in ``api/studies/registrations.py`` and
rejects with 422 before any registration row is created.
"""

from __future__ import annotations

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


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


async def test_cross_patient_registration_rejected(db_session, make_user, make_study) -> None:
    owner = await make_user()
    # Two studies under two DIFFERENT patients (both managed by owner, so
    # READ_PIXELS passes on both — the rejection is the cross-patient guard,
    # not an authz failure).
    _study_a, series_a = await make_study(owner, description="patient A CT")
    _study_b, series_b = await make_study(owner, description="patient B CT")
    assert _study_a.patient_id != _study_b.patient_id

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
        assert r.status_code == 422, r.text
        assert "cross" in r.text.lower() or "different patients" in r.text.lower()
    finally:
        app.dependency_overrides.clear()
