"""Smoke tests for ``NonVolumetricSeriesError`` detection.

Pin the contract that ``pack_series`` raises early when the series can't
be turned into a 3D volume, so the API translates the error into a 404
and the frontend falls back to ``<Series2DViewer>``.
"""

from __future__ import annotations

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset

from bvphoenix.services.volumes import (
    NON_VOLUMETRIC_SOP_CLASSES,
    NonVolumetricSeriesError,
    _all_non_volumetric,
    _orientations_consistent,
)


def _make_dataset(*, sop_class: str, iop: list[float] | None = None) -> Dataset:
    """Build a tiny in-memory pydicom Dataset with the fields the
    detection logic actually inspects. Pixel data is not needed for
    the precheck branches we want to exercise."""
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = sop_class
    fm.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = fm
    ds.SOPClassUID = sop_class
    if iop is not None:
        ds.ImageOrientationPatient = iop
    return ds


class TestNonVolumetricSopClassDetection:
    def test_secondary_capture_alone_is_non_volumetric(self) -> None:
        ds = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.7")
        assert _all_non_volumetric([ds])

    def test_presentation_state_alone_is_non_volumetric(self) -> None:
        ds = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.11.1")
        assert _all_non_volumetric([ds])

    def test_structured_report_is_non_volumetric(self) -> None:
        ds = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.88.11")
        assert _all_non_volumetric([ds])

    def test_ct_image_is_volumetric(self) -> None:
        ds = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.2")
        assert not _all_non_volumetric([ds])

    def test_mixed_series_is_treated_as_volumetric(self) -> None:
        # When at least one instance is a real CT image the precheck
        # leaves the decision to the geometric consistency check
        # downstream — we don't want to drop a study that happens to
        # carry one stray PR object alongside its real slices.
        sc = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.7")
        ct = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.2")
        assert not _all_non_volumetric([sc, ct])

    def test_set_membership_includes_common_kinds(self) -> None:
        # Quick sanity: the set must cover the most common SOP classes
        # we actually see on hospital DVDs (SC, PR, SR, encapsulated PDF).
        for sop in (
            "1.2.840.10008.5.1.4.1.1.7",
            "1.2.840.10008.5.1.4.1.1.11.1",
            "1.2.840.10008.5.1.4.1.1.88.11",
            "1.2.840.10008.5.1.4.1.1.104.1",
        ):
            assert sop in NON_VOLUMETRIC_SOP_CLASSES


class TestOrientationConsistency:
    AP = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    LAT = [0.0, 0.0, -1.0, 0.0, 1.0, 0.0]

    def test_single_dataset_is_consistent(self) -> None:
        ds = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.2", iop=self.AP)
        assert _orientations_consistent([ds])

    def test_two_axial_slices_are_consistent(self) -> None:
        a = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.2", iop=self.AP)
        b = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.2", iop=self.AP)
        assert _orientations_consistent([a, b])

    def test_scout_ap_plus_lat_is_inconsistent(self) -> None:
        # Classic CT scout: one AP projection + one LAT projection in
        # the same series. They share a SOP class (CT Image Storage)
        # but are NOT slices through one volume.
        a = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.2", iop=self.AP)
        b = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.2", iop=self.LAT)
        assert not _orientations_consistent([a, b])

    def test_missing_iop_is_treated_as_compatible(self) -> None:
        # Legacy CR / DX without IOP tags shouldn't trip the check.
        a = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.1")
        b = _make_dataset(sop_class="1.2.840.10008.5.1.4.1.1.1")
        assert _orientations_consistent([a, b])


def test_non_volumetric_error_inherits_value_error() -> None:
    # Backwards compat: existing callers that catch ValueError must
    # still match the new exception subclass.
    err = NonVolumetricSeriesError("test")
    assert isinstance(err, ValueError)
