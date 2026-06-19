"""Construct S3 keys for pathology-slide DeepZoom tile pyramids.

Same safety rationale as ``services.derivative_keys`` for DICOM: the
storage address is keyed on values BitVision controls — the internal
``PathologySlide.id`` (a UUID, collision-impossible across tenants) —
NOT on the scanner-supplied ``slide_instance_uid`` (which a buggy
scanner or a malicious upload could collide, overwriting another
patient's tiles).

The pyramid is rendered by the ``tile_wsi`` worker with pyvips
``dzsave`` using basename ``image``, which emits::

    pathology/<slide_id>/dzi/image.dzi
    pathology/<slide_id>/dzi/image_files/<level>/<col>_<row>.jpg

All under the derivatives bucket; never exposed to clients (the API
proxies tile bytes). The descriptor key is persisted on
``PathologySlide.s3_dzi_key``; the per-tile keys are recomputed from
``(level, col, row)`` on each request, never listed.
"""

from __future__ import annotations

import uuid

__all__ = [
    "DZI_BASENAME",
    "dzi_descriptor_key",
    "dzi_files_prefix",
    "dzi_prefix",
    "dzi_tile_key",
]

# pyvips dzsave basename. Kept as a constant so the worker (which passes
# it to dzsave) and the serving layer (which reads ``image_files/...``)
# cannot drift.
DZI_BASENAME = "image"


def dzi_prefix(slide_id: uuid.UUID | str) -> str:
    """Per-slide DZI directory prefix in the derivatives bucket."""
    return f"pathology/{slide_id}/dzi"


def dzi_descriptor_key(slide_id: uuid.UUID | str) -> str:
    """S3 key for the ``.dzi`` DeepZoom descriptor (XML)."""
    return f"{dzi_prefix(slide_id)}/{DZI_BASENAME}.dzi"


def dzi_files_prefix(slide_id: uuid.UUID | str) -> str:
    """S3 prefix holding the tile tree (``<level>/<col>_<row>.<fmt>``)."""
    return f"{dzi_prefix(slide_id)}/{DZI_BASENAME}_files"


def dzi_tile_key(
    slide_id: uuid.UUID | str,
    level: int,
    col: int,
    row: int,
    *,
    fmt: str = "jpg",
) -> str:
    """S3 key for one DeepZoom tile."""
    return f"{dzi_files_prefix(slide_id)}/{level}/{col}_{row}.{fmt}"
