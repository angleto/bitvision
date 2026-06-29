"""Patient consent ledger — the append-only grant/revoke history derived
from the authoritative consent rows (no separate events table).

Proves the crux the ledger sells: the FULL history is reconstructed, not
just current state — a grant -> revoke -> grant cycle yields three events,
including the past revoked episode — and point-in-time ``as_of`` answers
"what was in effect at instant T" (GDPR Art. 7(1)). Reads the same rows
that gate cohort selection, so the ledger cannot drift from what governs
data use.

Calls the service + endpoint handler directly on the test's event loop;
TestClient's separate loop corrupts the shared ``db_session``. Rows are
flushed (not committed) so the fixture rollback cleans up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bvphoenix.api.gdpr import ConsentLedgerOut, get_consent_ledger, list_consents
from bvphoenix.db.models.gdpr import Consent
from bvphoenix.db.models.training_consents import TrainingConsent
from bvphoenix.main import app
from bvphoenix.services.consent_ledger import build_consent_ledger
from tests.conftest import skip_if_no_db

_HASH = "a" * 64  # consent_hash is String(64)

T1 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
T3 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
T_AI = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def test_route_is_registered() -> None:
    assert any("consent-ledger" in getattr(r, "path", "") for r in app.routes), (
        "consent-ledger route must be registered"
    )


async def _seed(db, owner, study) -> None:
    """grant -> revoke -> grant ``research_use`` (account) + one active
    ``ai_training``, plus a study training consent revoked then re-granted."""
    rows = [
        Consent(
            user_subject_id=owner.subject_id, kind="research_use", granted_at=T1, revoked_at=T2
        ),
        Consent(user_subject_id=owner.subject_id, kind="research_use", granted_at=T3),
        Consent(user_subject_id=owner.subject_id, kind="ai_training", granted_at=T_AI),
        TrainingConsent(
            user_subject_id=owner.subject_id,
            study_id=study.id,
            tier="t3",
            consent_version=1,
            consent_hash=_HASH,
            granted_at=T1,
            revoked_at=T2,
            revoke_reason="changed my mind",
        ),
        TrainingConsent(
            user_subject_id=owner.subject_id,
            study_id=study.id,
            tier="t3",
            consent_version=2,
            consent_hash=_HASH,
            granted_at=T3,
        ),
    ]
    for r in rows:
        db.add(r)
    await db.flush()


@skip_if_no_db
async def test_ledger_reconstructs_full_append_only_history(db_session, make_user, make_study):
    owner = await make_user()
    study, _series = await make_study(owner)
    await _seed(db_session, owner, study)

    led = await build_consent_ledger(db_session, owner.subject_id)

    # 5 rows -> 7 events: research_use (grant,revoke,grant)=3, ai_training=1,
    # study t3 (grant,revoke,grant)=3.
    assert len(led["events"]) == 7

    # The PAST revoked episode is present — proves append-only, not
    # current-state-only. This is the whole reason the ledger exists.
    research = [e for e in led["events"] if e["kind"] == "research_use"]
    assert {(e["action"], e["at"]) for e in research} == {
        ("granted", T1.isoformat()),
        ("revoked", T2.isoformat()),
        ("granted", T3.isoformat()),
    }

    # Most-recent-first ordering.
    ats = [e["at"] for e in led["events"]]
    assert ats == sorted(ats, reverse=True)

    # Current state: research_use + ai_training granted (latest episode active).
    acct = {c["kind"]: c for c in led["account_consents"]}
    assert acct["research_use"]["granted"] is True
    assert acct["ai_training"]["granted"] is True

    # Active study consent carries the proof (version + hash); revoked excluded.
    assert len(led["active_study_consents"]) == 1
    asc = led["active_study_consents"][0]
    assert asc["tier"] == "t3"
    assert asc["consent_version"] == 2
    assert asc["consent_hash"] == _HASH

    # The study revoke event carries the captured reason + proof hash.
    study_revoke = next(
        e for e in led["events"] if e["scope"] == "study" and e["action"] == "revoked"
    )
    assert study_revoke["reason"] == "changed my mind"
    assert study_revoke["consent_hash"] == _HASH

    # Honest framing surfaced verbatim.
    assert "point-in-time" in led["scope"]


@skip_if_no_db
async def test_point_in_time_proof(db_session, make_user, make_study):
    owner = await make_user()
    study, _series = await make_study(owner)
    await _seed(db_session, owner, study)

    # During the first research_use episode (T1..T2): granted.
    during = datetime(2026, 1, 20, tzinfo=UTC)
    led = await build_consent_ledger(db_session, owner.subject_id, as_of=during)
    state = {a["kind"]: a["granted"] for a in led["as_of_state"]["account"]}
    assert state["research_use"] is True
    assert led["as_of"] == during.isoformat()
    assert led["as_of_state"]["active_study_consents"] == 1  # study t3 active T1..T2

    # In the gap after the revoke, before the re-grant (T2..T3): revoked.
    gap = datetime(2026, 2, 15, tzinfo=UTC)
    led2 = await build_consent_ledger(db_session, owner.subject_id, as_of=gap)
    state2 = {a["kind"]: a["granted"] for a in led2["as_of_state"]["account"]}
    assert state2["research_use"] is False
    assert led2["as_of_state"]["active_study_consents"] == 0

    # Required consents are implicit from account creation.
    assert state2["terms_of_service"] is True


@skip_if_no_db
async def test_endpoint_wraps_validates_and_parses_as_of(db_session, make_user, make_study):
    owner = await make_user()
    study, _series = await make_study(owner)
    await _seed(db_session, owner, study)

    # Human session: enforce_agent_scope is a no-op (state.is_agent falsy).
    req = SimpleNamespace(state=SimpleNamespace(is_agent=False))

    out = await get_consent_ledger(req, db_session, owner, as_of=None)
    assert isinstance(out, ConsentLedgerOut)
    assert len(out.events) == 7
    assert out.as_of_state is None

    out2 = await get_consent_ledger(req, db_session, owner, as_of="2026-01-20T00:00:00+00:00")
    assert out2.as_of_state is not None
    aof = {a.kind: a.granted for a in out2.as_of_state.account}
    assert aof["research_use"] is True

    with pytest.raises(HTTPException) as ei:
        await get_consent_ledger(req, db_session, owner, as_of="not-a-date")
    assert ei.value.status_code == 422


@skip_if_no_db
async def test_list_consents_refactor_preserves_behavior(db_session, make_user):
    """The shared collapse helper must not change ``/consents`` output."""
    owner = await make_user()
    db_session.add(
        Consent(user_subject_id=owner.subject_id, kind="research_use", granted_at=T1, revoked_at=T2)
    )
    db_session.add(Consent(user_subject_id=owner.subject_id, kind="research_use", granted_at=T3))
    await db_session.flush()

    rows = await list_consents(db_session, owner)
    by_kind = {c.kind: c for c in rows}
    # Latest episode is active -> granted, no revoked_at surfaced.
    assert by_kind["research_use"].granted is True
    assert by_kind["research_use"].revoked_at is None
    # Required kinds implicitly granted even with no row.
    assert by_kind["terms_of_service"].granted is True
