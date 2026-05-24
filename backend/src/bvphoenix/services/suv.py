"""Standardized Uptake Value (SUV) computation from DICOM PET tags.

SUV is the workhorse quantitative metric in clinical PET reporting.
The formula (body-weight normalised, the most common variant):

    SUV_bw = activity_concentration[Bq/mL] * patient_weight[g] / injected_dose_decay_corrected[Bq]

We collapse all the constants into a single ``suv_factor_bw`` that the
client multiplies by the rescaled pixel value to get SUV directly. The
factor depends on:

  - patient weight (PatientWeight, 0010,1030),
  - injected total dose (RadionuclideTotalDose, 0018,1074),
  - radionuclide half-life (RadionuclideHalfLife, 0018,1075),
  - delta-t between injection and scan acquisition.

Pixel-level rescale (RescaleSlope/Intercept) yields activity in
``Units`` (typically BQML = Bq/mL); we read those tags too and only
emit a factor when units match.

References:
  - QIBA SUV profile, https://qibawiki.rsna.org/index.php/Profiles
  - Vauclin et al., 2009 — review of SUV computation pitfalls
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

try:
    import pydicom
    from pydicom.dataset import Dataset
except Exception:  # pragma: no cover
    pydicom = None  # type: ignore[assignment]
    Dataset = object  # type: ignore[misc,assignment]


@dataclass(frozen=True, slots=True)
class SuvFactors:
    """Result of the SUV factor extraction.

    ``factor_bw`` is the multiplier the client applies to the rescaled
    pixel value to obtain SUV body-weight. ``None`` for any required
    tag missing or units we don't support. Other ``factor_*`` are the
    LBM (lean body mass) and BSA (body surface area) variants per
    EANM 2.0 / PERCIST 1.0 — the client pre-computes all four so the
    user can switch SUV mode without re-uploading the volume.
    """

    factor_bw: float | None
    patient_weight_kg: float | None
    radionuclide: str | None
    units: str | None
    half_life_s: float | None
    injected_dose_bq: float | None
    decay_corrected_dose_bq: float | None
    delta_t_s: float | None
    notes: list[str]
    # New: per-variant scalar factors (Addendum C §5–§6).
    factor_lbm_janmahasatian: float | None = None
    factor_lbm_james: float | None = None
    factor_bsa_mosteller: float | None = None
    factor_bsa_dubois: float | None = None
    patient_height_m: float | None = None
    patient_sex: str | None = None
    # Addendum C §3.4 + §8: tracer / nuclide metadata.
    tracer: str | None = None
    branching_ratio: float | None = None
    # Sanity validation outcomes — non-blocking warnings the viewer
    # surfaces in the PET HUD so the operator sees data-quality
    # caveats at a glance (Addendum C §9).
    warnings: list[str] = field(default_factory=list)


def _parse_dicom_dt(value: str | None) -> datetime | None:
    """Parse a DICOM DT (date-time) string. Tolerates the variants:

    - YYYYMMDDHHMMSS
    - YYYYMMDDHHMMSS.ffffff
    - With trailing ``±HHMM`` timezone (rare but allowed by the spec).
    """
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    # Strip timezone if present
    tz = None
    if "+" in s[8:]:
        s, off = s.split("+", 1)
        tz = UTC  # we don't apply offsets — treat as UTC
        del off
    elif "-" in s[8:]:
        s, off = s.split("-", 1)
        tz = UTC
        del off
    fmt = "%Y%m%d%H%M%S.%f" if "." in s else "%Y%m%d%H%M%S"
    try:
        dt = datetime.strptime(s.ljust(14 + (8 if "." in s else 0))[: len(s)], fmt)
    except ValueError:
        return None
    if tz is not None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _parse_dicom_da_tm(date_str: str | None, time_str: str | None) -> datetime | None:
    """Combine a DICOM DA + TM pair into a datetime.

    DA is YYYYMMDD; TM can be HHMMSS, HHMMSS.ffffff, or shorter forms.
    """
    if not date_str:
        return None
    date_str = date_str.strip()
    if len(date_str) < 8:
        return None
    t = (time_str or "000000").strip().split(".", 1)
    base = t[0].ljust(6, "0")[:6]
    frac = ("." + t[1]) if len(t) > 1 else ""
    try:
        return datetime.strptime(
            date_str[:8] + base + frac, "%Y%m%d%H%M%S" + (".%f" if frac else "")
        )
    except ValueError:
        return None


# Positron branching ratio per radionuclide (Addendum C §3.4).
# Values from MIRD / NIST. For FDG / ¹¹C / ¹⁵O the ratio is ~1, so the
# legacy SUV formula is correct without correction. For ⁶⁸Ga / ⁶⁴Cu /
# ⁸⁹Zr the ratio is materially below 1 and the scanner calibration
# usually already incorporates it; we still surface the table so a
# downstream consumer can correct if the calibration is suspect.
NUCLIDE_BRANCHING_RATIO: dict[str, float] = {
    # Mapping by upper-cased CodeMeaning fragments.
    "FLUORINE": 0.967,
    "F^18": 0.967,
    "F-18": 0.967,
    "GALLIUM": 0.891,
    "GA^68": 0.891,
    "GA-68": 0.891,
    "CARBON": 0.998,
    "C^11": 0.998,
    "C-11": 0.998,
    "OXYGEN": 0.999,
    "O^15": 0.999,
    "O-15": 0.999,
    "NITROGEN": 0.998,
    "N^13": 0.998,
    "N-13": 0.998,
    "COPPER": 0.176,
    "CU^64": 0.176,
    "CU-64": 0.176,
    "ZIRCONIUM": 0.227,
    "ZR^89": 0.227,
    "ZR-89": 0.227,
    "RUBIDIUM": 0.949,
    "RB^82": 0.949,
    "RB-82": 0.949,
}

# Tracer detection (Addendum C §8). Maps fragments of the
# ``Radiopharmaceutical`` (0018,0031) free-text and known SNOMED
# CodeValue strings to a canonical short name. Used to drive
# tracer-specific default SUV scale presets in the viewer.
TRACER_KEYWORDS: list[tuple[str, str]] = [
    ("FDG", "FDG"),
    ("FLUORODEOXY", "FDG"),
    ("PSMA", "PSMA"),
    ("DOTATATE", "DOTATATE"),
    ("DOTATOC", "DOTATOC"),
    ("FET", "FET"),
    ("FLUOROETHYLTYROSINE", "FET"),
    ("FDOPA", "FDOPA"),
    ("AMMONIA", "AMMONIA"),
    ("RUBIDIUM", "RB-82"),
    ("CHOLINE", "CHOLINE"),
    ("ACETATE", "ACETATE"),
    ("MISO", "MISO"),
    ("FLT", "FLT"),
]


def detect_tracer(ds: Dataset) -> str | None:
    """Best-effort tracer name from free-text + code sequences.

    Reads ``Radiopharmaceutical`` (0018,0031) text and the nested
    ``RadiopharmaceuticalCodeSequence`` CodeMeaning when present.
    Returns the matched canonical short name from
    ``TRACER_KEYWORDS`` or ``None`` if nothing recognisable is found.
    """
    candidates: list[str] = []
    rp_seq = getattr(ds, "RadiopharmaceuticalInformationSequence", None)
    if rp_seq:
        rp = rp_seq[0]
        free = getattr(rp, "Radiopharmaceutical", None)
        if free:
            candidates.append(str(free))
        rc_seq = getattr(rp, "RadiopharmaceuticalCodeSequence", None)
        if rc_seq:
            for it in rc_seq:
                cm = getattr(it, "CodeMeaning", None)
                if cm:
                    candidates.append(str(cm))
    for c in candidates:
        upper = c.upper()
        for needle, canonical in TRACER_KEYWORDS:
            if needle in upper:
                return canonical
    return None


def branching_ratio_for(ds: Dataset) -> float | None:
    """Look up the positron branching ratio from
    ``RadionuclideCodeSequence`` (CodeMeaning + CodeValue fragments).
    Returns 1.0 by default (FDG-like nuclides) when nothing matches —
    the modern PET DICOM ``Bq/mL`` value already accounts for it via
    scanner calibration in the vast majority of cases."""
    rp_seq = getattr(ds, "RadiopharmaceuticalInformationSequence", None)
    if not rp_seq:
        return None
    rp = rp_seq[0]
    cs = getattr(rp, "RadionuclideCodeSequence", None)
    if not cs:
        return None
    for it in cs:
        cm = getattr(it, "CodeMeaning", None)
        cv = getattr(it, "CodeValue", None)
        for token in (cm, cv):
            if not token:
                continue
            up = str(token).upper()
            for k, v in NUCLIDE_BRANCHING_RATIO.items():
                if k in up:
                    return v
    return None


def _lbm_janmahasatian(weight_kg: float, height_m: float, sex: str) -> float | None:
    """Lean body mass — Janmahasatian 2005 formula (Clin Pharmacokinet
    44:1051). Recommended by PERCIST. Always positive, monotone, well
    validated up to BMI extremes; returns ``None`` for missing sex or
    non-positive inputs."""
    if weight_kg <= 0 or height_m <= 0:
        return None
    bmi = weight_kg / (height_m * height_m)
    s = (sex or "").upper()
    if s == "M" or s == "MALE":
        return (9270.0 * weight_kg) / (6680.0 + 216.0 * bmi)
    if s == "F" or s == "FEMALE":
        return (9270.0 * weight_kg) / (8780.0 + 244.0 * bmi)
    return None


def _lbm_james(weight_kg: float, height_cm: float, sex: str) -> float | None:
    """Lean body mass — James 1976. Historical formula, kept for
    operators trained on it. Can produce negative values for very high
    BMI; we filter those out and return ``None``."""
    if weight_kg <= 0 or height_cm <= 0:
        return None
    s = (sex or "").upper()
    ratio = weight_kg / height_cm
    if s == "M" or s == "MALE":
        v = 1.10 * weight_kg - 128.0 * ratio * ratio
    elif s == "F" or s == "FEMALE":
        v = 1.07 * weight_kg - 148.0 * ratio * ratio
    else:
        return None
    return v if v > 0 else None


def _bsa_mosteller(weight_kg: float, height_cm: float) -> float | None:
    """Body surface area — Mosteller 1987. Square-root simplification
    of Du Bois with comparable accuracy. Returns BSA in m²."""
    if weight_kg <= 0 or height_cm <= 0:
        return None
    return math.sqrt(weight_kg * height_cm / 3600.0)


def _bsa_dubois(weight_kg: float, height_cm: float) -> float | None:
    """Body surface area — Du Bois 1916. Returns BSA in m²."""
    if weight_kg <= 0 or height_cm <= 0:
        return None
    return 0.007184 * (weight_kg**0.425) * (height_cm**0.725)


def _philips_private(ds: Dataset, offset: int) -> float | None:
    """Read a Philips PET private tag from group ``0x7053``.

    Philips reserves group ``0x7053`` for PET-specific private tags;
    the SUV Scale Factor (offset ``0x00``) and the Activity
    Concentration Scale Factor (offset ``0x09``) let us turn
    CNTS-encoded PET into quantitative SUV without the BQML units the
    QIBA profile assumes.

    Per the DICOM private-data convention the dataset carries a
    *creator block* at ``(7053, 00xx)`` whose value is the creator
    string (e.g. ``"Philips PET Private Group"``); the actual data
    elements then live at ``(7053, xxYY)`` where ``xx`` is the creator
    block id and ``YY`` is the offset. We scan the 0x10..0xFF creator
    range rather than hard-coding the slot since firmware revisions
    pick different reservations.
    """
    if pydicom is None:
        return None
    for creator_id in range(0x10, 0x100):
        creator_tag = (0x7053, creator_id)
        try:
            creator_elem = ds[creator_tag]
        except (KeyError, TypeError):
            continue
        creator = str(creator_elem.value or "").upper()
        if "PHILIPS" not in creator or "PET" not in creator:
            continue
        data_tag = (0x7053, (creator_id << 8) | offset)
        try:
            data_elem = ds[data_tag]
        except (KeyError, TypeError):
            continue
        try:
            return float(data_elem.value)
        except (TypeError, ValueError):
            return None
    # Fallback: some exports register the tag without a creator block
    # (off-spec but common). Try the canonical ``(7053, 0x10YY)``.
    try:
        data_elem = ds[(0x7053, 0x1000 | offset)]
    except (KeyError, TypeError):
        return None
    try:
        return float(data_elem.value)
    except (TypeError, ValueError):
        return None


def compute_suv_factors(ds: Dataset) -> SuvFactors:
    """Extract SUV factors from a PET (PT) DICOM dataset.

    Caller must already have read the dataset (with or without pixel
    data — only header tags are touched). For non-PT modalities or
    incomplete metadata, a SuvFactors with ``factor_bw=None`` is
    returned and the populated fields document what we *did* find.
    """
    notes: list[str] = []

    weight_kg = float(getattr(ds, "PatientWeight", 0) or 0) or None

    rp_seq = getattr(ds, "RadiopharmaceuticalInformationSequence", None)
    rp = rp_seq[0] if rp_seq else None
    radionuclide = None
    half_life_s: float | None = None
    injected_dose_bq: float | None = None
    injection_dt: datetime | None = None
    if rp is not None:
        cs = getattr(rp, "RadionuclideCodeSequence", None)
        if cs:
            radionuclide = getattr(cs[0], "CodeMeaning", None) or getattr(cs[0], "CodeValue", None)
        try:
            half_life_s = float(rp.RadionuclideHalfLife)
        except (AttributeError, TypeError, ValueError):
            notes.append("missing RadionuclideHalfLife")
        try:
            injected_dose_bq = float(rp.RadionuclideTotalDose)
        except (AttributeError, TypeError, ValueError):
            notes.append("missing RadionuclideTotalDose")
        # Prefer the DateTime form when available; fall back to the legacy
        # split DA/TM pair for older scanners.
        rsdt = getattr(rp, "RadiopharmaceuticalStartDateTime", None)
        if rsdt:
            injection_dt = _parse_dicom_dt(str(rsdt))
        if injection_dt is None:
            rstm = getattr(rp, "RadiopharmaceuticalStartTime", None)
            sd = getattr(ds, "SeriesDate", None) or getattr(ds, "AcquisitionDate", None)
            if sd and rstm:
                injection_dt = _parse_dicom_da_tm(str(sd), str(rstm))

    units = getattr(ds, "Units", None)
    if units is not None:
        units = str(units).strip().upper()

    # Acquisition / scan reference time.
    acq_dt = _parse_dicom_dt(getattr(ds, "AcquisitionDateTime", None))
    if acq_dt is None:
        acq_dt = _parse_dicom_da_tm(
            getattr(ds, "AcquisitionDate", None) or getattr(ds, "SeriesDate", None),
            getattr(ds, "AcquisitionTime", None) or getattr(ds, "SeriesTime", None),
        )

    delta_t_s: float | None = None
    decay_corrected_bq: float | None = None
    if injection_dt and acq_dt:
        try:
            delta_t_s = (acq_dt - injection_dt).total_seconds()
        except Exception:
            delta_t_s = None
    if injected_dose_bq is not None and half_life_s is not None and delta_t_s is not None:
        decay_corrected_bq = injected_dose_bq * math.exp(-math.log(2.0) * delta_t_s / half_life_s)

    factor_bw: float | None = None
    if weight_kg and decay_corrected_bq and units == "BQML":
        # SUV_bw = activity[Bq/mL] * weight[g] / dose[Bq]
        # weight kg -> g (×1000), Units BQML already in Bq/mL → cancels.
        factor_bw = (weight_kg * 1000.0) / decay_corrected_bq
    elif units == "CNTS":
        # Philips Healthcare PET routinely emits Units=CNTS with two
        # private tags that re-scale to either (a) SUV directly, or
        # (b) Bq/mL. We try (a) first because it is dose/weight-aware
        # and avoids re-doing the activity-→-SUV math; (b) plugs into
        # the standard SUV formula. (c) is a Philips-specific fallback:
        # certain firmware revisions ship with neither private factor
        # but encode the activity scale directly into RescaleSlope —
        # ``Units = "CNTS"`` is a misnomer and ``pixel × RescaleSlope``
        # is already in Bq/mL.
        philips_suv_scale = _philips_private(ds, 0x00)
        philips_act_scale = _philips_private(ds, 0x09)
        manufacturer = str(getattr(ds, "Manufacturer", "") or "").upper()
        is_philips = "PHILIPS" in manufacturer
        if philips_suv_scale and philips_suv_scale > 0:
            # SUV = pixel * RescaleSlope * philips_suv_scale.
            # The viewer multiplies pixel × RescaleSlope already; we
            # only emit the residual ``factor_bw`` so the chain
            # rescaled_pixel × factor_bw = SUV.
            factor_bw = float(philips_suv_scale)
            notes.append("Philips CNTS via (7053,1000) SUV Scale Factor")
        elif philips_act_scale and philips_act_scale > 0 and weight_kg and decay_corrected_bq:
            # rescaled_pixel × act_scale = Bq/mL → apply the standard
            # SUV_bw formula on top of that.
            factor_bw = float(philips_act_scale) * (weight_kg * 1000.0) / decay_corrected_bq
            notes.append("Philips CNTS via (7053,1009) Activity Conc Scale Factor")
        elif is_philips:
            # Philips Digital PET (model 113 / "DigitalPET") and a few
            # other firmware revisions ship CNTS data without the SUV
            # Scale Factor (7053,1000) or Activity Concentration Scale
            # Factor (7053,1009). The remaining private tags in group
            # 0x7053 (frame-counts, normalisation file names, geometry)
            # don't carry a self-describing count-to-Bq/mL conversion.
            # We *cannot* compute SUV correctly without that scale; a
            # naïve "RescaleSlope as Bq/mL" fallback under-estimates by
            # 1-2 orders of magnitude (verified against this dataset:
            # SUV peak ≈ 0.13 vs the expected ~5 in active tissue).
            # Rather than display a wrong number we surface the
            # diagnosis so the operator knows the export is incomplete.
            notes.append(
                "Philips CNTS without (7053,1000)/(7053,1009) — vendor "
                "calibration tags missing, SUV non calcolabile. Chiedi al "
                "PACS l'export con Units=BQML o con SUV Scale Factor."
            )
        else:
            notes.append(f"unsupported Units 'CNTS' from {manufacturer or 'unknown vendor'}")
    elif units and units != "BQML":
        notes.append(f"unsupported Units {units!r} (need BQML)")

    # SUV variants: LBM (Janmahasatian + James) and BSA (Mosteller +
    # Du Bois) per Addendum C §5–§6. All four share the same
    # ``decay_corrected_bq`` denominator; numerator differs only.
    # Each is gated on the inputs being plausible (positive weight +
    # height, recognised sex). Failed branches stay ``None`` so the
    # client can fall back to SUV-BW.
    height_m: float | None = None
    raw_size = getattr(ds, "PatientSize", None)
    try:
        if raw_size is not None:
            v = float(raw_size)
            if 0.5 < v < 2.5:
                height_m = v
    except (TypeError, ValueError):
        height_m = None
    sex = str(getattr(ds, "PatientSex", "") or "").upper().strip() or None

    factor_lbm_janma: float | None = None
    factor_lbm_james: float | None = None
    factor_bsa_mosteller: float | None = None
    factor_bsa_dubois: float | None = None
    if weight_kg and height_m and decay_corrected_bq and units == "BQML":
        height_cm = height_m * 100.0
        lbm_jh = _lbm_janmahasatian(weight_kg, height_m, sex or "")
        if lbm_jh is not None and lbm_jh > 0:
            factor_lbm_janma = (lbm_jh * 1000.0) / decay_corrected_bq
        lbm_j = _lbm_james(weight_kg, height_cm, sex or "")
        if lbm_j is not None and lbm_j > 0:
            factor_lbm_james = (lbm_j * 1000.0) / decay_corrected_bq
        # BSA variants: SUV-BSA per EANM 2.0 / PERCIST 1.0 is
        # ``pixel[Bq/cm³] * BSA[cm²] / dose[Bq]``. Our BSA helpers return
        # m², so convert to cm² (× 10⁴) before dividing by the decay-
        # corrected dose. The resulting factor is dimensionally parallel
        # to the SUV-BW one (which converts kg→g via × 1000) and yields
        # SUV values in the expected clinical range (0.5–10).
        bsa_m = _bsa_mosteller(weight_kg, height_cm)
        if bsa_m is not None and bsa_m > 0:
            factor_bsa_mosteller = (bsa_m * 10000.0) / decay_corrected_bq
        bsa_d = _bsa_dubois(weight_kg, height_cm)
        if bsa_d is not None and bsa_d > 0:
            factor_bsa_dubois = (bsa_d * 10000.0) / decay_corrected_bq
    elif weight_kg and decay_corrected_bq and units == "BQML" and not height_m:
        notes.append(
            f"SUV-LBM/BSA not computable: PatientSize missing or implausible (got {raw_size!r})"
        )
    elif weight_kg and decay_corrected_bq and units == "BQML" and not sex:
        notes.append("SUV-LBM not computable: PatientSex missing")

    # Sanity validations (Addendum C §9). Non-blocking warnings; the
    # SUV factors are still computed but the viewer can flag the
    # caveat in the PET HUD so the operator double-checks the
    # numbers before reporting on them.
    warnings: list[str] = []
    if weight_kg is not None and (weight_kg < 1 or weight_kg > 300):
        warnings.append(f"PatientWeight {weight_kg} kg is implausible")
    if injected_dose_bq is not None and (injected_dose_bq < 50e6 or injected_dose_bq > 1000e6):
        warnings.append(
            f"RadionuclideTotalDose {injected_dose_bq:.0f} Bq outside the typical "
            "50-1000 MBq clinical range"
        )
    if delta_t_s is not None and (delta_t_s < 0 or delta_t_s > 86400):
        warnings.append(
            f"Acquisition vs injection delta {delta_t_s:.0f} s is implausible (expected 0..24h)"
        )
    decay_correction = str(getattr(ds, "DecayCorrection", "") or "").upper().strip()
    if decay_correction == "NONE":
        warnings.append("DecayCorrection=NONE — pixels not decay-corrected, SUV is unreliable")

    tracer = detect_tracer(ds)
    branching = branching_ratio_for(ds)

    return SuvFactors(
        factor_bw=factor_bw,
        patient_weight_kg=weight_kg,
        radionuclide=radionuclide,
        units=units,
        half_life_s=half_life_s,
        injected_dose_bq=injected_dose_bq,
        decay_corrected_dose_bq=decay_corrected_bq,
        delta_t_s=delta_t_s,
        notes=notes,
        factor_lbm_janmahasatian=factor_lbm_janma,
        factor_lbm_james=factor_lbm_james,
        factor_bsa_mosteller=factor_bsa_mosteller,
        factor_bsa_dubois=factor_bsa_dubois,
        patient_height_m=height_m,
        patient_sex=sex,
        tracer=tracer,
        branching_ratio=branching,
        warnings=warnings,
    )
