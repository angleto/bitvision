"""Public data-governance policy endpoint + drift pins.

``GET /api/governance`` publishes the *applied* policy (de-id passes,
k-anon threshold, tiers, licences, patient rights) as a versioned,
machine-readable descriptor — the auditable counterpart to a closed
black-box. These tests are DB-free (they run in the PR gate) and pin the
published values to the runtime constants so the policy cannot drift from
the code, and assert the load-bearing honesty framing is present.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bvphoenix.api.governance import GOVERNANCE_POLICY_VERSION, build_governance_policy
from bvphoenix.config import get_settings
from bvphoenix.main import app
from bvphoenix.services.deid_text import _KIND_TO_PATTERN
from bvphoenix.services.k_anonymity import DEFAULT_K_MIN

client = TestClient(app)


def test_policy_is_pinned_to_runtime_constants() -> None:
    policy = build_governance_policy(get_settings())
    assert policy.policy_version == GOVERNANCE_POLICY_VERSION
    # k-anon threshold comes from the constant the enforcer actually uses.
    assert policy.k_anonymity_min == DEFAULT_K_MIN
    # The published de-id categories ARE the runtime rule table — not a
    # hand-maintained copy that could drift.
    assert policy.deidentification.text_regex_categories == [
        kind for kind, _pat, _placeholder in _KIND_TO_PATTERN
    ]
    assert set(policy.contribution_tiers) == {"t1", "t2", "t3", "t4"}


def test_framing_is_honest_not_overclaiming() -> None:
    """Guard-rail: the policy must frame as pseudonymization, NOT claim
    irreversible-anonymization parity (the one axis a closed lake is
    strong on). Overclaiming here would be inaccurate."""
    framing = build_governance_policy(get_settings()).framing.lower()
    assert "pseudonymization" in framing
    assert "not" in framing and "irreversible" in framing


def test_endpoint_is_public_and_well_formed() -> None:
    """No Authorization header — the policy describes policy, not data."""
    resp = client.get("/api/governance")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["policy_version"] == GOVERNANCE_POLICY_VERSION
    assert body["k_anonymity_min"] == DEFAULT_K_MIN
    assert body["code_license"] == "AGPL-3.0-or-later"
    for key in ("framing", "pseudonymization_approach", "deidentification", "patient_rights"):
        assert key in body, key
