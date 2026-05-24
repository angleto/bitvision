"""SUV factor computation regression tests.

Pins the SUV-BW, SUV-LBM (Janmahasatian + James) and SUV-BSA
(Mosteller + Du Bois) factors against literature values so we catch
unit-conversion regressions like the one that made SUV-BSA factors
underflow by a factor of 10000 (bsa_m² instead of bsa_cm²).

Reference patient: 80 kg, 1.65 m, F, FDG (¹⁸F, half-life 6586.2 s,
branching ratio 0.967), injected dose 208.22 MBq, scan starts 3340 s
after injection.
"""

from __future__ import annotations

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset

from bvphoenix.services.suv import compute_suv_factors


def _make_pet_dataset(
    *,
    weight_kg: float | None = 80.0,
    height_m: float | None = 1.65,
    sex: str | None = "F",
    dose_bq: float | None = 208_221_872.0,
    half_life_s: float | None = 6586.2,
    delta_t_s: float | None = 3340.0,
    units: str | None = "BQML",
) -> Dataset:
    """Build an in-memory PET DICOM dataset carrying just the tags
    ``compute_suv_factors`` reads. Defaults match the reference patient
    above so individual tests can override only the field under test."""
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = pydicom.uid.PositronEmissionTomographyImageStorage
    fm.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = fm
    ds.Modality = "PT"

    if weight_kg is not None:
        ds.PatientWeight = weight_kg
    if height_m is not None:
        ds.PatientSize = height_m
    if sex is not None:
        ds.PatientSex = sex
    if units is not None:
        ds.Units = units

    if dose_bq is not None or half_life_s is not None or delta_t_s is not None:
        rp = Dataset()
        if dose_bq is not None:
            rp.RadionuclideTotalDose = dose_bq
        if half_life_s is not None:
            rp.RadionuclideHalfLife = half_life_s
        if delta_t_s is not None:
            # Encode injection at t=0, scan at t=delta_t_s on a fixed reference
            # day so the parser computes the same delta deterministically.
            rp.RadiopharmaceuticalStartDateTime = "20260403080000"
            ds.AcquisitionDateTime = (
                "20260403"
                f"{8 + int(delta_t_s) // 3600:02d}"
                f"{(int(delta_t_s) % 3600) // 60:02d}"
                f"{int(delta_t_s) % 60:02d}"
            )
        # Identify the radionuclide so any tracer-specific path the
        # production code may walk doesn't end up parsing ``None``.
        nuc = Dataset()
        nuc.CodeMeaning = "^18^Fluorine"
        nuc.CodeValue = "C-111A1"
        rp.RadionuclideCodeSequence = [nuc]
        ds.RadiopharmaceuticalInformationSequence = [rp]
    return ds


class TestSuvBwReferencePatient:
    """SUV-BW must reproduce the QIBA-validated factor for the reference
    patient. Anchors all the other variant tests."""

    def test_factor_bw_matches_literature(self) -> None:
        ds = _make_pet_dataset()
        f = compute_suv_factors(ds)
        # 80 kg × 1000 / 1.465e8 Bq = 5.46e-4. Tolerance: 0.5 % to
        # absorb the decay-correction floating-point noise.
        assert f.factor_bw is not None
        assert abs(f.factor_bw - 5.46e-4) / 5.46e-4 < 0.005

    def test_realistic_pixel_yields_clinical_suv(self) -> None:
        """A typical FDG-avid lesion encodes ~10⁴ Bq/mL after rescale.
        SUV-BW should be in the 1–10 range (active tumour territory)."""
        ds = _make_pet_dataset()
        f = compute_suv_factors(ds)
        assert f.factor_bw is not None
        suv = 1.0e4 * f.factor_bw
        assert 1.0 < suv < 10.0


class TestSuvBsaUnitsRegression:
    """The BSA factor used to drop ``× 10⁴`` (cm²/m² conversion),
    yielding factors of order 1e-8 instead of 1e-4 and SUV displays
    rounded to 0.00. Pin the corrected magnitude here."""

    def test_bsa_dubois_factor_in_clinical_magnitude(self) -> None:
        ds = _make_pet_dataset()
        f = compute_suv_factors(ds)
        # BSA Du Bois ≈ 1.874 m² → 18740 cm² / 1.465e8 Bq ≈ 1.279e-4.
        assert f.factor_bsa_dubois is not None
        assert 1.0e-4 < f.factor_bsa_dubois < 2.0e-4
        # Tight check: ±2 % of the literature value.
        assert abs(f.factor_bsa_dubois - 1.279e-4) / 1.279e-4 < 0.02

    def test_bsa_mosteller_factor_in_clinical_magnitude(self) -> None:
        ds = _make_pet_dataset()
        f = compute_suv_factors(ds)
        # BSA Mosteller = sqrt(80*165/3600) ≈ 1.914 m² → 1.307e-4.
        assert f.factor_bsa_mosteller is not None
        assert 1.0e-4 < f.factor_bsa_mosteller < 2.0e-4
        assert abs(f.factor_bsa_mosteller - 1.307e-4) / 1.307e-4 < 0.02

    def test_bsa_pixel_yields_nonzero_suv(self) -> None:
        """The bug symptom: pixel × factor_bsa rounded to 0.00 in the
        viewer because the factor was 4 orders of magnitude too small.
        Asserts the factor produces a finite, displayable SUV."""
        ds = _make_pet_dataset()
        f = compute_suv_factors(ds)
        assert f.factor_bsa_dubois is not None
        suv_bsa = 1.0e4 * f.factor_bsa_dubois
        # SUV-BSA values are typically ~25 % of SUV-BW for the same
        # lesion (because BSA in cm² × pixel in Bq/cm³ has different
        # dimensions than weight × pixel/dose). Just assert it's a
        # number a clinician can read on the screen.
        assert suv_bsa > 0.1
        assert round(suv_bsa, 2) != 0.00


class TestSuvLbmFactors:
    """LBM (Janmahasatian + James) was already correct (× 1000 kg→g)
    pre-fix; pin the values so a future refactor of the BSA path
    doesn't accidentally break LBM too."""

    def test_lbm_janmahasatian_female(self) -> None:
        ds = _make_pet_dataset(sex="F")
        f = compute_suv_factors(ds)
        # BMI = 80/1.65² = 29.39 → LBM_F = 9270*80 / (8780+244*29.39)
        # = 46.49 kg → factor = 46490 / 1.465e8 = 3.173e-4.
        assert f.factor_lbm_janmahasatian is not None
        assert abs(f.factor_lbm_janmahasatian - 3.173e-4) / 3.173e-4 < 0.01

    def test_lbm_james_female(self) -> None:
        ds = _make_pet_dataset(sex="F")
        f = compute_suv_factors(ds)
        # James_F = 1.07*80 - 148*(80/165)² = 50.82 kg → 3.469e-4.
        assert f.factor_lbm_james is not None
        assert abs(f.factor_lbm_james - 3.469e-4) / 3.469e-4 < 0.01

    def test_lbm_male_branch(self) -> None:
        """The male path uses different coefficients; smoke-test it
        runs and produces a positive factor."""
        ds = _make_pet_dataset(sex="M", weight_kg=85.0, height_m=1.80)
        f = compute_suv_factors(ds)
        assert f.factor_lbm_janmahasatian is not None
        assert f.factor_lbm_janmahasatian > 0
        assert f.factor_lbm_james is not None
        assert f.factor_lbm_james > 0


class TestSuvDegradedInputs:
    """Missing or implausible inputs degrade gracefully: BW survives
    when only weight + dose are present; LBM/BSA become ``None`` when
    height or sex is absent. The viewer relies on the ``None`` sentinel
    to disable variants that aren't available."""

    def test_no_height_disables_bsa_and_lbm(self) -> None:
        ds = _make_pet_dataset(height_m=None)
        f = compute_suv_factors(ds)
        assert f.factor_bw is not None  # still computable
        assert f.factor_bsa_dubois is None
        assert f.factor_bsa_mosteller is None
        assert f.factor_lbm_janmahasatian is None
        assert f.factor_lbm_james is None
        assert any("PatientSize" in n for n in f.notes)

    def test_no_sex_disables_only_lbm(self) -> None:
        ds = _make_pet_dataset(sex=None)
        f = compute_suv_factors(ds)
        # BW + BSA still work (BSA doesn't need sex).
        assert f.factor_bw is not None
        assert f.factor_bsa_dubois is not None
        assert f.factor_bsa_mosteller is not None
        # LBM needs sex.
        assert f.factor_lbm_janmahasatian is None
        assert f.factor_lbm_james is None

    def test_implausible_height_is_rejected(self) -> None:
        """PatientSize outside [0.5, 2.5] m is treated as missing
        (clinical guard against scanners that store height in cm by
        mistake, which would inflate BSA by ×100)."""
        ds = _make_pet_dataset(height_m=170.0)  # cm-as-meters bug
        f = compute_suv_factors(ds)
        assert f.factor_bsa_dubois is None
        assert f.factor_bsa_mosteller is None

    def test_no_weight_disables_everything(self) -> None:
        ds = _make_pet_dataset(weight_kg=None)
        f = compute_suv_factors(ds)
        assert f.factor_bw is None
        assert f.factor_bsa_dubois is None
        assert f.factor_bsa_mosteller is None
        assert f.factor_lbm_janmahasatian is None
        assert f.factor_lbm_james is None

    def test_unsupported_units_disables_factor(self) -> None:
        ds = _make_pet_dataset(units="GML")  # not BQML, not CNTS
        f = compute_suv_factors(ds)
        assert f.factor_bw is None


class TestSuvFactorRelativeOrder:
    """Sanity check the relative magnitude of the factors against
    each other (a regression test that's robust to small refinements
    in the formulas: factor_bw > factor_lbm > factor_bsa, and all are
    in the same order of magnitude after the BSA unit fix)."""

    def test_factor_ordering_matches_clinical_intuition(self) -> None:
        ds = _make_pet_dataset()
        f = compute_suv_factors(ds)
        assert f.factor_bw is not None
        assert f.factor_lbm_janmahasatian is not None
        assert f.factor_bsa_dubois is not None
        # SUV-BW > SUV-LBM (lean mass < total mass for typical adults).
        assert f.factor_bw > f.factor_lbm_janmahasatian
        # All three within a factor of ~5 of each other.
        assert f.factor_bw / f.factor_bsa_dubois < 10
        assert f.factor_lbm_janmahasatian / f.factor_bsa_dubois < 10
