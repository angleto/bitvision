"""Public, machine-readable data-governance policy.

``GET /api/governance`` returns the *applied* governance policy as a
versioned descriptor: the de-identification passes, the k-anonymity
threshold, the contribution-tier model, the licences, and the patient
rights the platform honours. It is the auditable, open counterpart to a
closed irreversible black-box (the differentiator vs an institutional
data lake): anyone can read exactly what governance is applied, and pin
a version.

Public + pure (no DB, no auth, storage-isolated): the payload is a
description of policy, not data. Values are sourced from the real
constants the runtime enforces (``k_anonymity.DEFAULT_K_MIN``,
``deid_text._KIND_TO_PATTERN``) so the published policy cannot drift
from the code; the conformance test pins this.

GUARD-RAIL (load-bearing, see docs/data-governance.md): bitvision applies
*pseudonymization* + tiering + k-anonymity + auditable redaction. This is
deliberately NOT a claim of irreversible-anonymization parity — the one
asset class where an institutional lake is strong but closed. Overclaiming
here would be inaccurate. The framing field states this explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from bvphoenix.config import Settings, get_settings
from bvphoenix.services.deid_text import _KIND_TO_PATTERN
from bvphoenix.services.k_anonymity import DEFAULT_K_MIN
from bvphoenix.services.rate_limit import SEARCH_SEMANTIC_LIMIT, limiter

router = APIRouter(tags=["governance"])

# Bump when the governance POLICY changes (a new de-id pass, a different
# k threshold semantics, a tier redefinition). Independent of the app
# release version. Pinned to the schema/code by test_governance_policy.
GOVERNANCE_POLICY_VERSION = 1

_FRAMING = (
    "bitvision applies tiered pseudonymization, k-anonymity (k>=5) on training "
    "cohorts, and auditable text + DICOM de-identification. This is "
    "PSEUDONYMIZATION, not irreversible anonymization: the platform can "
    "re-identify within its own trust boundary, the patient retains ownership, "
    "portability (PHR-Bundle / GDPR Art. 20) and revocable consent. We do not "
    "claim parity with one-way irreversible anonymization."
)

_CONTRIBUTION_TIERS = {
    "t1": "Private — owner-only, never leaves the patient's control.",
    "t2": "Shared — disclosed to specific recipients via revocable share links.",
    "t3": "Training opt-in — de-identified, k-anonymized, usable in training cohorts.",
    "t4": "Public — released to the OpenData commons under a CC licence.",
}

_PATIENT_RIGHTS = {
    "ownership": "The patient owns their record; studies are theirs, not the institution's.",
    "portability": "One-click PHR-Bundle export (open container) / GDPR Art. 20.",
    "erasure": "GDPR Art. 17 erasure request; public releases transfer to an anonymous subject.",
    "consent": "Per-purpose consent (research / commercial / AI training), revocable; "
    "revocation propagates to future cohorts.",
}


class DeidentificationPolicy(BaseModel):
    text_regex_categories: list[str] = Field(
        description="PHI categories redacted from clinical-note text by regex passes, "
        "sourced from the runtime rule table."
    )
    text_llm_scrub: str = Field(
        description="Optional LLM scrub pass over note text; the model + provider are "
        "recorded per redaction event for audit."
    )
    dicom_header: str = Field(
        description="DICOM tag de-identification aligned to the PS3.15 Basic Application "
        "Level Confidentiality Profile (in-house table-driven engine)."
    )
    dicom_pixel: str = Field(
        description="Burned-in pixel PHI is gated conservatively on the public path; "
        "OCR + regex redaction tiers; hardening ongoing."
    )
    faces: str = Field(
        description="Face de-identification available for head/neck volumes before public release."
    )
    pathology_wsi: str = Field(
        description="Whole-slide label / macro images carrying scanner-printed PHI are "
        "de-identified before public release."
    )


class GovernancePolicyOut(BaseModel):
    policy_version: int
    app_version: str
    generated_at: str
    framing: str
    code_license: str
    opendata_default_license: str
    pseudonymization_approach: str
    k_anonymity_min: int
    contribution_tiers: dict[str, str]
    patient_rights: dict[str, str]
    deidentification: DeidentificationPolicy
    references: dict[str, str]


def build_governance_policy(settings: Settings) -> GovernancePolicyOut:
    """Pure assembly from the runtime constants. No DB, no secrets."""
    return GovernancePolicyOut(
        policy_version=GOVERNANCE_POLICY_VERSION,
        app_version=settings.app_version or "dev",
        generated_at=datetime.now(UTC).isoformat(),
        framing=_FRAMING,
        code_license="AGPL-3.0-or-later",
        opendata_default_license="CC-BY-4.0",
        pseudonymization_approach="tier-based pseudonymization + k-anonymity + auditable redaction",
        k_anonymity_min=DEFAULT_K_MIN,
        contribution_tiers=dict(_CONTRIBUTION_TIERS),
        patient_rights=dict(_PATIENT_RIGHTS),
        deidentification=DeidentificationPolicy(
            text_regex_categories=[kind for kind, _pat, _placeholder in _KIND_TO_PATTERN],
            text_llm_scrub="optional, model + provider recorded per redaction event",
            dicom_header="PS3.15 Basic Application Level Confidentiality Profile",
            dicom_pixel="conservative public-path gate; OCR + regex redaction tiers",
            faces="available for head/neck volumes before public release",
            pathology_wsi="label / macro de-identification before public release",
        ),
        references={
            "governance_dossier": "docs/data-governance.md",
            "phr_bundle": "docs/phr-bundle.md",
            "deidentification_provenance": "GET /api/studies/{id}/deidentification-provenance",
        },
    )


@router.get(
    "/governance",
    response_model=GovernancePolicyOut,
    summary="Public applied data-governance policy (versioned)",
)
@limiter.limit(SEARCH_SEMANTIC_LIMIT)
async def governance_policy(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> GovernancePolicyOut:
    """Return the applied data-governance policy. Public, no auth: it
    describes policy, not data. Rate-limited at the semantic-search tier."""
    return build_governance_policy(settings)
