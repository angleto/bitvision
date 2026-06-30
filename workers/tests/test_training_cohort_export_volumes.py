"""Volume-format export (nnU-Net / MONAI / COCO) in the cohort streamer.

End-to-end over ``_stream_cohort_volumes_sync`` with synthetic in-memory
DICOM + masks and a storage stub that captures the produced ZIP, so the
file layout, the framework manifests, and the per-series burned-in-PHI gate
are verified without Postgres or S3. Complements the pure-serializer unit
tests (``backend/tests/test_training_cohort_formats``) and the raw-bundle
stats test."""

from __future__ import annotations

import gzip
import json
import threading
import uuid
import zipfile
from io import BytesIO

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

pytest.importorskip("stream_zip")

from bvphoenix.services import training_cohort_formats as fmts

from bvworkers.tasks import training_cohort_export as mod

_CT = "1.2.840.10008.5.1.4.1.1.2"  # CT (low burned-in risk)
_US = "1.2.840.10008.5.1.4.1.1.6.1"  # US (high burned-in risk)


def _ct_slice(z: float, *, fill: int, rows: int = 4, cols: int = 4, sop: str = _CT) -> bytes:
    ds = pydicom.Dataset()
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = UID(sop)
    ds.Modality = "CT" if sop == _CT else "US"
    ds.ImageType = ["ORIGINAL", "PRIMARY"]
    ds.BodyPartExamined = "CHEST"
    ds.Rows = rows
    ds.Columns = cols
    ds.ImagePositionPatient = [0.0, 0.0, float(z)]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.SliceThickness = 1.0
    ds.RescaleSlope = 1.0
    ds.RescaleIntercept = 0.0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.PixelData = np.full((rows, cols), fill, dtype=np.int16).tobytes()
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = UID(sop)
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


class _CollectingStorage:
    """Captures the streamed ZIP so the test can unzip + inspect it."""

    def __init__(self) -> None:
        self.zip_bytes = b""

    def upload_iter(self, gen, *, bucket: str, key: str, content_type: str):
        self.zip_bytes = b"".join(gen)

        class _R:
            size_bytes = 0

        _R.size_bytes = len(self.zip_bytes)
        return _R()


def _run(fmt: str, monkeypatch, *, mask_bytes: bytes | None = None, top_sop: str = _CT):
    """One CT series (2 slices 4x4) + one liver mask; stream as ``fmt`` and
    return the produced {name: bytes} from the captured ZIP."""
    s1 = uuid.uuid4()
    d0 = _ct_slice(0.0, fill=100, sop=top_sop)
    d1 = _ct_slice(1.0, fill=200)
    # mask aligned to (nz=2, ny=4, nx=4): a 2x2 block on slice 0.
    if mask_bytes is None:
        m = np.zeros((2, 4, 4), dtype=np.uint8)
        m[0, 1:3, 1:3] = 1
        mask_bytes = m.tobytes()
    bodies = {"d0": d0, "d1": d1, "m0": mask_bytes}
    monkeypatch.setattr(mod, "_fetch_blob_bytes", lambda _s, _b, key, *, deidentify: bodies[key])
    storage = _CollectingStorage()
    monkeypatch.setattr(mod, "get_s3_storage", lambda: storage)
    monkeypatch.setattr(mod, "get_defacer", lambda: None)

    series_plan = [
        {
            "study_syn": "study-0001",
            "study_id": s1,
            "series_idx": 1,
            "dicom": [{"bucket": "b", "key": "d0"}, {"bucket": "b", "key": "d1"}],
            "masks": [{"label": "liver", "label_map": {"1": "liver"}, "bucket": "b", "key": "m0"}],
        }
    ]
    label_index = fmts.build_label_index([s["masks"] for s in series_plan])
    _size, stats, skipped = mod._stream_cohort_volumes_sync(
        series_plan,
        b'{"schema":"bvphoenix.training-labels/v1"}',
        fmt,
        label_index,
        bucket="b",
        key="out.zip",
        progress_q=[0],
        cancel=threading.Event(),
    )
    names: dict[str, bytes] = {}
    with zipfile.ZipFile(BytesIO(storage.zip_bytes)) as zf:
        for n in zf.namelist():
            names[n] = zf.read(n)
    return names, stats, skipped, s1


def test_nnunet_layout_and_manifest(monkeypatch):
    names, stats, skipped, s1 = _run("nnunet", monkeypatch)
    assert "imagesTr/study-0001_series-01_0000.nii.gz" in names
    assert "labelsTr/study-0001_series-01.nii.gz" in names
    assert "dataset.json" in names
    assert "labels.json" in names
    assert skipped == []

    import nibabel as nib

    img = nib.Nifti1Image.from_bytes(
        gzip.decompress(names["imagesTr/study-0001_series-01_0000.nii.gz"])
    )
    lbl = nib.Nifti1Image.from_bytes(gzip.decompress(names["labelsTr/study-0001_series-01.nii.gz"]))
    assert img.shape == (4, 4, 2)  # (nx, ny, nz)
    assert lbl.shape == (4, 4, 2)
    assert np.allclose(img.affine, lbl.affine)  # image + label share geometry
    assert set(np.unique(np.asarray(lbl.dataobj))) <= {0, 1}

    d = json.loads(names["dataset.json"])
    assert d["channel_names"] == {"0": "CT"}
    assert d["labels"] == {"background": 0, "liver": 1}
    assert d["numTraining"] == 1
    assert d["file_ending"] == ".nii.gz"
    # Per-study payout weight = the emitted artifact bytes.
    assert stats[s1]["size_bytes"] > 0


def test_monai_datalist(monkeypatch):
    names, _stats, _skipped, _s1 = _run("monai", monkeypatch)
    assert "dataset.json" in names
    d = json.loads(names["dataset.json"])
    assert d["labels"] == {"0": "background", "1": "liver"}
    assert d["modality"] == {"0": "CT"}
    assert d["numTraining"] == 1
    assert d["training"][0]["image"] == "./imagesTr/study-0001_series-01_0000.nii.gz"
    assert d["training"][0]["label"] == "./labelsTr/study-0001_series-01.nii.gz"


def test_coco_slices_and_annotations(monkeypatch):
    names, _stats, _skipped, _s1 = _run("coco", monkeypatch)
    png_names = [n for n in names if n.endswith(".png")]
    assert png_names == ["images/study-0001_series-01_z0000.png"]  # only the annotated slice
    assert "annotations/instances.json" in names
    coco = json.loads(names["annotations/instances.json"])
    assert len(coco["images"]) == 1
    assert coco["categories"] == [{"id": 1, "name": "liver"}]
    ann = coco["annotations"][0]
    assert ann["category_id"] == 1
    assert ann["bbox"] == [1, 1, 2, 2]
    assert ann["area"] == 4


def test_high_risk_series_dropped_whole(monkeypatch):
    # A single high-risk (US) slice drops the ENTIRE series — a volume cannot
    # ship with a PHI hole, so it is excluded as a unit (not slice-by-slice).
    names, _stats, skipped, _s1 = _run("nnunet", monkeypatch, top_sop=_US)
    assert not any(n.startswith("imagesTr/") for n in names)
    assert len(skipped) == 1
    assert skipped[0]["risk"] == "high"
    # The manifests still ship (an empty but well-formed dataset).
    assert json.loads(names["dataset.json"])["numTraining"] == 0


def test_misaligned_mask_drops_series(monkeypatch):
    # Mask voxel count != image volume -> never ship a mis-rasterized label.
    names, _stats, skipped, _s1 = _run("nnunet", monkeypatch, mask_bytes=b"\x01\x02\x03")
    assert not any(n.startswith("labelsTr/") for n in names)
    assert len(skipped) == 1
    assert skipped[0]["risk"].startswith("format:")
