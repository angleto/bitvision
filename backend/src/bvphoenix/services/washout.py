"""Contrast wash-out quantification across the phases of one CT study.

Given the mean HU of one anatomical ROI sampled in each contrast phase,
compute the enhancement and the wash-out indices used to characterise a
lesion. Pure and side-effect-free; the API layer measures the per-phase HU
(``services.roi_sampling``) and calls in here.

Formulas and thresholds are NOT invented — they are the established adrenal
wash-out indices (also applied to other hypervascular lesions). With
U = unenhanced HU, E = enhanced HU (portal-venous, ~60-75 s), D = delayed
HU (~15 min):

    Absolute Percentage Washout  APW = 100 * (E - D) / (E - U)
    Relative Percentage Washout  RPW = 100 * (E - D) / E

Literature reference thresholds: APW >= 60% and RPW >= 40% are reported as
strongly associated with adrenal adenoma; an unenhanced attenuation < 10 HU
is itself diagnostic of a lipid-rich adenoma (wash-out then unnecessary).
Sources (PubMed): Korobkin et al., AJR 1998 (DOI 10.2214/ajr.170.3.9490968);
Szolar & Kammerhuber, Radiology 1998 (DOI 10.1148/radiology.207.2.9577483).

This module returns the COMPUTED NUMBERS and factual threshold comparisons
only. It makes no diagnosis: ``apw_ge_60`` is a numeric fact, not a verdict;
the clinical interpretation is the radiologist's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bvphoenix.db.models.dicom import ACQUISITION_PHASES

# Literature reference thresholds (see module docstring).
APW_ADENOMA_THRESHOLD = 60.0
RPW_ADENOMA_THRESHOLD = 40.0
UNENHANCED_LIPID_RICH_HU = 10.0

# Which classified phase plays each role in the wash-out formula, most
# preferred first. "Enhanced" is the early post-contrast peak (portal-venous
# in the adrenal protocol; arterial/corticomedullary as fallbacks);
# "delayed" is the late wash-out phase.
_ENHANCED_PREFERENCE = ("portal_venous", "arterial", "corticomedullary")
_DELAYED_PREFERENCE = ("delayed", "excretory", "hepatobiliary")
_PHASE_ORDER = {p: i for i, p in enumerate(ACQUISITION_PHASES)}


@dataclass(frozen=True)
class PhaseHu:
    acquisition_phase: str
    hu_mean: float
    time_offset_s: float | None = None


@dataclass
class WashoutResult:
    unenhanced_phase: str | None = None
    enhanced_phase: str | None = None
    delayed_phase: str | None = None
    unenhanced_hu: float | None = None
    enhanced_hu: float | None = None
    delayed_hu: float | None = None
    absolute_enhancement_hu: float | None = None  # E - U
    apw: float | None = None
    rpw: float | None = None
    apw_ge_60: bool | None = None
    rpw_ge_40: bool | None = None
    unenhanced_below_10hu: bool | None = None
    # HU-vs-phase time-attenuation curve, in canonical phase order.
    curve: list[PhaseHu] = field(default_factory=list)


def _pick(by_phase: dict[str, PhaseHu], preference: tuple[str, ...]) -> PhaseHu | None:
    for p in preference:
        if p in by_phase:
            return by_phase[p]
    return None


def compute_washout(points: list[PhaseHu]) -> WashoutResult:
    """Compute enhancement + APW/RPW from per-phase mean HU.

    ``points`` is one entry per classified phase (mean HU of the same ROI).
    Phases not present simply restrict which indices are computable: APW
    needs unenhanced + enhanced + delayed; RPW needs only enhanced +
    delayed. Returns NaN-free fields (``None`` when not computable).
    """
    by_phase: dict[str, PhaseHu] = {}
    for pt in points:
        # Keep the first occurrence of each phase (a study should carry one
        # series per phase; if duplicated, the earliest wins deterministically).
        by_phase.setdefault(pt.acquisition_phase, pt)

    curve = sorted(by_phase.values(), key=lambda p: _PHASE_ORDER.get(p.acquisition_phase, 99))
    res = WashoutResult(curve=curve)

    unenh = by_phase.get("unenhanced")
    enh = _pick(by_phase, _ENHANCED_PREFERENCE)
    dly = _pick(by_phase, _DELAYED_PREFERENCE)

    # The delayed phase must come after the enhanced phase in acquisition
    # order; otherwise the pair is not a valid wash-out measurement.
    if (
        enh
        and dly
        and _PHASE_ORDER.get(dly.acquisition_phase, 99)
        <= _PHASE_ORDER.get(enh.acquisition_phase, -1)
    ):
        dly = None

    if unenh:
        res.unenhanced_phase = unenh.acquisition_phase
        res.unenhanced_hu = unenh.hu_mean
        res.unenhanced_below_10hu = unenh.hu_mean < UNENHANCED_LIPID_RICH_HU
    if enh:
        res.enhanced_phase = enh.acquisition_phase
        res.enhanced_hu = enh.hu_mean
        if unenh:
            res.absolute_enhancement_hu = enh.hu_mean - unenh.hu_mean
    if dly:
        res.delayed_phase = dly.acquisition_phase
        res.delayed_hu = dly.hu_mean

    if enh and dly:
        e, d = enh.hu_mean, dly.hu_mean
        if unenh is not None:
            denom = e - unenh.hu_mean
            if abs(denom) > 1e-6:
                res.apw = 100.0 * (e - d) / denom
                res.apw_ge_60 = res.apw >= APW_ADENOMA_THRESHOLD
        if abs(e) > 1e-6:
            res.rpw = 100.0 * (e - d) / e
            res.rpw_ge_40 = res.rpw >= RPW_ADENOMA_THRESHOLD

    return res
