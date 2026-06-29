"""Thesaurus query-expansion mechanism + the 0039 clinical seed content.

These are DB-free: ``expand_tsquery`` is pure given a loaded cache, and the
0039 seed is plain data. The end-to-end "does 'fegato' return liver studies"
assertion lives in the DB-backed ``test_search.py`` (needs Postgres FTS).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

import bvphoenix.services.thesaurus as thesaurus
from bvphoenix.services.thesaurus import expand_tsquery


def _param_values(expr) -> set[str]:
    """The bound-parameter VALUES of the compiled tsquery.

    ``plainto_tsquery('italian', q)`` carries its config + query as binds; the
    query/synonym surface forms are exactly those param values. (We can't use
    ``literal_binds`` here — the regconfig literal has no literal renderer.)
    """
    compiled = expr.compile(dialect=postgresql.dialect())
    return {str(v) for v in compiled.params.values()}


@pytest.fixture
def _clean_cache(monkeypatch):
    # Isolate the module-level thesaurus cache per test.
    monkeypatch.setattr(thesaurus, "_synonyms", {}, raising=False)
    yield


def test_expand_is_noop_without_thesaurus(_clean_cache):
    values = _param_values(expand_tsquery("fegato"))
    assert "fegato" in values
    # No expansion loaded -> bare dual-config query, nothing else OR'd in.
    assert "liver" not in values


def test_expand_ors_in_synonym_variants(monkeypatch):
    monkeypatch.setattr(thesaurus, "_synonyms", {"fegato": ["liver", "hepatic"]})
    values = _param_values(expand_tsquery("fegato"))
    # Base term AND every variant are present in the OR'd tsquery.
    assert {"fegato", "liver", "hepatic"} <= values


def test_expand_tokenizes_multi_word_query(monkeypatch):
    monkeypatch.setattr(thesaurus, "_synonyms", {"colangio": ["mrcp"], "rm": ["mri"]})
    values = _param_values(expand_tsquery("RM colangio"))
    assert {"mrcp", "mri"} <= values


def test_expand_caps_variant_explosion(monkeypatch):
    # _MAX_VARIANTS guards against a pathological tsquery blow-up.
    monkeypatch.setattr(thesaurus, "_synonyms", {"x": [f"v{i}" for i in range(100)]})
    values = _param_values(expand_tsquery("x x x"))
    present = sum(1 for i in range(100) if f"v{i}" in values)
    assert present <= thesaurus._MAX_VARIANTS


# --- 0039 clinical seed content -------------------------------------------


def _load_migration_seed() -> dict[str, list[str]]:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0039_clinical_thesaurus_terms.py"
    )
    spec = importlib.util.spec_from_file_location("_mig0039", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._SEED


def test_seed_covers_the_reported_terms():
    seed = _load_migration_seed()
    # The four Italian terms the user reported as returning nothing.
    assert "liver" in seed["fegato"]
    assert "cancer" in seed["cancro"]
    assert {"cholangiography", "mrcp"} <= set(seed["colangio"])
    # Mammography must reach both the DICOM modality code and the organ terms
    # (the MG OpenData studies carry a null StudyDescription).
    assert {"mg", "breast"} <= set(seed["mammografia"])


def test_seed_has_reverse_pairs():
    seed = _load_migration_seed()
    assert "fegato" in seed["liver"]
    assert "cancro" in seed["cancer"]


def test_seed_terms_are_single_lowercase_tokens():
    # The expander looks up one [a-z0-9]+ token at a time, so a multi-word or
    # upper-case TERM key would never be matched. Variants may be multi-word.
    seed = _load_migration_seed()
    for term in seed:
        assert re.fullmatch(r"[a-z0-9]+", term), f"bad term key: {term!r}"
