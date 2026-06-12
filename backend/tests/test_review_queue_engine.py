"""Engine + state-machine contract tests — fully isolated.

Exercises the shared review engine end-to-end through a *fake* profile
and store (plain objects implementing the mixin attribute set), per the
task contract: no consumer tables, no Postgres, no HTTP. Provenance is
asserted on the captured ``ProvenanceEvent`` objects collected by a
stub session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from bvphoenix.db.models import ProvenanceEvent
from bvphoenix.services.review_queue import (
    REVIEW_STATUSES,
    REVIEW_TERMINAL_STATUSES,
    REVIEW_TRANSITIONS,
    CheckContext,
    CheckResult,
    DecisionPolicy,
    ReviewActor,
    ReviewDecisionError,
    ReviewProfile,
    ReviewTransitionError,
    StagedComponent,
    StagedItem,
    aggregate_verdicts,
    engine,
    validate_transition,
)
from bvphoenix.services.review_queue.actor import SYSTEM_ACTOR
from bvphoenix.services.review_queue.checks import run_checks
from bvphoenix.services.review_queue.profile import (
    _clear_profiles_for_tests,
    get_profile,
    register_profile,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    """Just enough AsyncSession surface for the engine: ``add`` collects
    the provenance rows the engine appends."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    @property
    def provenance(self) -> list[ProvenanceEvent]:
        return [o for o in self.added if isinstance(o, ProvenanceEvent)]


@dataclass
class FakeItem:
    """A store row: exactly the ReviewableItemMixin attribute set."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: str = "received"
    auto_checks: dict | None = None
    auto_verdict: str | None = None
    reviewed_by_subject_id: uuid.UUID | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    etag: uuid.UUID = field(default_factory=uuid.uuid4)


def _component(name: str = "referto.pdf", payload: bytes = b"%PDF-1.4 fake") -> StagedComponent:
    async def _read() -> bytes:
        return payload

    return StagedComponent(
        name=name, size_bytes=len(payload), content_type="application/pdf", read=_read
    )


class StaticCheck:
    """Configurable test plugin: fixed verdict, counts invocations."""

    def __init__(self, name: str, verdict: str = "pass", *, boom: bool = False) -> None:
        self.name = name
        self.verdict = verdict
        self.boom = boom
        self.calls = 0

    async def run(self, ctx: CheckContext) -> CheckResult:
        self.calls += 1
        if self.boom:
            raise RuntimeError("plugin exploded")
        return CheckResult(verdict=self.verdict, details={"call": self.calls})


def make_profile(
    *,
    checks: tuple = (),
    gate: str = "human_only",
    require_reason: bool = False,
    can_decide=None,
    accept_boom: bool = False,
) -> tuple[ReviewProfile, dict]:
    """Fake profile with instrumented hooks; returns (profile, counters)."""
    counters = {"on_accept": 0, "on_reject": 0, "load_staged": 0}

    async def load_item(db, item_id):  # pragma: no cover — worker-path only
        return None

    async def load_staged(db, item) -> StagedItem:
        counters["load_staged"] += 1
        return StagedItem(item_id=item.id, components=[_component()], manifest={})

    async def on_accept(db, item, actor):
        counters["on_accept"] += 1
        if accept_boom:
            raise RuntimeError("promotion exploded")
        return {"document_id": "fake"}

    async def on_reject(db, item, actor, reason):
        counters["on_reject"] += 1

    profile = ReviewProfile(
        name="test-profile",
        provenance_target_kind="inbox_item",
        checks=checks,
        decision=DecisionPolicy(gate=gate, require_reason=require_reason, can_decide=can_decide),
        load_item=load_item,
        load_staged=load_staged,
        on_accept=on_accept,
        on_reject=on_reject,
    )
    return profile, counters


HUMAN = ReviewActor(kind="human", subject_id=uuid.uuid4())
AGENT = ReviewActor(kind="agent", agent_token_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# State machine table
# ---------------------------------------------------------------------------


def test_transition_table_covers_every_status() -> None:
    assert set(REVIEW_TRANSITIONS) == set(REVIEW_STATUSES)
    for terminal in REVIEW_TERMINAL_STATUSES:
        assert REVIEW_TRANSITIONS[terminal] == frozenset()
    # every transition target is a known status
    for targets in REVIEW_TRANSITIONS.values():
        assert targets <= set(REVIEW_STATUSES)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        ("received", "accepted"),  # no jumps past the checks
        ("received", "needs_review"),
        ("processing", "promoted"),
        ("blocked", "accepted"),  # the hard-fail gate
        ("accepted", "rejected"),  # decisions are not reversible
        ("promoted", "processing"),  # terminal
        ("rejected", "processing"),  # terminal
        ("nonexistent", "processing"),  # unknown source
    ],
)
def test_validate_transition_rejects_inadmissible_edges(src: str, dst: str) -> None:
    with pytest.raises(ReviewTransitionError):
        validate_transition(src, dst)


def test_validate_transition_accepts_declared_edges() -> None:
    for src, targets in REVIEW_TRANSITIONS.items():
        for dst in targets:
            validate_transition(src, dst)  # must not raise


def test_aggregate_verdicts_worst_of() -> None:
    assert aggregate_verdicts([]) == "pass"
    assert aggregate_verdicts(["pass", "warn"]) == "warn"
    assert aggregate_verdicts(["warn", "fail", "pass"]) == "fail"
    assert aggregate_verdicts(["fail", "error"]) == "error"
    assert aggregate_verdicts(["error", "block", "pass"]) == "block"


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_received_to_promoted() -> None:
    profile, counters = make_profile(checks=(StaticCheck("clamav"), StaticCheck("dedup", "warn")))
    db = FakeSession()
    item = FakeItem()
    etags = [item.etag]

    await engine.start_processing(db, profile, item)
    assert item.status == "processing"
    etags.append(item.etag)

    verdict = await engine.run_auto_checks(db, profile, item)
    assert verdict == "warn"
    assert item.status == "needs_review"
    assert item.auto_verdict == "warn"
    assert item.auto_checks is not None
    assert set(item.auto_checks["checks"]) == {"clamav", "dedup"}
    etags.append(item.etag)

    await engine.decide(db, profile, item, decision="accepted", actor=HUMAN)
    assert item.status == "accepted"
    assert item.reviewed_by_subject_id == HUMAN.subject_id
    assert item.reviewed_at is not None
    etags.append(item.etag)

    outcome = await engine.promote(db, profile, item)
    assert item.status == "promoted"
    assert outcome == {"document_id": "fake"}
    assert counters["on_accept"] == 1
    assert counters["on_reject"] == 0
    etags.append(item.etag)

    # etag bumped on every transition (promote does two)
    assert len(set(etags)) == len(etags)

    activities = [e.activity for e in db.provenance]
    assert activities == [
        "transition.processing",
        "transition.needs_review",
        "transition.accepted",
        "transition.promoting",
        "transition.promoted",
    ]
    assert all(e.target_kind == "inbox_item" for e in db.provenance)
    assert all(e.target_id == item.id for e in db.provenance)


@pytest.mark.asyncio
async def test_block_verdict_lands_on_blocked_and_cannot_be_accepted() -> None:
    profile, counters = make_profile(checks=(StaticCheck("clamav", "block"),))
    db = FakeSession()
    item = FakeItem(status="processing")

    verdict = await engine.run_auto_checks(db, profile, item)
    assert verdict == "block"
    assert item.status == "blocked"

    # the gate authorizes the human, but the state machine has no
    # blocked -> accepted edge
    with pytest.raises(ReviewTransitionError):
        await engine.decide(db, profile, item, decision="accepted", actor=HUMAN)
    assert item.status == "blocked"
    assert counters["on_accept"] == 0

    await engine.decide(db, profile, item, decision="rejected", actor=HUMAN, reason="malware")
    assert item.status == "rejected"
    assert counters["on_reject"] == 1


@pytest.mark.asyncio
async def test_auto_checks_rerun_is_idempotent() -> None:
    check = StaticCheck("magic_allowlist")
    profile, _ = make_profile(checks=(check,))
    db = FakeSession()
    item = FakeItem(status="processing")

    await engine.run_auto_checks(db, profile, item)
    first = item.auto_checks["checks"]["magic_allowlist"]
    assert item.status == "needs_review"

    # re-run path: needs_review -> processing -> needs_review
    await engine.start_processing(db, profile, item)
    await engine.run_auto_checks(db, profile, item)
    assert item.status == "needs_review"
    assert check.calls == 2
    # still exactly one entry, overwritten in place
    assert set(item.auto_checks["checks"]) == {"magic_allowlist"}
    assert item.auto_checks["checks"]["magic_allowlist"]["details"]["call"] == 2
    assert first["details"]["call"] == 1


@pytest.mark.asyncio
async def test_crashing_plugin_records_error_not_pass() -> None:
    profile, _ = make_profile(checks=(StaticCheck("clamav", boom=True), StaticCheck("dedup")))
    db = FakeSession()
    item = FakeItem(status="processing")

    verdict = await engine.run_auto_checks(db, profile, item)
    assert verdict == "error"
    assert item.status == "needs_review"  # error does not block
    entry = item.auto_checks["checks"]["clamav"]
    assert entry["verdict"] == "error"
    assert "plugin exploded" in entry["details"]["exception"]


@pytest.mark.asyncio
async def test_duplicate_check_names_refused() -> None:
    profile, _ = make_profile(checks=(StaticCheck("dup"), StaticCheck("dup")))
    db = FakeSession()
    item = FakeItem(status="processing")
    with pytest.raises(ValueError, match="duplicate check names"):
        await engine.run_auto_checks(db, profile, item)


@pytest.mark.asyncio
async def test_run_checks_requires_processing_state() -> None:
    profile, _ = make_profile()
    db = FakeSession()
    with pytest.raises(ReviewTransitionError):
        await engine.run_auto_checks(db, profile, FakeItem(status="received"))


# ---------------------------------------------------------------------------
# Decision gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_only_gate_refuses_agent_and_system() -> None:
    profile, _ = make_profile(gate="human_only")
    db = FakeSession()
    item = FakeItem(status="needs_review")

    with pytest.raises(ReviewDecisionError) as exc:
        await engine.decide(db, profile, item, decision="accepted", actor=AGENT)
    assert exc.value.code == "decision.human_only"

    with pytest.raises(ReviewDecisionError) as exc:
        await engine.decide(db, profile, item, decision="accepted", actor=SYSTEM_ACTOR)
    assert exc.value.code == "decision.system_forbidden"

    assert item.status == "needs_review"  # nothing moved
    assert db.provenance == []


@pytest.mark.asyncio
async def test_agent_capable_gate_allows_agent_with_provenance() -> None:
    profile, _ = make_profile(gate="agent_capable")
    db = FakeSession()
    item = FakeItem(status="needs_review")

    await engine.decide(db, profile, item, decision="accepted", actor=AGENT)
    assert item.status == "accepted"
    # agent decisions never masquerade as humans
    assert item.reviewed_by_subject_id is None
    (event,) = db.provenance
    assert event.agent_kind == "agent"
    assert event.agent_token_id == AGENT.agent_token_id
    assert event.agent_subject_id is None

    # system still refused even on agent_capable profiles
    with pytest.raises(ReviewDecisionError):
        await engine.decide(
            db, profile, FakeItem(status="needs_review"), decision="rejected", actor=SYSTEM_ACTOR
        )


@pytest.mark.asyncio
async def test_require_reason_enforced() -> None:
    profile, _ = make_profile(require_reason=True)
    db = FakeSession()
    item = FakeItem(status="needs_review")

    with pytest.raises(ReviewDecisionError) as exc:
        await engine.decide(db, profile, item, decision="rejected", actor=HUMAN, reason="  ")
    assert exc.value.code == "decision.reason_required"

    await engine.decide(db, profile, item, decision="rejected", actor=HUMAN, reason="duplicate")
    assert item.review_note == "duplicate"


@pytest.mark.asyncio
async def test_can_decide_rbac_hook() -> None:
    async def deny(db, actor, item) -> bool:
        return False

    profile, _ = make_profile(can_decide=deny)
    db = FakeSession()
    item = FakeItem(status="needs_review")
    with pytest.raises(ReviewDecisionError) as exc:
        await engine.decide(db, profile, item, decision="accepted", actor=HUMAN)
    assert exc.value.code == "decision.not_authorized"


# ---------------------------------------------------------------------------
# Promotion / rejection hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_requires_accepted_state() -> None:
    profile, counters = make_profile()
    db = FakeSession()
    with pytest.raises(ReviewTransitionError):
        await engine.promote(db, profile, FakeItem(status="needs_review"))
    assert counters["on_accept"] == 0


@pytest.mark.asyncio
async def test_promotion_failure_lands_on_failed_and_reraises() -> None:
    profile, counters = make_profile(accept_boom=True)
    db = FakeSession()
    item = FakeItem(status="accepted")

    with pytest.raises(RuntimeError, match="promotion exploded"):
        await engine.promote(db, profile, item)
    assert item.status == "failed"
    assert counters["on_accept"] == 1
    activities = [e.activity for e in db.provenance]
    assert activities == ["transition.promoting", "transition.failed"]
    assert db.provenance[-1].diff["error"] == "promotion exploded"


@pytest.mark.asyncio
async def test_reject_hook_called_exactly_once() -> None:
    profile, counters = make_profile()
    db = FakeSession()
    item = FakeItem(status="needs_review")
    await engine.decide(db, profile, item, decision="rejected", actor=HUMAN, reason="no")
    assert counters["on_reject"] == 1
    # a second decision on a terminal item is inadmissible
    with pytest.raises(ReviewTransitionError):
        await engine.decide(db, profile, item, decision="rejected", actor=HUMAN, reason="no")
    assert counters["on_reject"] == 1


@pytest.mark.asyncio
async def test_expire_terminalizes_undecided_item() -> None:
    profile, _ = make_profile()
    db = FakeSession()
    item = FakeItem(status="needs_review")
    await engine.expire(db, profile, item, reason="retention 30d")
    assert item.status == "expired"
    (event,) = db.provenance
    assert event.agent_kind == "system"
    assert event.diff["reason"] == "retention 30d"


# ---------------------------------------------------------------------------
# End-to-end through run_checks directly (registry-level invariants)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_checks_preserves_entries_of_removed_checks() -> None:
    db = FakeSession()
    staged = StagedItem(item_id=uuid.uuid4(), components=[_component()])
    ctx = CheckContext(db=db, staged=staged)  # type: ignore[arg-type]

    first, _ = await run_checks(ctx, [StaticCheck("a"), StaticCheck("b", "fail")])
    second, verdict = await run_checks(ctx, [StaticCheck("a")], previous=first)
    # "b" no longer in the profile: its last outcome stays visible and
    # still weighs on the aggregate (history is not silently dropped)
    assert set(second["checks"]) == {"a", "b"}
    assert verdict == "fail"


# ---------------------------------------------------------------------------
# Profile registry
# ---------------------------------------------------------------------------


def test_profile_registry_round_trip_and_conflicts() -> None:
    _clear_profiles_for_tests()
    profile, _ = make_profile()
    try:
        register_profile(profile)
        assert get_profile("test-profile") is profile
        register_profile(profile)  # same object: idempotent
        other, _ = make_profile()
        with pytest.raises(ValueError, match="already registered"):
            register_profile(other)
        with pytest.raises(KeyError, match="unknown review profile"):
            get_profile("missing")
    finally:
        _clear_profiles_for_tests()


# ---------------------------------------------------------------------------
# Actor invariants
# ---------------------------------------------------------------------------


def test_actor_invariants() -> None:
    with pytest.raises(ValueError, match="requires subject_id"):
        ReviewActor(kind="human")
    with pytest.raises(ValueError, match="agent_token_id or agent_assistant_id"):
        ReviewActor(kind="agent")
    ReviewActor(kind="agent", agent_assistant_id=uuid.uuid4())  # ok
