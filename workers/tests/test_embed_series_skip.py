"""``embed_series`` DICOM decode: terminal skips AND the multi-frame path.

Two halves, both exercising ``_dicom_to_pil``:

* Terminal skips — a no-PixelData DICOM (an SR) and a degenerate non-image
  pixel layout (a SEG label map collapses to one) must raise
  ``SeriesNotEmbeddable`` (a terminal skip), NOT a ValueError/TypeError.
  That is what lets the worker return ``status='skipped'`` instead of
  writing an ``embedding_errors`` row and triggering an arq retry storm on
  objects that can never embed.

* Multi-frame decode — an Enhanced MR (SOP class
  ``1.2.840.10008.5.1.4.1.1.4.1``) and any multi-frame series arrive as a
  3-D ``(frames, H, W)`` or 4-D ``(frames, H, W, C)`` ``pixel_array``.
  These MUST collapse to a single representative middle frame and yield a
  valid RGB image — they must NOT be skipped. This is the resolution of the
  Flow task 100ecc3b "open question": modern multi-frame MR collections
  (TCIA ReMIND / UPENN-GBM) are embeddable, so the visual-search index
  covers them. Only Raw Data Storage (``.66``) is legitimately excluded,
  and that happens upstream via the SOP-class filter — see
  ``backend/tests/test_embeddable.py`` and
  ``backend/tests/test_embed_backfill_candidates.py``.
"""

from __future__ import annotations

import numpy as np
import pytest

# thumbnails (pulled in transitively by services.embeddable) imports PIL; skip
# cleanly on a minimal env that lacks it rather than erroring at collection.
pytest.importorskip("PIL")

from bvphoenix.services.embeddable import SeriesNotEmbeddable

from bvworkers.tasks import embed_series as mod


class _FakeNoPixelDS:
    def __contains__(self, key: str) -> bool:
        return False


class _FakeOddLayoutDS:
    """Has PixelData but decodes to a non-displayable (1, 1, N) buffer —
    the canonical SEG/odd-layout shape that PIL cannot handle."""

    def __contains__(self, key: str) -> bool:
        return key == "PixelData"

    @property
    def pixel_array(self):
        return np.zeros((1, 1, 512), dtype=np.uint16)


class _FakePixelDS:
    """Has PixelData; ``pixel_array`` returns the array it is constructed with.

    Carries no ``RescaleSlope`` / ``RescaleIntercept`` attributes, so
    ``_dicom_to_pil`` falls back to the 1.0 / 0.0 defaults via ``getattr`` —
    exactly the no-rescale Enhanced-MR case.
    """

    def __init__(self, arr: np.ndarray) -> None:
        self._arr = arr

    def __contains__(self, key: str) -> bool:
        return key == "PixelData"

    @property
    def pixel_array(self):
        return self._arr


def test_no_pixel_data_is_terminal_skip(monkeypatch):
    monkeypatch.setattr(mod.pydicom, "dcmread", lambda *a, **k: _FakeNoPixelDS())
    with pytest.raises(SeriesNotEmbeddable) as ei:
        mod._dicom_to_pil(b"ignored")
    assert ei.value.reason == "no_pixel_data"


def test_unsupported_layout_is_terminal_skip(monkeypatch):
    monkeypatch.setattr(mod.pydicom, "dcmread", lambda *a, **k: _FakeOddLayoutDS())
    with pytest.raises(SeriesNotEmbeddable) as ei:
        mod._dicom_to_pil(b"ignored")
    assert ei.value.reason == "unsupported_pixel_layout"


def test_single_frame_grayscale_decodes_to_rgb(monkeypatch):
    # Baseline: a plain 2-D (H, W) grayscale slice decodes to an RGB image.
    arr = np.arange(16 * 16, dtype=np.uint16).reshape(16, 16)
    monkeypatch.setattr(mod.pydicom, "dcmread", lambda *a, **k: _FakePixelDS(arr))
    img = mod._dicom_to_pil(b"ignored")
    assert img.mode == "RGB"
    assert img.size == (16, 16)  # PIL size is (W, H)


def test_multiframe_grayscale_collapses_to_middle_frame(monkeypatch):
    # Enhanced MR / any multi-frame grayscale arrives as (frames, H, W). It
    # MUST collapse to one middle frame and decode — never a terminal skip
    # (Flow task 100ecc3b open question). A returned (16, 16) image proves a
    # single frame was extracted, not the whole 5-frame stack.
    arr = np.arange(5 * 16 * 16, dtype=np.uint16).reshape(5, 16, 16)
    monkeypatch.setattr(mod.pydicom, "dcmread", lambda *a, **k: _FakePixelDS(arr))
    img = mod._dicom_to_pil(b"ignored")
    assert img.mode == "RGB"
    assert img.size == (16, 16)


def test_multiframe_rgb_collapses_to_middle_frame(monkeypatch):
    # Multi-frame colour: (frames, H, W, 3). The 4-D path takes the middle
    # frame and yields a valid RGB image of the per-frame size.
    arr = np.arange(5 * 16 * 16 * 3, dtype=np.uint8).reshape(5, 16, 16, 3)
    monkeypatch.setattr(mod.pydicom, "dcmread", lambda *a, **k: _FakePixelDS(arr))
    img = mod._dicom_to_pil(b"ignored")
    assert img.mode == "RGB"
    assert img.size == (16, 16)
