"""Golden cases for the contrast-phase classifier on REALISTIC multi-vendor
protocols (Italian + English descriptions, bolus-tracking timing).

These are not a real-patient golden (that needs a pointer to an actual
stored study and a live stack — the real-patient-100% rule still applies
there); they pin that the classifier labels well-formed real-world
protocol descriptions/timing correctly at high confidence, and that
genuinely ambiguous inputs degrade to needs_confirmation rather than a
confident wrong label.
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


def test_golden_liver_four_phase_english_with_bolus_timing() -> None:
    start = time(10, 0, 0)
    series = [
        SeriesPhaseInput(
            _sid(1),
            "CT",
            1,
            "LIVER",
            "CT Abdomen pre-contrast",
            acquisition_time_of_day=time(9, 59, 55),
            contrast_bolus_start_time=start,
        ),
        SeriesPhaseInput(
            _sid(2),
            "CT",
            2,
            "LIVER",
            "Liver late arterial",
            acquisition_time_of_day=time(10, 0, 35),
            contrast_bolus_agent="Iomeron 400",
            contrast_bolus_start_time=start,
        ),
        SeriesPhaseInput(
            _sid(3),
            "CT",
            3,
            "LIVER",
            "Liver portal venous",
            acquisition_time_of_day=time(10, 1, 12),
            contrast_bolus_agent="Iomeron 400",
            contrast_bolus_start_time=start,
        ),
        SeriesPhaseInput(
            _sid(4),
            "CT",
            4,
            "LIVER",
            "Liver delayed 3 min",
            acquisition_time_of_day=time(10, 3, 0),
            contrast_bolus_agent="Iomeron 400",
            contrast_bolus_start_time=start,
        ),
    ]
    r = _by_id(classify_study_phases(series))
    expected = {
        _sid(1): "unenhanced",
        _sid(2): "arterial",
        _sid(3): "portal_venous",
        _sid(4): "delayed",
    }
    for sid, phase in expected.items():
        assert r[sid].acquisition_phase == phase, f"{sid} -> {r[sid].acquisition_phase}"
        assert r[sid].confidence is not None and r[sid].confidence >= CONFIRM_THRESHOLD


def test_golden_liver_four_phase_italian() -> None:
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "FEGATO", "Addome basale"),
        SeriesPhaseInput(_sid(2), "CT", 2, "FEGATO", "Fase arteriosa"),
        SeriesPhaseInput(_sid(3), "CT", 3, "FEGATO", "Fase portale"),
        SeriesPhaseInput(_sid(4), "CT", 4, "FEGATO", "Fase tardiva"),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "unenhanced"
    assert r[_sid(2)].acquisition_phase == "arterial"
    assert r[_sid(3)].acquisition_phase == "portal_venous"
    assert r[_sid(4)].acquisition_phase == "delayed"


def test_golden_kidney_three_phase_italian() -> None:
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "RENE", "Fase corticomidollare"),
        SeriesPhaseInput(_sid(2), "CT", 2, "RENE", "Fase nefrografica"),
        SeriesPhaseInput(_sid(3), "CT", 3, "RENE", "Fase escretoria"),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "corticomedullary"
    assert r[_sid(2)].acquisition_phase == "nephrographic"
    assert r[_sid(3)].acquisition_phase == "excretory"


def test_golden_vendor_terse_abbreviations() -> None:
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "ABDOMEN", "ABD ART"),
        SeriesPhaseInput(_sid(2), "CT", 2, "ABDOMEN", "ABD PV"),
        SeriesPhaseInput(_sid(3), "CT", 3, "ABDOMEN", "ABD NC"),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "arterial"
    assert r[_sid(2)].acquisition_phase == "portal_venous"
    assert r[_sid(3)].acquisition_phase == "unenhanced"


def test_golden_eob_mr_hepatobiliary_description_only() -> None:
    # Gd-EOB hepatobiliary is description-driven (an MR concept); even on a
    # CT-typed series the keyword must classify it (CT timing never would).
    series = [SeriesPhaseInput(_sid(1), "CT", 1, "LIVER", "EOB hepatobiliary phase 20 min")]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "hepatobiliary"


def test_golden_ambiguous_degrades_to_confirmation() -> None:
    # Description says arterial but the true delay is portal-venous range:
    # a conflict must keep the description label BUT flag for confirmation,
    # never a confident wrong number.
    start = time(8, 0, 0)
    series = [
        SeriesPhaseInput(
            _sid(1),
            "CT",
            1,
            "LIVER",
            "arterial",
            acquisition_time_of_day=time(8, 1, 15),
            contrast_bolus_start_time=start,
        )
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(1)].acquisition_phase == "arterial"
    assert r[_sid(1)].needs_confirmation


def test_golden_real_study_addome_completo_descriptions() -> None:
    # Real series descriptions from a TC addome completo (study 70ce04b1):
    # only the true phases get a label; scout/recon/MPR/prep/screenshot don't,
    # so the viewer opens exactly the phases instead of arbitrary CT series.
    series = [
        SeriesPhaseInput(_sid(1), "CT", 1, "CHEST", "Scout"),
        SeriesPhaseInput(_sid(2), "CT", 2, "CHEST", "Basale"),
        SeriesPhaseInput(_sid(3), "CT", 3, "CHEST", "Polmone 1.25"),
        SeriesPhaseInput(_sid(9), "CT", 9, "CHEST", "tardiva dopo portale"),
        SeriesPhaseInput(_sid(99), "CT", 99, "CHEST", "Screen Save"),
        SeriesPhaseInput(_sid(200), "CT", 200, "CHEST", "Serie Prep Smart"),
        SeriesPhaseInput(_sid(300), "CT", 300, "CHEST", "SAG"),
        SeriesPhaseInput(_sid(301), "CT", 301, "CHEST", "COR"),
    ]
    r = _by_id(classify_study_phases(series))
    assert r[_sid(2)].acquisition_phase == "unenhanced"  # Basale
    assert r[_sid(9)].acquisition_phase == "delayed"  # tardiva (beats "portale")
    for junk in (_sid(1), _sid(3), _sid(99), _sid(200), _sid(300), _sid(301)):
        assert r[junk].acquisition_phase is None, f"{junk} should be unclassified"
