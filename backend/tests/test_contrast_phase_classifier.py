"""Pure unit tests for the contrast-phase classifier
(``services.contrast_phase``). No DB, no S3 — synthetic series metadata.

These pin the clinically-relevant behaviour:
* clear descriptions classify with high confidence;
* timing confirms a description (boost) or conflicts (degrade to confirm);
* timing alone yields a low-confidence candidate (needs confirmation);
* a series lacking its own contrast agent while siblings have it reads as
  unenhanced;
* nothing is ever *confidently* wrong — ambiguity degrades to confirm or
  to unknown, never to a high-confidence wrong label.
"""

from __future__ import annotations

import uuid
from datetime import time

from bvphoenix.services.contrast_phase import (
    CONFIRM_THRESHOLD,
    SeriesPhaseInput,
    classify_study_phases,
)


def _sid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _by_id(results):  # type: ignore[no-untyped-def]
    return {r.series_id: r for r in results}


def test_liver_four_phase_by_description() -> None:
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "LIVER", "Pre-contrast"),
        SeriesPhaseInput(_sid(2), "CT", 2, "LIVER", "Late arterial phase"),
        SeriesPhaseInput(_sid(3), "CT", 3, "LIVER", "Portal venous phase"),
        SeriesPhaseInput(_sid(4), "CT", 4, "LIVER", "Delayed phase 5 min"),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "unenhanced"
    assert r[_sid(2)].acquisition_phase == "arterial"
    assert r[_sid(3)].acquisition_phase == "portal_venous"
    assert r[_sid(4)].acquisition_phase == "delayed"
    for s in series:
        assert r[s.series_id].confidence is not None
        assert r[s.series_id].confidence >= CONFIRM_THRESHOLD
        assert not r[s.series_id].needs_confirmation


def test_description_plus_true_timing_boosts_confidence() -> None:
    start = time(8, 14, 0)
    series = [
        SeriesPhaseInput(
            _sid(1),
            "CT",
            1,
            "LIVER",
            "Arterial phase",
            acquisition_time_of_day=time(8, 14, 30),  # +30s
            contrast_bolus_agent="Iohexol",
            contrast_bolus_start_time=start,
        ),
        SeriesPhaseInput(
            _sid(2),
            "CT",
            2,
            "LIVER",
            "Portal venous",
            acquisition_time_of_day=time(8, 15, 10),  # +70s
            contrast_bolus_agent="Iohexol",
            contrast_bolus_start_time=start,
        ),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "arterial"
    assert r[_sid(2)].acquisition_phase == "portal_venous"
    # Agreement -> top confidence band.
    assert r[_sid(1)].confidence >= 0.9
    assert r[_sid(2)].confidence >= 0.9
    assert r[_sid(1)].time_offset_s == 30.0
    assert not r[_sid(1)].offset_is_relative


def test_description_timing_conflict_degrades_to_confirm() -> None:
    start = time(8, 14, 0)
    series = [
        SeriesPhaseInput(
            _sid(1),
            "CT",
            1,
            "LIVER",
            "Arterial phase",
            acquisition_time_of_day=time(8, 15, 10),  # +70s -> portal window
            contrast_bolus_agent="Iohexol",
            contrast_bolus_start_time=start,
        ),
    ]
    r = _by_id(classify_study_phases(series))
    # Description wins (authoritative) but the disagreement flags it.
    assert r[_sid(1)].acquisition_phase == "arterial"
    assert r[_sid(1)].confidence < CONFIRM_THRESHOLD
    assert r[_sid(1)].needs_confirmation


def test_timing_only_is_low_confidence_candidate() -> None:
    start = time(8, 14, 0)
    series = [
        SeriesPhaseInput(
            _sid(1),
            "CT",
            1,
            "LIVER",
            "Abdomen 1.0",  # no phase keyword
            acquisition_time_of_day=time(8, 14, 35),  # +35s -> arterial
            contrast_bolus_agent="Iohexol",
            contrast_bolus_start_time=start,
        ),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "arterial"
    assert r[_sid(1)].needs_confirmation  # timing-only -> confirm
    assert r[_sid(1)].confidence < CONFIRM_THRESHOLD


def test_unenhanced_detected_by_missing_agent() -> None:
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "LIVER", "Abdomen", contrast_bolus_agent=None),
        SeriesPhaseInput(_sid(2), "CT", 2, "LIVER", "Abdomen", contrast_bolus_agent="Iohexol"),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "unenhanced"
    # Agent-absence alone is vendor-dependent: a candidate to confirm,
    # never a trusted label.
    assert r[_sid(1)].needs_confirmation
    # The enhanced sibling with no description/timing stays unknown, not
    # confidently mislabelled.
    assert r[_sid(2)].acquisition_phase is None


def test_kidney_three_phase_by_description() -> None:
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "KIDNEY", "Corticomedullary phase"),
        SeriesPhaseInput(_sid(2), "CT", 2, "KIDNEY", "Nephrographic phase"),
        SeriesPhaseInput(_sid(3), "CT", 3, "KIDNEY", "Excretory phase"),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "corticomedullary"
    assert r[_sid(2)].acquisition_phase == "nephrographic"
    assert r[_sid(3)].acquisition_phase == "excretory"


def test_hepatobiliary_eob() -> None:
    series = [SeriesPhaseInput(_sid(1), "CT", 1, "LIVER", "Hepatobiliary phase (EOB) 20 min")]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "hepatobiliary"


def test_non_ct_series_is_unknown() -> None:
    series = [SeriesPhaseInput(_sid(1), "MR", 1, "LIVER", "Portal venous phase")]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase is None
    assert r[_sid(1)].confidence is None
    assert not r[_sid(1)].needs_confirmation  # unknown != candidate


def test_renal_window_used_for_renal_region() -> None:
    start = time(9, 0, 0)
    # +120s in the renal protocol is nephrographic; in the hepatic windows
    # it would be 'delayed'. Region must steer the window.
    series = [
        SeriesPhaseInput(
            _sid(1),
            "CT",
            1,
            "KIDNEY",
            "renal protocol",
            acquisition_time_of_day=time(9, 2, 0),
            contrast_bolus_agent="Iohexol",
            contrast_bolus_start_time=start,
        ),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "nephrographic"


def test_output_order_matches_input_order() -> None:
    series = [
        SeriesPhaseInput(_sid(3), "CT", 3, "LIVER", "Delayed"),
        SeriesPhaseInput(_sid(1), "CT", 1, "LIVER", "Pre-contrast"),
        SeriesPhaseInput(_sid(2), "MR", 2, "LIVER", "noise"),
    ]
    out = classify_study_phases(series)
    assert [r.series_id for r in out] == [_sid(3), _sid(1), _sid(2)]


def test_multiphase_protocol_name_does_not_contaminate() -> None:
    """A study-level ProtocolName that enumerates several phases
    ("Basale/Arteriosa-Venosa") must NOT stamp a single phase onto every
    series — it is ambiguous and ignored. Only the description-matched
    series get labelled; scouts / reformats / dose reports stay unknown.

    Reproduces the real bug where every Scout / SAG / COR / dose-report
    series inherited 'portal_venous' from the protocol's "Venosa".
    """
    proto = "5.2 Torace Addome Pelvi (Basale/Arteriosa-Venosa)"
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "CHEST", "Scout", protocol_name=proto),
        SeriesPhaseInput(_sid(2), "CT", 2, "CHEST", "Basale", protocol_name=proto),
        SeriesPhaseInput(_sid(3), "CT", 3, "CHEST", "Polmone 1.25", protocol_name=proto),
        SeriesPhaseInput(_sid(4), "CT", 9, "CHEST", "tardiva dopo portale", protocol_name=proto),
        SeriesPhaseInput(_sid(5), "CT", 300, "CHEST", "SAG", protocol_name=proto),
        SeriesPhaseInput(_sid(6), "CT", 301, "CHEST", "COR", protocol_name=proto),
        SeriesPhaseInput(_sid(7), "CT", 999, "CHEST", "Rapporto dose", protocol_name=proto),
    ]
    r = _by_id(classify_study_phases(series))
    # Only the two clearly-described phases are labelled.
    assert r[_sid(2)].acquisition_phase == "unenhanced"
    assert r[_sid(4)].acquisition_phase == "delayed"
    # Everything the protocol would have contaminated stays unknown.
    for sid in (_sid(1), _sid(3), _sid(5), _sid(6), _sid(7)):
        assert r[sid].acquisition_phase is None


def test_single_phase_protocol_name_is_a_fallback_signal() -> None:
    """When ProtocolName names exactly one phase and the description is
    silent, it is trusted as a (low) fallback signal."""
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "LIVER", "series 1", protocol_name="Late arterial"),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "arterial"


def test_localizer_capture_dose_prep_never_labelled() -> None:
    """Scout / Screen Save / Dose report / Smart-Prep are not a contrast
    phase: never labelled, even if a phase word appears in the text (so a
    'Serie Prep Smart venosa' cannot leak a portal label)."""
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "ABDOMEN", "Scout topogram"),
        SeriesPhaseInput(_sid(2), "CT", 2, "ABDOMEN", "Screen Save"),
        SeriesPhaseInput(_sid(3), "CT", 3, "ABDOMEN", "Rapporto dose"),
        SeriesPhaseInput(_sid(4), "CT", 4, "ABDOMEN", "Serie Prep Smart venosa"),
    ]
    r = _by_id(classify_study_phases(series))
    for s in series:
        assert r[s.series_id].acquisition_phase is None
