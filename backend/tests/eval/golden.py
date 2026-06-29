"""Golden set for the search relevance harness.

A small, fixed, seeded corpus + graded queries that pin the behaviours
the search must never regress on. Kept deterministic (no model, no
randomness) so the lexical-relevance gate runs in CI without the heavy
``ai`` extra: every query here is answerable by the dual-config FTS
path of ``/api/search`` alone.

Each query asserts an *exact* expectation (recall@10 == 1.0 for the
unambiguous ones) rather than "X of N" — per the project rule that a
golden test on known data must be 100%, never a softened fraction.

Markers (``evalNN``) are unique alphanumeric tokens embedded in each
description: the fixture maps ``marker -> study_id`` at seed time, and
the same token doubles as the cross-patient probe (a foreign user
searching it must get zero of this corpus back).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StudySpec:
    marker: str
    description: str
    modality: str = "CT"
    body_part: str = "CHEST"


@dataclass(frozen=True)
class GoldenQuery:
    name: str
    q: str | None
    relevant_markers: frozenset[str]
    modality: str | None = None
    body_part: str | None = None


# Owned by the corpus patient. Italian prose + English/DICOM acronyms.
CORPUS: tuple[StudySpec, ...] = (
    StudySpec("evalm1", "TC torace con mezzo di contrasto evalm1", "CT", "CHEST"),
    StudySpec("evalm2", "Nodulo al polmone destro evalm2", "CT", "CHEST"),
    StudySpec("evalm3", "RM encefalo sequenza T2 FLAIR evalm3", "MR", "HEAD"),
    StudySpec("evalm4", "Ecografia addome completo evalm4", "US", "ABDOMEN"),
    StudySpec("evalm5", "Radiografia del torace evalm5", "CR", "CHEST"),
    StudySpec("evalm6", "Mammografia bilaterale evalm6", "MG", "BREAST"),
)

QUERIES: tuple[GoldenQuery, ...] = (
    # Italian stemming: plural query -> singular description.
    GoldenQuery("stem_polmoni", "polmoni", frozenset({"evalm2"})),
    # Exact acronym preserved by the 'simple' half of the dual config.
    GoldenQuery("acronym_flair", "FLAIR", frozenset({"evalm3"})),
    # "torace" hits two CHEST studies by description (evalm1, evalm5) AND, via
    # the thesaurus (torace->chest) + structured-field matching, every study
    # whose body_part is CHEST (adds evalm2) — free text now reaches the
    # structured fields, not just the description.
    GoldenQuery("term_torace", "torace", frozenset({"evalm1", "evalm2", "evalm5"})),
    # Single-doc exact phrase token.
    GoldenQuery("term_contrasto", "contrasto", frozenset({"evalm1"})),
    # Structured filter, no free text: every CT study, nothing else.
    GoldenQuery("filter_ct", None, frozenset({"evalm1", "evalm2"}), modality="CT"),
    # The acronym "CT" reaches evalm1 by description (corpus says "TC", bridged
    # by the thesaurus TC<->CT) and BOTH CT studies by the modality-code match
    # in free text — so it converges with the structured CT filter.
    GoldenQuery("synonym_ct", "CT", frozenset({"evalm1", "evalm2"})),
    # Free text reaches a study whose only signal is its DICOM modality code:
    # "mammografia" -> {mammography, MG, breast} (thesaurus) matches evalm6 by
    # description, by modality (MG), and by body_part (BREAST). This is the
    # user-reported scenario (an Italian term finding an MG study).
    GoldenQuery("freetext_mammografia", "mammografia", frozenset({"evalm6"})),
    # The English organ term reaches the BREAST study by body_part alone (its
    # description carries no English token).
    GoldenQuery("freetext_breast", "breast", frozenset({"evalm6"})),
)

# A foreign user issuing this query must receive ZERO of the corpus
# studies back (cross-patient isolation is a security invariant, asserted
# at recall@infinity, not merely top-k).
CROSS_PATIENT_PROBE = "evalm2"
