"""DICOM SEG serializer (geometry-preserving export of a stored mask).

DB-free: ``build_segmentation_seg`` takes an in-memory mask + the source
datasets, so the geometry/coding/provenance behaviour is fully testable without
Postgres or S3. The DB/S3 orchestration (``export_segmentation_seg``) is thin
and covered by the integration suite.
"""

from __future__ import annotations

import io

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from bvphoenix.services.dicom_seg_export import (
    SEGMENTATION_SOP_CLASS_UID,
    SegExportError,
    build_segmentation_seg,
)

_STUDY = generate_uid()
_SERIES = generate_uid()
_FRAME = generate_uid()


def _ct_slice(z: float, rows: int = 8, cols: int = 8) -> Dataset:
    ds = Dataset()
    ds.PatientID = "ANON"
    ds.PatientName = "ANON^ANON"
    ds.PatientBirthDate = ""
    ds.PatientSex = ""
    ds.StudyInstanceUID = _STUDY
    ds.SeriesInstanceUID = _SERIES
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.FrameOfReferenceUID = _FRAME
    ds.StudyDate = ""
    ds.StudyTime = ""
    ds.AccessionNumber = ""
    ds.StudyID = ""
    ds.ReferringPhysicianName = ""
    ds.Rows = rows
    ds.Columns = cols
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.ImagePositionPatient = [10.0, 20.0, float(z)]
    ds.PixelSpacing = [0.7, 0.7]
    ds.SliceThickness = 2.0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = fm
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    return ds


def _read(seg_bytes: bytes) -> Dataset:
    return pydicom.dcmread(io.BytesIO(seg_bytes))


def test_seg_is_geometry_preserving_label_map():
    sources = [_ct_slice(z) for z in (0.0, 2.0, 4.0)]  # already z-sorted
    mask = np.zeros((3, 8, 8), dtype=np.uint8)
    mask[:, 1:4, 1:4] = 1  # liver
    mask[:, 5:7, 5:7] = 2  # spleen

    out = build_segmentation_seg(
        mask,
        sources,
        label_map={"1": "liver", "2": "spleen"},
        default_label="organs",
        producer="totalsegmentator",
        producer_version="2.0",
        author_kind="agent",
    )
    seg = _read(out)
    assert seg.SOPClassUID == SEGMENTATION_SOP_CLASS_UID
    # Geometry preserved + source referenced (the whole point).
    assert seg.FrameOfReferenceUID == _FRAME
    assert seg.ReferencedSeriesSequence[0].SeriesInstanceUID == _SERIES
    assert seg.StudyInstanceUID == _STUDY
    assert len(seg.SegmentSequence) == 2
    labels = {s.SegmentNumber: s.SegmentLabel for s in seg.SegmentSequence}
    assert labels == {1: "liver", 2: "spleen"}
    # Per-frame ImagePositionPatient is copied from a source slice (one of the
    # source z positions), i.e. real patient-space geometry, not index space.
    src_ipps = {(10.0, 20.0, z) for z in (0.0, 2.0, 4.0)}
    for fg in seg.PerFrameFunctionalGroupsSequence:
        ipp = tuple(float(x) for x in fg.PlanePositionSequence[0].ImagePositionPatient)
        assert ipp in src_ipps


def test_seg_binary_single_segment_uses_default_label():
    sources = [_ct_slice(z) for z in (0.0, 2.0)]
    mask = np.zeros((2, 8, 8), dtype=np.uint8)
    mask[:, 2:5, 2:5] = 1
    out = build_segmentation_seg(
        mask,
        sources,
        label_map=None,
        default_label="liver lesion",
        producer="manual",
        author_kind="human",
    )
    seg = _read(out)
    assert len(seg.SegmentSequence) == 1
    assert seg.SegmentSequence[0].SegmentLabel == "liver lesion"


def test_seg_provenance_manual_vs_automatic():
    sources = [_ct_slice(z) for z in (0.0, 2.0)]
    mask = np.zeros((2, 8, 8), dtype=np.uint8)
    mask[:, 2:5, 2:5] = 1

    manual = _read(
        build_segmentation_seg(
            mask, sources, label_map=None, default_label="x", producer="manual", author_kind="human"
        )
    )
    assert manual.SegmentSequence[0].SegmentAlgorithmType == "MANUAL"

    auto = _read(
        build_segmentation_seg(
            mask,
            sources,
            label_map=None,
            default_label="x",
            producer="totalsegmentator",
            model_id="totalsegmentator",
            producer_version="2.0",
            author_kind="agent",
        )
    )
    seg0 = auto.SegmentSequence[0]
    assert seg0.SegmentAlgorithmType == "AUTOMATIC"
    # AI provenance is visible: family code = DCM "Artificial Intelligence".
    algo = seg0.SegmentationAlgorithmIdentificationSequence[0]
    fam = algo.AlgorithmFamilyCodeSequence[0]
    assert fam.CodeValue == "123110" and fam.CodingSchemeDesignator == "DCM"
    assert algo.AlgorithmName == "totalsegmentator"


def test_seg_rejects_slice_count_mismatch():
    sources = [_ct_slice(z) for z in (0.0, 2.0)]  # 2 slices
    mask = np.zeros((3, 8, 8), dtype=np.uint8)  # 3 mask slices
    mask[:, 1:3, 1:3] = 1
    with pytest.raises(SegExportError, match="slice-count mismatch"):
        build_segmentation_seg(mask, sources, label_map=None, default_label="x")


def test_seg_rejects_plane_mismatch():
    sources = [_ct_slice(0.0, rows=8, cols=8)]
    mask = np.zeros((1, 6, 6), dtype=np.uint8)  # wrong plane size
    mask[:, 1:3, 1:3] = 1
    with pytest.raises(SegExportError, match="does not match source"):
        build_segmentation_seg(mask, sources, label_map=None, default_label="x")


def test_seg_rejects_empty_mask():
    sources = [_ct_slice(0.0)]
    mask = np.zeros((1, 8, 8), dtype=np.uint8)
    with pytest.raises(SegExportError, match="empty"):
        build_segmentation_seg(mask, sources, label_map=None, default_label="x")
