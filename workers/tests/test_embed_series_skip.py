"""``embed_series`` decode treats non-image objects as terminal skips.

A no-PixelData DICOM (an SR) and a degenerate non-image pixel layout (a SEG
label map collapses to one) must raise ``SeriesNotEmbeddable`` — a terminal
skip — NOT a ValueError/TypeError. That is what lets the worker return
``status='skipped'`` instead of writing an ``embedding_errors`` row and
triggering an arq retry storm on objects that can never embed.
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
