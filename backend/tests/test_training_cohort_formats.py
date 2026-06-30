"""Pure-unit tests for the training-cohort format serializers (P5).

DB-free + S3-free: every function takes in-memory pixels / masks, so the
geometry, label remapping, NIfTI/PNG encoding, and manifest shapes are
testable without Postgres or S3 (mirrors ``test_dicom_seg_export``). These
guard the parts where a silent bug would corrupt a published training set:
mask<->image alignment, the global label index, and the COCO RLE.
"""

from __future__ import annotations

import gzip
import io
import json

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from bvphoenix.services.training_cohort_formats import (
    CocoBuilder,
    CohortFormatError,
    build_image_volume,
    build_label_index,
    build_label_volume,
    default_window,
    encode_png,
    monai_dataset_json,
    nnunet_dataset_json,
    rle_encode_2d,
    window_to_uint8,
    write_nifti,
)


def _slice(z: float, *, rows: int = 4, cols: int = 4, fill: int = 0, instance: int = 1) -> Dataset:
    ds = Dataset()
    ds.Modality = "CT"
    ds.Rows = rows
    ds.Columns = cols
    ds.InstanceNumber = instance
    ds.ImagePositionPatient = [10.0, 20.0, float(z)]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [0.8, 0.7]  # [row(Y), col(X)]
    ds.SliceThickness = 2.0
    ds.RescaleSlope = 1.0
    ds.RescaleIntercept = -1024.0
    arr = np.full((rows, cols), fill, dtype=np.int16)
    ds.PixelData = arr.tobytes()
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = fm
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    return ds


# --------------------------------------------------------------------------
# build_image_volume
# --------------------------------------------------------------------------
def test_build_image_volume_sorts_and_rescales() -> None:
    # Pass slices out of z-order; expect sort by z and HU rescale applied.
    s_hi = _slice(4.0, fill=100, instance=2)
    s_lo = _slice(0.0, fill=200, instance=1)
    arr, spacing, ordered = build_image_volume([s_hi, s_lo])
    assert arr.shape == (2, 4, 4)
    # Sorted ascending by z -> slice 0 is the z=0 (fill 200) one.
    assert float(arr[0, 0, 0]) == 200.0 - 1024.0
    assert float(arr[1, 0, 0]) == 100.0 - 1024.0
    # spacing is (col_mm=X, row_mm=Y, slice_mm); slice from IPP z-delta = 4.0.
    assert spacing == pytest.approx((0.7, 0.8, 4.0))
    assert [float(d.ImagePositionPatient[2]) for d in ordered] == [0.0, 4.0]


def test_build_image_volume_rejects_inconsistent_size() -> None:
    with pytest.raises(CohortFormatError):
        build_image_volume([_slice(0.0), _slice(1.0, rows=8, cols=8)])


def test_build_image_volume_rejects_empty() -> None:
    with pytest.raises(CohortFormatError):
        build_image_volume([])


# --------------------------------------------------------------------------
# label index + label volume
# --------------------------------------------------------------------------
def test_build_label_index_is_sorted_and_one_based() -> None:
    idx = build_label_index(
        [
            [{"label": "seg-a", "label_map": {"1": "liver", "2": "spleen"}}],
            [{"label": "tumor", "label_map": {}}],
        ]
    )
    assert idx == {"liver": 1, "spleen": 2, "tumor": 3}  # alphabetical, 1-based


def test_build_label_volume_paints_global_ids() -> None:
    idx = {"liver": 1, "tumor": 2}
    nz, ny, nx = 2, 4, 4
    liver = np.zeros((nz, ny, nx), dtype=np.uint8)
    liver[0, 1, 1] = 1
    tumor = np.zeros((nz, ny, nx), dtype=np.uint8)
    tumor[1, 2, 2] = 1
    masks = [
        {"label": "seg", "label_map": {"1": "liver"}, "raw": liver.tobytes()},
        {"label": "tumor", "label_map": {}, "raw": tumor.tobytes()},
    ]
    vol = build_label_volume(masks, (nz, ny, nx), idx)
    assert vol is not None
    assert vol[0, 1, 1] == 1
    assert vol[1, 2, 2] == 2
    assert int(vol.sum()) == 1 + 2


def test_build_label_volume_size_mismatch_raises() -> None:
    idx = {"liver": 1}
    masks = [{"label": "seg", "label_map": {"1": "liver"}, "raw": b"\x01\x02\x03"}]
    with pytest.raises(CohortFormatError):
        build_label_volume(masks, (2, 4, 4), idx)


def test_build_label_volume_empty_returns_none() -> None:
    idx = {"liver": 1}
    empty = np.zeros((1, 4, 4), dtype=np.uint8)
    masks = [{"label": "seg", "label_map": {"1": "liver"}, "raw": empty.tobytes()}]
    assert build_label_volume(masks, (1, 4, 4), idx) is None


# --------------------------------------------------------------------------
# NIfTI
# --------------------------------------------------------------------------
def test_write_nifti_roundtrips_geometry_and_voxels() -> None:
    import nibabel as nib

    arr = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)  # (nz, ny, nx)
    raw = write_nifti(arr, (0.7, 0.8, 2.0))
    img = nib.Nifti1Image.from_bytes(gzip.decompress(raw))
    data = np.asarray(img.dataobj)  # (nx, ny, nz)
    assert data.shape == (4, 3, 2)
    assert np.allclose(np.diag(img.affine)[:3], [0.7, 0.8, 2.0])
    # Voxel order preserved: arr[z, y, x] == data[x, y, z].
    for z in range(2):
        for y in range(3):
            for x in range(4):
                assert data[x, y, z] == arr[z, y, x]


def test_image_and_label_nifti_share_geometry() -> None:
    """The whole point: a label written from a mask aligned to the same
    series must land on the identical grid as the image."""
    import nibabel as nib

    img_arr, spacing, _ = build_image_volume([_slice(0.0, instance=1), _slice(2.0, instance=2)])
    lbl_arr = np.zeros_like(img_arr, dtype=np.uint8)
    lbl_arr[0, 1, 1] = 1
    img = nib.Nifti1Image.from_bytes(gzip.decompress(write_nifti(img_arr, spacing)))
    lbl = nib.Nifti1Image.from_bytes(gzip.decompress(write_nifti(lbl_arr, spacing)))
    assert np.allclose(img.affine, lbl.affine)
    assert img.shape == lbl.shape


# --------------------------------------------------------------------------
# manifests
# --------------------------------------------------------------------------
def test_nnunet_dataset_json_shape() -> None:
    d = nnunet_dataset_json(modality="CT", label_index={"liver": 1, "tumor": 2}, num_training=5)
    assert d["channel_names"] == {"0": "CT"}
    assert d["labels"] == {"background": 0, "liver": 1, "tumor": 2}
    assert d["numTraining"] == 5
    assert d["file_ending"] == ".nii.gz"
    json.dumps(d)  # serializable


def test_monai_dataset_json_shape() -> None:
    cases = [("imagesTr/c1_0000.nii.gz", "labelsTr/c1.nii.gz")]
    d = monai_dataset_json(modality="CT", label_index={"liver": 1}, cases=cases)
    assert d["labels"] == {"0": "background", "1": "liver"}
    assert d["modality"] == {"0": "CT"}
    assert d["numTraining"] == 1
    assert d["training"] == [
        {"image": "./imagesTr/c1_0000.nii.gz", "label": "./labelsTr/c1.nii.gz"}
    ]
    json.dumps(d)


# --------------------------------------------------------------------------
# windowing + PNG
# --------------------------------------------------------------------------
def test_window_to_uint8_clamps() -> None:
    slc = np.array([[-1000.0, 0.0, 1000.0]], dtype=np.float32)
    out = window_to_uint8(slc, wc=0.0, ww=400.0)
    assert out.dtype == np.uint8
    assert out[0, 0] == 0  # below window
    assert out[0, 2] == 255  # above window


def test_default_window_prefers_dicom_then_data_range() -> None:
    ds = _slice(0.0)
    ds.WindowCenter = 40
    ds.WindowWidth = 400
    assert default_window(ds, np.zeros((4, 4))) == (40.0, 400.0)
    bare = Dataset()
    arr = np.array([[0.0, 100.0]])
    wc, ww = default_window(bare, arr)
    assert wc == 50.0 and ww == 100.0


def test_encode_png_is_decodable() -> None:
    from PIL import Image

    arr = np.array([[0, 128], [255, 64]], dtype=np.uint8)
    png = encode_png(arr)
    back = np.array(Image.open(io.BytesIO(png)))
    assert np.array_equal(back, arr)


# --------------------------------------------------------------------------
# COCO RLE + builder
# --------------------------------------------------------------------------
def _rle_decode(rle: dict) -> np.ndarray:
    """Minimal column-major COCO RLE decoder for test verification."""
    h, w = rle["size"]
    flat = np.zeros(h * w, dtype=np.uint8)
    pos = 0
    val = 0
    for run in rle["counts"]:
        flat[pos : pos + run] = val
        pos += run
        val ^= 1
    return flat.reshape((h, w), order="F")


def test_rle_roundtrip() -> None:
    mask = np.array([[0, 1, 1], [1, 1, 0], [0, 0, 1]], dtype=np.uint8)
    rle = rle_encode_2d(mask)
    assert _rle_decode(rle).tolist() == mask.tolist()


def test_rle_handles_all_zero_and_all_one() -> None:
    z = np.zeros((3, 3), dtype=np.uint8)
    o = np.ones((3, 3), dtype=np.uint8)
    assert _rle_decode(rle_encode_2d(z)).sum() == 0
    assert _rle_decode(rle_encode_2d(o)).sum() == 9


def test_coco_builder_emits_images_and_annotations() -> None:
    b = CocoBuilder({"liver": 1, "tumor": 2})
    label = np.zeros((4, 5), dtype=np.uint8)
    label[1:3, 1:3] = 1  # liver block
    label[0, 4] = 2  # tumor pixel
    assert b.add_slice("images/c1_z0000.png", label) is True
    # Empty slice contributes nothing.
    assert b.add_slice("images/c1_z0001.png", np.zeros((4, 5), dtype=np.uint8)) is False
    coco = b.build()
    assert len(coco["images"]) == 1
    assert {a["category_id"] for a in coco["annotations"]} == {1, 2}
    liver_ann = next(a for a in coco["annotations"] if a["category_id"] == 1)
    assert liver_ann["bbox"] == [1, 1, 2, 2]  # xywh
    assert liver_ann["area"] == 4
    assert _rle_decode(liver_ann["segmentation"]).sum() == 4
    assert coco["categories"] == [{"id": 1, "name": "liver"}, {"id": 2, "name": "tumor"}]


def test_coco_builder_empty_has_no_images() -> None:
    b = CocoBuilder({"liver": 1})
    assert b.has_images() is False
    assert b.build()["images"] == []
