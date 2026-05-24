"""F10: signing a training license requires a DUC-approved review.
Unit tests on the service helper so the rule is pinned in isolation
from the API surface."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from bvphoenix.db.models import DUCRequest, TrainingLicense
from bvphoenix.services.training_licenses import (
    TrainingLicenseError,
    sign_license,
)


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _Session:
    """Scripted responses in call order. ``sign_license`` reads up to
    two rows (license, linked DUC request)."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.flushed = 0

    async def execute(self, _stmt: Any) -> _Result:
        row = self._responses.pop(0) if self._responses else None
        return _Result(row)

    async def flush(self) -> None:
        self.flushed += 1


def _make_license(
    *,
    status: str = "approved",
    duc_id: uuid.UUID | None = None,
) -> TrainingLicense:
    row = TrainingLicense(
        licensee_name="ACME Labs",
        licensee_email="legal@acme.test",
        price_usd_cents=500_000,
    )
    # The ORM fields are plain Python attributes at construction time,
    # so this is enough to drive the sign_license branches.
    row.id = uuid.uuid4()
    row.status = status
    row.duc_request_id = duc_id
    row.signed_at = None
    return row


def _make_duc_request(status: str = "approved") -> DUCRequest:
    req = DUCRequest(license_id=uuid.uuid4(), summary="…")
    req.id = uuid.uuid4()
    req.status = status
    return req


@pytest.mark.asyncio
async def test_happy_path_flips_to_signed_when_duc_approved() -> None:
    duc = _make_duc_request(status="approved")
    lic = _make_license(status="approved", duc_id=duc.id)
    db = _Session(responses=[lic, duc])

    out = await sign_license(db, license_id=lic.id)

    assert out is lic
    assert out.status == "signed"
    assert out.signed_at is not None
    assert db.flushed == 1


@pytest.mark.asyncio
async def test_missing_license_raises() -> None:
    db = _Session(responses=[None])
    with pytest.raises(TrainingLicenseError, match="not found"):
        await sign_license(db, license_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_refuses_when_already_signed() -> None:
    lic = _make_license(status="signed", duc_id=uuid.uuid4())
    db = _Session(responses=[lic])
    with pytest.raises(TrainingLicenseError, match="already signed"):
        await sign_license(db, license_id=lic.id)
    # Idempotent refusal: status unchanged, no flush.
    assert lic.status == "signed"
    assert db.flushed == 0


@pytest.mark.asyncio
async def test_refuses_when_revoked() -> None:
    lic = _make_license(status="revoked", duc_id=uuid.uuid4())
    db = _Session(responses=[lic])
    with pytest.raises(TrainingLicenseError, match="revoked"):
        await sign_license(db, license_id=lic.id)


@pytest.mark.asyncio
async def test_refuses_when_still_pending_duc() -> None:
    lic = _make_license(status="pending_duc", duc_id=uuid.uuid4())
    db = _Session(responses=[lic])
    with pytest.raises(TrainingLicenseError, match="DUC-approved"):
        await sign_license(db, license_id=lic.id)


@pytest.mark.asyncio
async def test_refuses_approved_license_with_no_duc_id() -> None:
    """Belt-and-suspenders: even if a hand-edit put a license into
    'approved', no DUC link means the committee did not weigh in."""
    lic = _make_license(status="approved", duc_id=None)
    db = _Session(responses=[lic])
    with pytest.raises(TrainingLicenseError, match="no linked DUC request"):
        await sign_license(db, license_id=lic.id)


@pytest.mark.asyncio
async def test_refuses_when_duc_request_is_missing() -> None:
    lic = _make_license(status="approved", duc_id=uuid.uuid4())
    db = _Session(responses=[lic, None])
    with pytest.raises(TrainingLicenseError, match="missing"):
        await sign_license(db, license_id=lic.id)


@pytest.mark.asyncio
async def test_refuses_when_duc_request_is_not_approved() -> None:
    duc = _make_duc_request(status="pending")
    lic = _make_license(status="approved", duc_id=duc.id)
    db = _Session(responses=[lic, duc])
    with pytest.raises(TrainingLicenseError, match="committee has not approved"):
        await sign_license(db, license_id=lic.id)
    # License left alone.
    assert lic.status == "approved"
    assert db.flushed == 0


@pytest.mark.asyncio
async def test_refuses_when_duc_request_was_rejected() -> None:
    duc = _make_duc_request(status="rejected")
    lic = _make_license(status="approved", duc_id=duc.id)
    db = _Session(responses=[lic, duc])
    with pytest.raises(TrainingLicenseError, match="rejected"):
        await sign_license(db, license_id=lic.id)
