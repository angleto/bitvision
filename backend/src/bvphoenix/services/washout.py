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


@dataclass(frozen=True)
class PhaseRelativeHu:
    """Lesion HU minus reference-parenchyma HU in one phase.

    For a liver lesion the clinical "wash-out" is *relative to the
    surrounding parenchyma* (LI-RADS), not the adrenal APW/RPW formula: the
    lesion turning hypodense versus liver in the portal/delayed phase
    (``delta_hu`` going negative) IS the qualitative wash-out.
    ``delta_hu = lesion_hu - parenchyma_hu``.
    """

    acquisition_phase: str
    lesion_hu: float
    parenchyma_hu: float
    delta_hu: float


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
    # The anatomical region the operator scoped the measurement to (adrenal /
    # liver / None=other). Drives which indices/flags are clinically valid.
    region: str | None = None
    # HU-vs-phase time-attenuation curve, in canonical phase order.
    curve: list[PhaseHu] = field(default_factory=list)
    # Reference-parenchyma HU per phase (liver workflow), same phase order.
    parenchyma_curve: list[PhaseHu] = field(default_factory=list)
    # Lesion-minus-parenchyma HU per phase (the liver wash-out signal).
    relative_curve: list[PhaseRelativeHu] = field(default_factory=list)


def _pick(by_phase: dict[str, PhaseHu], preference: tuple[str, ...]) -> PhaseHu | None:
    for p in preference:
        if p in by_phase:
            return by_phase[p]
    return None


def compute_washout(
    points: list[PhaseHu],
    *,
    region: str | None = None,
    parenchyma: list[PhaseHu] | None = None,
) -> WashoutResult:
    """Compute enhancement + wash-out from per-phase mean HU, region-aware.

    ``points`` is one entry per classified phase (mean HU of the lesion ROI).
    APW needs unenhanced + enhanced + delayed; RPW needs enhanced + delayed.
    Returns NaN-free fields (``None`` when not computable).

    ``region`` scopes the *interpretation* (the maths is identical, the
    clinical meaning is not):

    * ``"adrenal"`` — the masses APW/RPW were validated on. The adenoma
      threshold flags (``apw_ge_60`` / ``rpw_ge_40`` / ``unenhanced_below_10hu``)
      are emitted.
    * ``"liver"`` — APW/RPW do NOT apply (liver wash-out is qualitative,
      relative to parenchyma per LI-RADS). The adrenal indices AND flags are
      withheld; pass ``parenchyma`` (per-phase reference-ROI HU) to populate
      the lesion-vs-parenchyma ``relative_curve`` instead.
    * ``None`` / other — the raw APW/RPW numbers are returned as
      adrenal-derived reference values but with NO verdict flags (they are
      only meaningful for adrenal masses).

    ``parenchyma`` is an optional second ROI sampled in the same phases (the
    liver reference); when present, ``relative_curve`` carries the
    lesion-minus-parenchyma HU per phase regardless of region.
    """
    by_phase: dict[str, PhaseHu] = {}
    for pt in points:
        # Keep the first occurrence of each phase (a study should carry one
        # series per phase; if duplicated, the earliest wins deterministically).
        by_phase.setdefault(pt.acquisition_phase, pt)

    curve = sorted(by_phase.values(), key=lambda p: _PHASE_ORDER.get(p.acquisition_phase, 99))
    res = WashoutResult(curve=curve, region=region)

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
        if abs(e) > 1e-6:
            res.rpw = 100.0 * (e - d) / e

    # Region-scoped interpretation. The APW/RPW *numbers* are arithmetic; the
    # adenoma *verdict* flags (and the "<10 HU lipid-rich" flag) are only valid
    # for adrenal masses, so they are emitted for adrenal only. Liver discards
    # the adrenal indices entirely in favour of the parenchyma-relative read.
    if region == "adrenal":
        if res.apw is not None:
            res.apw_ge_60 = res.apw >= APW_ADENOMA_THRESHOLD
        if res.rpw is not None:
            res.rpw_ge_40 = res.rpw >= RPW_ADENOMA_THRESHOLD
        if unenh is not None:
            res.unenhanced_below_10hu = unenh.hu_mean < UNENHANCED_LIPID_RICH_HU
    elif region == "liver":
        res.apw = None
        res.rpw = None

    # Lesion-vs-parenchyma relative enhancement — the actual liver wash-out
    # signal. A plain factual comparison, so computed whenever a parenchyma
    # ROI was sampled, independent of region.
    if parenchyma:
        p_by_phase: dict[str, PhaseHu] = {}
        for pt in parenchyma:
            p_by_phase.setdefault(pt.acquisition_phase, pt)
        res.parenchyma_curve = sorted(
            p_by_phase.values(), key=lambda p: _PHASE_ORDER.get(p.acquisition_phase, 99)
        )
        rel: list[PhaseRelativeHu] = []
        for lp in curve:
            pp = p_by_phase.get(lp.acquisition_phase)
            if pp is not None:
                rel.append(
                    PhaseRelativeHu(
                        acquisition_phase=lp.acquisition_phase,
                        lesion_hu=lp.hu_mean,
                        parenchyma_hu=pp.hu_mean,
                        delta_hu=lp.hu_mean - pp.hu_mean,
                    )
                )
        res.relative_curve = rel

    return res
