"""F10.3: DUC service unit tests.

The service uses ``db.execute`` for every read and ``db.flush`` on
writes; the ORM attributes are plain Python fields on the mapped
classes once we pass a constructed object in. We exercise the
quorum rule and the state-transition edges, stubbing the DB so the
tests don't need Postgres."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from bvphoenix.db.models import (
    DUCMember,
    DUCRequest,
    DUCVote,
    TrainingLicense,
)
from bvphoenix.services import duc as duc_service

# ---- Stub session --------------------------------------------------------


class _Result:
    """Shim that mimics the result chain the service uses:
    ``(await db.execute(...)).scalar_one_or_none()`` /
    ``.scalars().all()``."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        return self._rows[0]

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _Session:
    """Hands back configured results in order. Writes are captured
    on ``added`` + ``updates`` for inspection."""

    def __init__(self, responses: list[list[Any]]) -> None:
        self._responses = list(responses)
        self.added: list[Any] = []
        self.updates: list[Any] = []
        self.flushed = 0

    async def execute(self, stmt: Any, *_args: Any, **_kwargs: Any) -> _Result:
        s = str(stmt)
        if "UPDATE" in s.upper():
            self.updates.append(stmt)
            return _Result([])
        if not self._responses:
            return _Result([])
        return _Result(self._responses.pop(0))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


def _member(*, role: str = "member", active: bool = True) -> DUCMember:
    m = DUCMember(user_subject_id=uuid.uuid4(), role=role)
    m.id = uuid.uuid4()
    if not active:
        import datetime as _dt

        m.revoked_at = _dt.datetime.now(_dt.UTC)
    return m


# ---- Quorum --------------------------------------------------------------


def test_quorum_is_strict_majority() -> None:
    assert duc_service._quorum(0) == 1
    assert duc_service._quorum(1) == 1  # at least one vote
    assert duc_service._quorum(2) == 2
    assert duc_service._quorum(3) == 2
    assert duc_service._quorum(4) == 3
    assert duc_service._quorum(5) == 3
    assert duc_service._quorum(6) == 4


# ---- submit_request ------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_request_happy_path() -> None:
    lic = TrainingLicense(
        licensee_name="Acme",
        licensee_email="buy@acme.example",
        price_usd_cents=100_000,
    )
    lic.id = uuid.uuid4()
    lic.status = "draft"

    # responses in order: license lookup, open-request lookup (none)
    db = _Session(responses=[[lic], []])
    req = await duc_service.submit_request(
        db,
        license_id=lic.id,
        submitted_by=uuid.uuid4(),
        summary="Clinical CT dataset for pneumonia model — 500 studies, T3 opt-in.",
    )
    assert isinstance(req, DUCRequest)
    assert lic.status == "pending_duc"
    assert lic.duc_request_id == req.id
    assert req in db.added


@pytest.mark.asyncio
async def test_submit_request_refuses_duplicate() -> None:
    lic = TrainingLicense(
        licensee_name="Acme",
        licensee_email="buy@acme.example",
        price_usd_cents=100_000,
    )
    lic.id = uuid.uuid4()
    existing = DUCRequest(license_id=lic.id, status="pending", summary="prior review")
    # license lookup, existing open request
    db = _Session(responses=[[lic], [existing]])
    with pytest.raises(duc_service.DUCError):
        await duc_service.submit_request(
            db,
            license_id=lic.id,
            submitted_by=None,
            summary="second submission should not open",
        )


@pytest.mark.asyncio
async def test_submit_request_rejects_unknown_license() -> None:
    db = _Session(responses=[[]])  # no license
    with pytest.raises(duc_service.DUCError):
        await duc_service.submit_request(
            db,
            license_id=uuid.uuid4(),
            submitted_by=None,
            summary="licence does not exist",
        )


# ---- record_vote + try_close --------------------------------------------


@pytest.mark.asyncio
async def test_record_vote_noquorum_leaves_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = DUCRequest(
        license_id=uuid.uuid4(),
        status="pending",
        summary="review",
    )
    request.id = uuid.uuid4()
    voter = _member()

    # response order: request lookup, member lookup, existing vote (none).
    # try_close is patched out; its own tests below cover its logic.
    db = _Session(responses=[[request], [voter], []])

    async def fake_try_close(_db: Any, _req: Any) -> str:
        return "pending"

    monkeypatch.setattr(duc_service, "try_close", fake_try_close)
    vote = await duc_service.record_vote(
        db,
        request_id=request.id,
        member_id=voter.id,
        decision="approve",
        rationale="LGTM",
    )

    assert isinstance(vote, DUCVote)
    assert vote.decision == "approve"
    assert vote in db.added


@pytest.mark.asyncio
async def test_record_vote_revises_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = DUCRequest(license_id=uuid.uuid4(), status="pending", summary="review")
    request.id = uuid.uuid4()
    voter = _member()
    existing = DUCVote(
        request_id=request.id,
        member_id=voter.id,
        decision="approve",
        rationale="first",
    )
    db = _Session(responses=[[request], [voter], [existing]])

    async def fake_try_close(_db: Any, _req: Any) -> str:
        return "pending"

    monkeypatch.setattr(duc_service, "try_close", fake_try_close)
    vote = await duc_service.record_vote(
        db,
        request_id=request.id,
        member_id=voter.id,
        decision="reject",
        rationale="changed my mind",
    )

    assert vote is existing  # same row
    assert existing.decision == "reject"
    assert existing.rationale == "changed my mind"
    assert db.added == []  # no new row created


@pytest.mark.asyncio
async def test_record_vote_rejects_unknown_decision() -> None:
    db = _Session(responses=[])
    with pytest.raises(ValueError):
        await duc_service.record_vote(
            db,
            request_id=uuid.uuid4(),
            member_id=uuid.uuid4(),
            decision="strongly agree",
            rationale=None,
        )


@pytest.mark.asyncio
async def test_record_vote_rejects_closed_request() -> None:
    request = DUCRequest(license_id=uuid.uuid4(), status="approved", summary="already done")
    request.id = uuid.uuid4()
    db = _Session(responses=[[request]])
    with pytest.raises(duc_service.DUCError):
        await duc_service.record_vote(
            db,
            request_id=request.id,
            member_id=uuid.uuid4(),
            decision="approve",
            rationale=None,
        )


@pytest.mark.asyncio
async def test_try_close_approves_when_quorum_reached() -> None:
    request = DUCRequest(license_id=uuid.uuid4(), status="pending", summary="r")
    request.id = uuid.uuid4()
    voters = [_member() for _ in range(3)]  # quorum = 2
    approves = [
        DUCVote(
            request_id=request.id,
            member_id=voters[0].id,
            decision="approve",
            rationale=None,
        ),
        DUCVote(
            request_id=request.id,
            member_id=voters[1].id,
            decision="approve",
            rationale=None,
        ),
    ]
    # active members, votes on request
    db = _Session(responses=[voters, approves])
    status = await duc_service.try_close(db, request)
    assert status == "approved"
    assert request.status == "approved"
    assert request.closed_at is not None
    # The UPDATE on training_licenses was issued.
    assert len(db.updates) == 1


@pytest.mark.asyncio
async def test_try_close_rejects_when_quorum_rejects() -> None:
    request = DUCRequest(license_id=uuid.uuid4(), status="pending", summary="r")
    request.id = uuid.uuid4()
    voters = [_member() for _ in range(3)]  # quorum = 2
    rejects = [
        DUCVote(
            request_id=request.id,
            member_id=voters[0].id,
            decision="reject",
            rationale=None,
        ),
        DUCVote(
            request_id=request.id,
            member_id=voters[1].id,
            decision="reject",
            rationale=None,
        ),
    ]
    db = _Session(responses=[voters, rejects])
    status = await duc_service.try_close(db, request)
    assert status == "rejected"
    assert request.status == "rejected"


@pytest.mark.asyncio
async def test_try_close_keeps_pending_when_split() -> None:
    request = DUCRequest(license_id=uuid.uuid4(), status="pending", summary="r")
    request.id = uuid.uuid4()
    voters = [_member() for _ in range(5)]  # quorum = 3
    votes = [
        DUCVote(
            request_id=request.id,
            member_id=voters[0].id,
            decision="approve",
            rationale=None,
        ),
        DUCVote(
            request_id=request.id,
            member_id=voters[1].id,
            decision="reject",
            rationale=None,
        ),
    ]
    db = _Session(responses=[voters, votes])
    status = await duc_service.try_close(db, request)
    assert status == "pending"
    assert request.status == "pending"
    assert request.closed_at is None
