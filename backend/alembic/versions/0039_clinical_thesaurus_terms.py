"""Enrich the radiology synonym thesaurus with clinical IT/EN/code terms.

Free-text study search (``/api/search``) is lexical over study/series
descriptions plus, since this change, the modality code and body part. The
descriptions are frequently English, coded, or null (TCIA/IDC OpenData), so
an Italian query like "fegato", "cancro", "colangio" or "mammografia" matched
nothing even though the exams exist (modality MG, body_part LIVER/BREAST,
tag organ:liver, ...). The original seed (0013) covered only modality
acronyms and a handful of regions.

This migration adds a curated, bilingual (IT<->EN) + code core: common
anatomy, the oncology vocabulary, and the cholangio / mammography families,
so the query side OR-expands an Italian term into its English / DICOM-code
equivalents (e.g. "mammografia" -> {mammography, mg, breast, mammella}).
Hand-built and conservative; RadLex/UMLS mapping stays the upgrade path.

Idempotent: a term absent from ``search_synonyms`` is inserted; a term
already present (e.g. "mammografia" from 0013) has its variants UNION-merged,
so this never clobbers a hand-edited row and re-running is a no-op. Touched
rows are stamped version 2 so the eval harness can pin the snapshot.

Revision ID: 0039_clinical_thesaurus_terms
Revises: 0038_event_document_links
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op

revision = "0039_clinical_thesaurus_terms"
down_revision = "0038_event_document_links"
branch_labels = None
depends_on = None

_THESAURUS_VERSION = 2

# term -> equivalent surface forms OR'd into the FTS query. Terms are single
# tokens (the query expander looks up one [a-z0-9]+ token at a time); variants
# may be multi-word (plainto_tsquery splits them). Symmetric pairs are listed
# both ways so expansion works whichever side is typed.
_SEED: dict[str, list[str]] = {
    # --- anatomy: IT <-> EN (+ adjective forms) ---
    "fegato": ["liver", "hepatic", "epatico", "epatica"],
    "liver": ["fegato", "epatico", "epatica", "hepatic"],
    "epatico": ["liver", "fegato", "hepatic"],
    "polmone": ["lung", "polmonare", "pulmonary"],
    "polmoni": ["lungs", "lung", "polmonare"],
    "lung": ["polmone", "polmonare", "pulmonary"],
    "rene": ["kidney", "renale", "renal"],
    "reni": ["kidneys", "kidney", "renale"],
    "kidney": ["rene", "renale", "renal"],
    "mammella": ["breast", "mammario", "mammaria", "seno"],
    "seno": ["breast", "mammella", "mammografia"],
    "breast": ["mammella", "seno", "mammario", "mammography"],
    "prostata": ["prostate", "prostatico"],
    "prostate": ["prostata", "prostatico"],
    "pancreas": ["pancreatico", "pancreatic"],
    "milza": ["spleen", "splenico"],
    "spleen": ["milza", "splenico"],
    "cervello": ["brain", "encefalo", "cerebrale"],
    "brain": ["cervello", "encefalo", "cerebrale"],
    "tiroide": ["thyroid", "tiroideo"],
    "thyroid": ["tiroide", "tiroideo"],
    "vescica": ["bladder", "vescicale"],
    "bladder": ["vescica"],
    "surrene": ["adrenal", "surrenalico"],
    "linfonodo": ["lymph node", "linfonodale", "lymph"],
    "linfonodi": ["lymph nodes", "lymph node", "linfonodale"],
    "biliari": ["biliary", "biliare", "bile duct", "vie biliari"],
    "biliare": ["biliary", "bile duct"],
    "colecisti": ["gallbladder", "cistifellea"],
    "cistifellea": ["gallbladder", "colecisti"],
    "abdomen": ["addome", "addominale"],
    "addominale": ["abdomen", "addome"],
    "chest": ["torace", "toracico", "thorax"],
    # --- oncology ---
    "cancro": ["cancer", "tumore", "tumor", "neoplasia", "neoplasm", "carcinoma", "maligno"],
    "cancer": ["cancro", "tumore", "tumor", "neoplasia", "neoplasm", "carcinoma"],
    "tumore": ["tumor", "cancro", "cancer", "neoplasia", "massa", "mass"],
    "tumor": ["tumore", "cancro", "cancer", "neoplasia", "mass"],
    "neoplasia": ["neoplasm", "tumore", "tumor", "cancro", "cancer"],
    "neoplasm": ["neoplasia", "tumor", "cancer"],
    "carcinoma": ["cancro", "cancer", "tumore"],
    "metastasi": ["metastasis", "metastases", "mets", "metastatico", "metastatic"],
    "metastasis": ["metastasi", "mets", "metastatic"],
    "linfoma": ["lymphoma"],
    "lymphoma": ["linfoma"],
    "nodulo": ["nodule", "nodular", "nodulare"],
    "nodule": ["nodulo", "nodular"],
    "massa": ["mass", "tumore", "lesione"],
    "mass": ["massa", "tumore", "tumor"],
    "lesione": ["lesion"],
    "lesion": ["lesione"],
    # --- procedures / regions / the cholangio + mammography families ---
    "colangio": [
        "cholangiography",
        "cholangio",
        "mrcp",
        "colangiografia",
        "colangiopancreatografia",
        "biliary",
    ],
    "colangiografia": ["cholangiography", "mrcp", "cholangio"],
    "mrcp": ["colangio", "cholangiography", "magnetic resonance cholangiopancreatography"],
    "cholangiography": ["colangio", "colangiografia", "mrcp"],
    "colangiocarcinoma": ["cholangiocarcinoma", "colangio"],
    "cholangiocarcinoma": ["colangiocarcinoma", "colangio"],
    # "mammografia" already exists from 0013 ([mammography, mammogram]); the
    # UNION-merge below adds the DICOM modality code and the IT/EN organ terms
    # so the search bar reaches MG studies that carry no description.
    "mammografia": ["mammography", "mammogram", "mg", "mammella", "breast", "seno"],
    "biopsia": ["biopsy"],
    "stadiazione": ["staging", "restaging", "ristadiazione"],
    "staging": ["stadiazione", "restaging", "ristadiazione"],
    "restaging": ["ristadiazione", "stadiazione", "staging"],
    "sorveglianza": ["surveillance", "follow-up", "followup", "controllo"],
    "surveillance": ["sorveglianza", "follow-up", "controllo"],
}


def _arr(variants: list[str]) -> str:
    """Postgres text[] literal with each element double-quoted/escaped."""
    return (
        "{"
        + ",".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in variants)
        + "}"
    )


def upgrade() -> None:
    for term, variants in _SEED.items():
        arr = _arr(variants)
        safe_term = term.replace("'", "''")
        # Insert when absent (the table was created in 0013).
        op.execute(
            "INSERT INTO public.search_synonyms (term, variants, version) "
            f"SELECT '{safe_term}', '{arr}'::text[], {_THESAURUS_VERSION} "
            "WHERE NOT EXISTS "
            f"(SELECT 1 FROM public.search_synonyms WHERE lower(term) = '{safe_term}')"
        )
        # Enrich when present: UNION the variant sets (dedup), bump version.
        # array(select distinct unnest(...)) keeps it order-insensitive and
        # idempotent on re-run.
        op.execute(
            "UPDATE public.search_synonyms SET "
            f"variants = ARRAY(SELECT DISTINCT unnest(variants || '{arr}'::text[])), "
            f"version = GREATEST(version, {_THESAURUS_VERSION}) "
            f"WHERE lower(term) = '{safe_term}'"
        )


def downgrade() -> None:
    # Remove the rows this migration introduced. Rows that predate it
    # (the 0013 seed) are left in place; their UNION-merged variants are not
    # un-merged (a synonym superset is harmless and downgrade is rare).
    pre_existing = {
        "tc",
        "ct",
        "rm",
        "mri",
        "rx",
        "eco",
        "ecografia",
        "mdc",
        "contrasto",
        "ggo",
        "pet",
        "torace",
        "addome",
        "encefalo",
        "mammografia",
    }
    to_delete = [t for t in _SEED if t not in pre_existing]
    for term in to_delete:
        safe_term = term.replace("'", "''")
        op.execute(f"DELETE FROM public.search_synonyms WHERE lower(term) = '{safe_term}'")
