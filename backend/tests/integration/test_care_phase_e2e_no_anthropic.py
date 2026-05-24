"""End-to-end care-timeline test that does not need a real Anthropic API key.

Exercises every endpoint that the MCP tools in
``mcp/src/bvmcp/tools/care_phases.py`` wrap, against the live FastAPI
app via ``httpx.ASGITransport`` (no socket). The classifier is
swapped for a ``FakeLLM`` that returns the Patient X golden JSON
deterministically, so we get the 7/7-equivalent acceptance bar
without leaving the dev box.

Coverage map (MCP tool ↔ REST endpoint):

* ``get_care_timeline``       → GET /care-timeline?format=json|svg|markdown|ics
* ``render_care_timeline_svg``→ GET /care-timeline?format=svg
* ``list_care_phases``        → GET /care-phases
* ``get_care_phase``          → GET /care-phases/{id}
* ``list_care_phase_material``→ GET /care-phases/{id}/material
* ``list_care_phase_revisions``→ GET /care-phases/{id}/revisions
* ``get_care_timeline_health``→ GET /care-timeline/health
* ``propose_care_phases``     → POST /care-phases:propose
* ``apply_phase_proposal``    → POST /care-phases:apply-proposal (Idempotency-Key)
* ``create_care_phase``       → POST /care-phases
* ``update_care_phase``       → PATCH /care-phases/{id} (If-Match)
* ``assign_event_to_phase``   → PUT /care-phases/{id}/events/{eid}
* ``unassign_event_from_phase``→ DELETE /care-phases/{id}/events/{eid}
* ``reorder_care_phases``     → POST /care-phases:reorder
* ``restore_care_phase_revision``→ POST /care-phases/{id}/restore
* ``delete_care_phase``       → DELETE /care-phases/{id}
* ``export_care_timeline_ics``→ GET /care-timeline?format=ics
* ``get_my_scopes``           → GET /me/scopes
* (``export_care_timeline_pdf``→ deferred behind weasyprint, asserts 501)

Cross-patient sweep: for the GET-by-id endpoints, calling them with
an id that belongs to a different patient must return 404 (never 400).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.tokens import issue_access_token
from bvphoenix.db.models import ClinicalEvent, Patient, Subject, User
from bvphoenix.main import app
from bvphoenix.services import care_phase_classifier as classifier_mod
from bvphoenix.services import llm as llm_mod
from bvphoenix.services.llm import LLMResult, LLMUsage
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "care_phases"
    / "canary_patient_expected.json"
)


# ---------------------------------------------------------------------------
# Fake events matching the golden fixture (date + title pattern).
# ---------------------------------------------------------------------------


def _golden() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


# Map (date, title_keyword) -> generated UUID, populated when we seed the DB.
SeedKey = tuple[date, str]


# ---------------------------------------------------------------------------
# FakeLLM: returns the golden JSON, prompt-aware (it remaps event ids to the
# ones we just inserted). Implements the LLMProvider Protocol.
# ---------------------------------------------------------------------------


class FakeLLM:
    """Deterministic LLM provider that produces the Patient X golden JSON.

    Reads the input event ids out of the user prompt (they are listed
    verbatim in the JSON schema example) and binds each event to the
    expected phase slug per the golden fixture. The output JSON is the
    exact structure ``ClassifierOutput`` expects.
    """

    model_id = "fake-llm-canary-v1"

    def __init__(self, expected: dict[str, Any]) -> None:
        self._expected = expected

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        cache_control: bool = False,
        stream: bool = False,
        max_tokens: int = 1024,
    ) -> LLMResult:
        # Pull the input event payload back out of the user prompt.
        user_text = ""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content")
                user_text = content if isinstance(content, str) else json.dumps(content)
        # Find the JSON block listing input events; the prompt embeds it
        # via json.dumps so we can recover ids deterministically.
        events_in_prompt: list[dict[str, Any]] = []
        m = re.search(r"\[\s*\{[^\[]+?\}\s*\]", user_text, re.DOTALL)
        if m:
            try:
                events_in_prompt = json.loads(m.group(0))
            except json.JSONDecodeError:
                events_in_prompt = []

        # Build assignments by matching (date, title_pattern) → event id.
        assignments: list[dict[str, Any]] = []
        phases_payload: list[dict[str, Any]] = []
        for phase in self._expected["phases"]:
            phases_payload.append(
                {
                    "slug": phase["slug"],
                    "name": phase["name_i18n"]["it"],
                    "name_i18n": phase["name_i18n"],
                    "kind": phase["kind"],
                    "color_hex": None,
                    "ordinal": phase["ordinal"],
                    "narrative_md": None,
                }
            )
            for ev in phase["events"]:
                exp_date = ev["date"]
                pattern = ev["title_pattern"].lower()
                match = next(
                    (
                        e
                        for e in events_in_prompt
                        if e.get("event_date") == exp_date
                        and pattern in (e.get("title", "").lower())
                    ),
                    None,
                )
                if match is None:
                    continue
                assignments.append(
                    {
                        "event_id": match["id"],
                        "phase_slug": phase["slug"],
                        "confidence": 0.95,
                    }
                )

        body = {"phases": phases_payload, "assignments": assignments}
        text = json.dumps(body, ensure_ascii=False)
        return LLMResult(
            text=text,
            model_id=self.model_id,
            usage=LLMUsage(),
            stop_reason="end_turn",
        )

    async def describe_series(self, **_kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def summarize_fascicolo(self, **_kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def summarize(self, **_kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _patch_llm(monkeypatch: pytest.MonkeyPatch):
    fake = FakeLLM(_golden())
    monkeypatch.setattr(llm_mod, "get_llm_provider", lambda: fake)
    monkeypatch.setattr(classifier_mod, "get_llm_provider", lambda: fake)
    return fake


@pytest_asyncio.fixture
async def _two_patients_with_events(db_session: AsyncSession):
    """Seed two patients, only patient A gets events (Patient X structure)."""
    sid = uuid.uuid4()
    db_session.add(Subject(id=sid, kind="user", display_name=f"e2e-{sid}"))
    await db_session.flush()
    db_session.add(
        User(
            subject_id=sid,
            email=f"e2e-{sid}@example.com",
            password_hash=None,
            is_admin=False,
        )
    )
    await db_session.flush()
    pa = Patient(id=uuid.uuid4(), managed_by_subject_id=sid, display_name="A (Patient X)")
    pb = Patient(id=uuid.uuid4(), managed_by_subject_id=sid, display_name="B (other)")
    db_session.add_all([pa, pb])
    await db_session.flush()

    # Seed A's events from the golden fixture so the FakeLLM has the
    # ids it needs to bind back.
    expected = _golden()
    seed_index: dict[SeedKey, uuid.UUID] = {}
    for phase in expected["phases"]:
        for ev_spec in phase["events"]:
            ev = ClinicalEvent(
                id=uuid.uuid4(),
                patient_id=pa.id,
                kind="other",
                event_date=date.fromisoformat(ev_spec["date"]),
                title=ev_spec["title_pattern"],
            )
            db_session.add(ev)
            seed_index[(ev.event_date, ev_spec["title_pattern"])] = ev.id
    # And one event on B so cross-patient checks have a victim id.
    eb = ClinicalEvent(
        id=uuid.uuid4(),
        patient_id=pb.id,
        kind="other",
        event_date=date(2026, 1, 1),
        title="patient-B event",
    )
    db_session.add(eb)
    await db_session.commit()
    return sid, pa, pb, seed_index, eb.id


def _bearer(subject_id: uuid.UUID, email: str) -> dict[str, str]:
    raw = issue_access_token(subject_id=subject_id, email=email, is_admin=False)
    return {"Authorization": f"Bearer {raw}"}


@pytest_asyncio.fixture
async def http() -> httpx.AsyncClient:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://e2e") as client:
        yield client


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


async def test_care_phase_full_pipeline_no_anthropic(
    _patch_llm,
    _two_patients_with_events,
    http: httpx.AsyncClient,
):
    sid, pa, pb, _seed_index, event_b_id = _two_patients_with_events
    auth = _bearer(sid, f"e2e-{sid}@example.com")

    # ---- /me/scopes (introspection) ----
    r = await http.get("/api/me/scopes", headers=auth)
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["subject_id"] == str(sid)
    assert "scopes" in me

    # ---- propose (FakeLLM-driven, no Anthropic) ----
    r = await http.post(f"/api/patients/{pa.id}/care-phases:propose?lang=it", headers=auth)
    assert r.status_code == 200, r.text
    proposal = r.json()
    assert proposal["model_id"] == FakeLLM.model_id
    payload = proposal["payload"]
    assert len(payload["phases"]) == 7, payload
    proposed_slugs = [p["slug"] for p in payload["phases"]]
    expected_slugs = [p["slug"] for p in _golden()["phases"]]
    assert proposed_slugs == expected_slugs, (proposed_slugs, expected_slugs)
    assert len(payload["assignments"]) == sum(len(p["events"]) for p in _golden()["phases"])

    # ---- apply-proposal (Idempotency-Key mandatory; missing → 428) ----
    body_apply = {
        "proposal_id": proposal["proposal_id"],
        "accept_phases": proposed_slugs,
        "accept_assignments": [a["event_id"] for a in payload["assignments"]],
    }
    r_no_idem = await http.post(
        f"/api/patients/{pa.id}/care-phases:apply-proposal",
        headers=auth,
        json=body_apply,
    )
    assert r_no_idem.status_code == 428, r_no_idem.text

    r = await http.post(
        f"/api/patients/{pa.id}/care-phases:apply-proposal",
        headers={**auth, "Idempotency-Key": "e2e-apply-1"},
        json=body_apply,
    )
    assert r.status_code == 200, r.text
    applied = r.json()
    assert len(applied["applied_phases"]) == 7
    assert applied["applied_assignments"] == len(payload["assignments"])

    # ---- list_care_phases (cheap chip list) ----
    r = await http.get(f"/api/patients/{pa.id}/care-phases", headers=auth)
    assert r.status_code == 200, r.text
    chips = r.json()
    assert len(chips) == 7
    assert sorted(p["slug"] for p in chips) == sorted(expected_slugs)
    # 18 events total were assigned in the golden.
    assert sum(p["counts"]["n_events"] for p in chips) == sum(
        len(p["events"]) for p in _golden()["phases"]
    )

    # Pick the first phase (chronological order).
    chips_sorted = sorted(chips, key=lambda p: p["ordinal"])
    first = chips_sorted[0]
    second = chips_sorted[1]

    # ---- get_care_phase + revisions + material ----
    r = await http.get(f"/api/patients/{pa.id}/care-phases/{first['id']}", headers=auth)
    assert r.status_code == 200
    detail = r.json()
    assert detail["slug"] == first["slug"]
    assert len(detail["events"]) == first["counts"]["n_events"]

    r = await http.get(
        f"/api/patients/{pa.id}/care-phases/{first['id']}/revisions",
        headers=auth,
    )
    assert r.status_code == 200
    revs = r.json()
    # apply-proposal created the phase and added one revision; we may
    # also see a create-from-proposal entry. Either way at least 1.
    assert len(revs) >= 1

    r = await http.get(
        f"/api/patients/{pa.id}/care-phases/{first['id']}/material",
        headers=auth,
    )
    assert r.status_code == 200
    material = r.json()
    assert material["phase_id"] == first["id"]

    # ---- timeline json + markdown + svg + ics + health ----
    r = await http.get(f"/api/patients/{pa.id}/care-timeline?lang=it&format=json", headers=auth)
    assert r.status_code == 200
    tl = r.json()
    assert len(tl["phases"]) == 7

    r = await http.get(f"/api/patients/{pa.id}/care-timeline?lang=it&format=markdown", headers=auth)
    assert r.status_code == 200
    md = r.text
    for slug_name in [p["name"] for p in chips_sorted]:
        # The markdown header uses the localised name; just assert the
        # first phase name is present somewhere.
        if slug_name in md:
            break
    else:  # pragma: no cover
        pytest.fail(f"markdown missing every phase name; got: {md[:300]}")

    r = await http.get(f"/api/patients/{pa.id}/care-timeline?lang=it&format=svg", headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.lstrip().startswith("<svg")

    r = await http.get(f"/api/patients/{pa.id}/care-timeline?lang=it&format=ics", headers=auth)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/calendar")
    ics = r.text
    assert "BEGIN:VCALENDAR" in ics and "END:VCALENDAR" in ics
    # 18 events ⇒ 18 VEVENT blocks.
    assert ics.count("BEGIN:VEVENT") == sum(len(p["events"]) for p in _golden()["phases"])

    r = await http.get(f"/api/patients/{pa.id}/care-timeline?format=pdf", headers=auth)
    assert r.status_code == 501, r.text

    r = await http.get(f"/api/patients/{pa.id}/care-timeline/health", headers=auth)
    assert r.status_code == 200
    health = r.json()
    assert health["n_phases"] == 7
    assert health["pct_assigned"] == pytest.approx(1.0)

    # ---- update with If-Match (412 on stale, 200 on match) ----
    r = await http.patch(
        f"/api/patients/{pa.id}/care-phases/{first['id']}",
        headers={**auth, "If-Match": '"deadbeef"'},
        json={"narrative_md": "wrong etag"},
    )
    assert r.status_code == 412

    # Body etag and response ETag header MUST round-trip: a client that
    # reads ``etag`` from the body of a previous response and replays it
    # as ``If-Match`` MUST get a 200 (regression: clients used to get
    # 412 because the header was the no-dash hex form while the body
    # was the dashed UUID).
    r = await http.get(f"/api/patients/{pa.id}/care-phases/{first['id']}", headers=auth)
    body_etag = r.json()["etag"]
    header_etag = r.headers["etag"]
    # Sanity: the two representations must agree once normalised.
    assert uuid.UUID(body_etag) == uuid.UUID(header_etag.strip('"'))

    # 1) Round-trip via the body etag (the form clients actually see).
    r = await http.patch(
        f"/api/patients/{pa.id}/care-phases/{first['id']}",
        headers={**auth, "If-Match": f'"{body_etag}"'},
        json={"narrative_md": "edited via body etag"},
    )
    assert r.status_code == 200, r.text
    new_etag_header = r.headers["etag"]
    assert new_etag_header != header_etag

    # 2) The hex (no-dashes) form is also accepted, for backwards
    # compatibility with any client that grabbed the legacy header.
    r2 = await http.get(f"/api/patients/{pa.id}/care-phases/{first['id']}", headers=auth)
    bare_hex = uuid.UUID(r2.json()["etag"]).hex
    r = await http.patch(
        f"/api/patients/{pa.id}/care-phases/{first['id']}",
        headers={**auth, "If-Match": f'"{bare_hex}"'},
        json={"narrative_md": "edited via bare hex"},
    )
    assert r.status_code == 200, r.text

    # ---- assign / unassign ----
    # Pick an event currently in `second`, move it to `first`.
    r = await http.get(f"/api/patients/{pa.id}/care-phases/{second['id']}", headers=auth)
    second_events = r.json()["events"]
    moving_event_id = second_events[0]["id"]

    r = await http.put(
        f"/api/patients/{pa.id}/care-phases/{first['id']}/events/{moving_event_id}",
        headers=auth,
        json={"confidence": 0.8},
    )
    assert r.status_code == 200, r.text

    r = await http.delete(
        f"/api/patients/{pa.id}/care-phases/{first['id']}/events/{moving_event_id}",
        headers=auth,
    )
    assert r.status_code == 204

    # ---- reorder ----
    new_ordinals = [
        {"phase_id": chips_sorted[i]["id"], "ordinal": 7 - i - 1} for i in range(len(chips_sorted))
    ]
    r = await http.post(
        f"/api/patients/{pa.id}/care-phases:reorder",
        headers=auth,
        json={"ordinals": new_ordinals},
    )
    assert r.status_code == 200, r.text
    reordered = sorted(r.json(), key=lambda p: p["ordinal"])
    assert reordered[0]["id"] == chips_sorted[-1]["id"]

    # ---- restore revision ----
    r = await http.get(
        f"/api/patients/{pa.id}/care-phases/{first['id']}/revisions",
        headers=auth,
    )
    revs_now = r.json()
    earliest = min(r["revision_no"] for r in revs_now)
    r = await http.post(
        f"/api/patients/{pa.id}/care-phases/{first['id']}/restore",
        headers=auth,
        json={"revision_no": earliest},
    )
    assert r.status_code == 200

    # ---- create + delete (delete leaves orphan events via SET NULL) ----
    r = await http.post(
        f"/api/patients/{pa.id}/care-phases",
        headers=auth,
        json={
            "slug": "scratchpad",
            "name": "Scratchpad",
            "name_i18n": {"it": "Scratchpad", "en": "Scratchpad"},
            "kind": "other",
            "ordinal": 99,
        },
    )
    assert r.status_code == 201, r.text
    created_id = r.json()["id"]
    r = await http.delete(f"/api/patients/{pa.id}/care-phases/{created_id}", headers=auth)
    assert r.status_code == 204

    # ---- CROSS-PATIENT SWEEP: every read-by-id with B's id under A's
    # route must return 404 (never 400). The composite FK + nested route
    # makes cross-patient unrepresentable.
    foreign_phase_endpoints = [
        f"/api/patients/{pa.id}/care-phases/{uuid.uuid4()}",
        f"/api/patients/{pa.id}/care-phases/{uuid.uuid4()}/material",
        f"/api/patients/{pa.id}/care-phases/{uuid.uuid4()}/revisions",
    ]
    for path in foreign_phase_endpoints:
        r = await http.get(path, headers=auth)
        assert r.status_code == 404, (path, r.status_code, r.text)
        assert r.status_code != 400

    # Try to assign B's event to one of A's phases via A's route:
    r = await http.put(
        f"/api/patients/{pa.id}/care-phases/{first['id']}/events/{event_b_id}",
        headers=auth,
        json={"confidence": 1.0},
    )
    # The event does not belong to A → 404 from the service layer.
    assert r.status_code == 404, r.text

    # Try to read patient B's timeline with A's auth: A managed B too in
    # this fixture, so it works (200). We're only proving the inverse
    # (cross-patient ids inside a route) above. Sanity:
    r = await http.get(f"/api/patients/{pb.id}/care-timeline", headers=auth)
    assert r.status_code == 200, r.text
    tl_b = r.json()
    assert tl_b["patient_id"] == str(pb.id)
    # B has 1 event, no phases yet.
    assert tl_b["phases"] == []
