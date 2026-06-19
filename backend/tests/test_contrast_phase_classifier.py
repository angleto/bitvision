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
