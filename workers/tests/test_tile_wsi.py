"""Tests for the WSI DZI tiling worker.

The disk-discipline upload walk (``_upload_pyramid``) and the reader
routing run hermetically here (a fake S3 client + a tmp DZI tree, no real
gigapixel slide, no libvips). The full ``tile_wsi`` orchestration
(idempotency / capability probe / PHI guard) is DB-touching and gated on
a reachable Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bvworkers.tasks import tile_wsi as mod


class _FakeS3:
    """Records put_object calls without touching the network."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, str, bytes, str]] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str, **_kw):  # noqa: N803
        self.puts.append((Bucket, Key, Body, ContentType))


def _write_dzi_tree(root: Path) -> tuple[str, str]:
    """Build a minimal DeepZoom output (image.dzi + image_files/<lvl>/<c>_<r>.jpg)
    the way pyvips dzsave would, with two levels."""
    base = root / "image"
    files = root / "image_files"
    (files / "0").mkdir(parents=True)
    (files / "1").mkdir(parents=True)
    (files / "0" / "0_0.jpg").write_bytes(b"tile-0-0-0")
    (files / "1" / "0_0.jpg").write_bytes(b"tile-1-0-0")
    (files / "1" / "1_0.jpg").write_bytes(b"tile-1-1-0")
    descriptor = root / "image.dzi"
    descriptor.write_bytes(b"<Image TileSize='512' Overlap='0' Format='jpeg'/>")
    return str(base), str(files)


def test_upload_pyramid_uploads_all_tiles_plus_descriptor_and_unlinks(tmp_path: Path) -> None:
    base, files_dir = _write_dzi_tree(tmp_path)
    s3 = _FakeS3()
    slide_id = "11111111-1111-1111-1111-111111111111"

    n = mod._upload_pyramid(
        s3=s3,
        bucket="bvphoenix-derivatives",
        slide_id=slide_id,
        files_dir=files_dir,
        descriptor_path=f"{base}.dzi",
        put_extra={},
    )

    # 3 tiles + 1 descriptor.
    assert n == 4
    keys = {k for (_b, k, _body, _ct) in s3.puts}
    assert f"pathology/{slide_id}/dzi/image_files/0/0_0.jpg" in keys
    assert f"pathology/{slide_id}/dzi/image_files/1/1_0.jpg" in keys
    assert f"pathology/{slide_id}/dzi/image.dzi" in keys
    # The descriptor is the readiness marker — uploaded LAST.
    assert s3.puts[-1][1] == f"pathology/{slide_id}/dzi/image.dzi"
    assert s3.puts[-1][3] == "application/xml"
    # Every tile + level dir was unlinked as we drained it (bounded disk).
    assert not os.path.isdir(os.path.join(files_dir, "0"))
    assert not os.path.isdir(os.path.join(files_dir, "1"))


def test_upload_pyramid_empty_tree_is_noop(tmp_path: Path) -> None:
    s3 = _FakeS3()
    n = mod._upload_pyramid(
        s3=s3,
        bucket="b",
        slide_id="s",
        files_dir=str(tmp_path / "missing"),
        descriptor_path=str(tmp_path / "missing.dzi"),
        put_extra={},
    )
    assert n == 0
    assert s3.puts == []


def test_wsi_formats_route_through_openslide() -> None:
    """WSI source formats need the openslide reader; ordinary image classes
    use the plain libvips loader."""
    for fmt in ("svs", "ndpi", "ome-tiff", "dicom-wsi", "mrxs", "scn"):
        assert fmt in mod._WSI_READER_FORMATS
    for fmt in ("jpeg", "png", "image", "tiff"):
        assert fmt not in mod._WSI_READER_FORMATS


@pytest.mark.parametrize(
    ("slide_class", "source_format", "expect_openslide"),
    [
        ("wsi", "svs", True),
        ("wsi", "other", True),  # class wins
        ("micrograph", "png", False),
        ("gross", "jpeg", False),
    ],
)
def test_reader_decision(slide_class: str, source_format: str, expect_openslide: bool) -> None:
    use_openslide = slide_class == "wsi" or source_format in mod._WSI_READER_FORMATS
    assert use_openslide is expect_openslide
