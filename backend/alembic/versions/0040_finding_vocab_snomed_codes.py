"""Map the Finding controlled vocabulary onto SNOMED CT.

P-followup of the annotation overhaul (Flow task 89536d5b). Migration 0020
seeded ``finding_types`` / ``anatomy_sites`` / ``morphology_terms`` with
``code``/``code_system`` deliberately NULL — "a curated follow-up, not a
fabricated guess". This migration fills that in.

Code system: **SNOMED CT** (FHIR-native, the system the platform's FHIR
export already speaks; interoperable + verifiable). Every concept id below
was verified against the live EBI Ontology Lookup Service (OLS4,
``ontology=snomed``); the label OLS4 returns is kept in the inline comment
as the audit trail. Anatomy → ``(body structure)`` roots; finding types →
``(morphologic abnormality)`` / ``(finding)``; morphology → the dedicated
SNOMED "radiographic lesion margin/shape characteristic" findings.

Coverage is deliberately PARTIAL: ``other`` (catch-all) and the imaging
morphology descriptors with no clean 1:1 SNOMED concept (smooth,
well_defined, solid, part_solid, ground_glass, calcified, cavitary) are left
NULL rather than mapped to an approximate code. The schema + the training
manifest already handle NULL codes; a future RadLex/BI-RADS pass can curate
the remainder.

Idempotent: each UPDATE is guarded ``WHERE code IS NULL`` so it never
clobbers a manually-curated code. Reversible: downgrade clears only the rows
this migration stamped with ``SNOMED-CT``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_finding_vocab_snomed_codes"
down_revision = "0039_clinical_thesaurus_terms"
branch_labels = None
depends_on = None

_CODE_SYSTEM = "SNOMED-CT"

# key -> SNOMED CT concept id (OLS4-verified label in the comment).
_FINDING_TYPE_CODES = {
    "nodule": "27925004",  # Nodule
    "mass": "4147007",  # Mass (morphologic abnormality)
    "cyst": "367643001",  # Cyst (morphologic abnormality)
    "lymph_node": "30746006",  # Lymphadenopathy
    "consolidation": "95436008",  # Lung consolidation
    "ground_glass_opacity": "1217294009",  # Ground glass lung opacity (finding)
    "effusion": "41699000",  # Effusion (morphologic abnormality)
    "edema": "79654002",  # Edema (morphologic abnormality)
    "fracture": "72704001",  # Fracture (morphologic abnormality)
    "calcification": "18115005",  # Pathologic calcification
    "hemorrhage": "50960005",  # Hemorrhage (morphologic abnormality)
    "infarct": "55641003",  # Infarct (morphologic abnormality)
    "aneurysm": "85659009",  # Aneurysm (morphologic abnormality)
    "stenosis": "415582006",  # Stenosis (morphologic abnormality)
    "lesion": "52988006",  # Lesion (morphologic abnormality)
    # "other" intentionally unmapped (catch-all).
}

_ANATOMY_CODES = {
    "liver": "10200004",  # Liver structure
    "spleen": "78961009",  # Splenic structure
    "pancreas": "15776009",  # Pancreatic structure
    "kidney": "64033007",  # Kidney structure
    "adrenal": "23451007",  # Adrenal structure
    "lung": "39607008",  # Lung structure
    "lung_upper_lobe": "45653009",  # Structure of upper lobe of lung
    "lung_middle_lobe": "72481006",  # Structure of middle lobe of right lung
    "lung_lower_lobe": "90572001",  # Structure of lower lobe of lung
    "mediastinum": "72410000",  # Mediastinal structure
    "breast": "76752008",  # Breast structure
    "prostate": "41216001",  # Prostatic structure
    "brain": "12738006",  # Brain structure
    "bone": "272673000",  # Bone structure
    "lymph_node_region": "59441001",  # Structure of lymph node
    "bladder": "89837001",  # Urinary bladder structure
    "thyroid": "69748006",  # Thyroid structure
    "bowel": "113276009",  # Intestinal structure
}

_MORPHOLOGY_CODES = {
    "spiculated": "129742005",  # Lesion with spiculated margin (finding)
    "lobulated": "129735005",  # Lobular shaped lesion
    "circumscribed": "129738007",  # Circumscribed lesion
    "irregular": "129736006",  # Irregular shaped lesion
    "ill_defined": "129741003",  # Indistinct lesion (ill-defined margin)
    "necrotic": "6574001",  # Necrosis (morphologic abnormality)
    # No clean SNOMED concept (left NULL, not guessed): smooth, well_defined,
    # cavitary, solid, part_solid, ground_glass, calcified.
}

_TABLES = {
    "finding_types": _FINDING_TYPE_CODES,
    "anatomy_sites": _ANATOMY_CODES,
    "morphology_terms": _MORPHOLOGY_CODES,
}


def upgrade() -> None:
    for table, mapping in _TABLES.items():
        stmt = sa.text(
            f"UPDATE {table} SET code_system = :cs, code = :code WHERE key = :key AND code IS NULL"
        )
        for key, code in mapping.items():
            op.execute(stmt.bindparams(cs=_CODE_SYSTEM, code=code, key=key))


def downgrade() -> None:
    for table, mapping in _TABLES.items():
        stmt = sa.text(
            f"UPDATE {table} SET code_system = NULL, code = NULL "
            "WHERE key = :key AND code_system = :cs"
        )
        for key in mapping:
            op.execute(stmt.bindparams(key=key, cs=_CODE_SYSTEM))
