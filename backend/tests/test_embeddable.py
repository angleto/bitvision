"""Unit tests for the BiomedCLIP embeddability policy (services.embeddable).

This is the single source of truth deciding which DICOM series get enqueued
for image embedding. Non-image series (SR / PR / SEG / RT) must be excluded
by both the Modality blocklist and the SOP-class set, and the SQL clause used
at the enqueue sites must agree with the Python predicate.
"""

from __future__ import annotations

import pytest

from bvphoenix.services.embeddable import (
    NON_EMBEDDABLE_SOP_CLASSES,
    SeriesNotEmbeddable,
    embeddable_modality_clause,
    embeddable_sop_class_clause,
    is_embeddable_modality,
    is_embeddable_sop_class,
)
from bvphoenix.services.thumbnails import NO_PIXEL_DATA_SOP_CLASSES


@pytest.mark.parametrize(
    "modality", ["CT", "MR", "DX", "CR", "PT", "US", "NM", "XA", "MG", "OT", "SC"]
)
def test_image_modalities_are_embeddable(modality):
    assert is_embeddable_modality(modality)


@pytest.mark.parametrize(
    "modality", ["SR", "PR", "SEG", "KO", "REG", "RTSTRUCT", "RTPLAN", "RTDOSE", "DOC"]
)
def test_non_image_modalities_are_not_embeddable(modality):
    assert not is_embeddable_modality(modality)


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_null_or_blank_modality_is_let_through(raw):
    # Blocklist semantics: unknown defers to the worker pixel-decode backstop
    # so a real image with an unusual/missing Modality is never dropped.
    assert is_embeddable_modality(raw)


@pytest.mark.parametrize("raw", [" sr ", "Sr", "seg", " SEG "])
def test_modality_match_is_case_and_space_insensitive(raw):
    assert not is_embeddable_modality(raw)


def test_sop_class_image_vs_non_image():
    assert is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.2")  # CT Image
    assert is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.4")  # MR Image
    assert is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.4.1")  # Enhanced MR Image
    assert is_embeddable_sop_class(None)  # legacy / unknown
    assert not is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.88.11")  # Basic Text SR
    assert not is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.11.1")  # Pres State
    assert not is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.66.4")  # Segmentation
    assert not is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.66")  # Raw Data Storage
    assert not is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.481.3")  # RT Struct Set
    assert not is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.104.1")  # Encapsulated PDF


def test_raw_data_storage_is_non_embeddable():
    # Regression: Raw Data Storage (.66, distinct from the .66.x SEG/REG
    # variants) carries no standard PixelData. It was missing from the
    # no-pixel set, so the backfill re-offered such series forever and the
    # worker silently skipped them at decode (TCIA ReMIND / UPENN-GBM MR).
    assert "1.2.840.10008.5.1.4.1.1.66" in NO_PIXEL_DATA_SOP_CLASSES
    assert "1.2.840.10008.5.1.4.1.1.66" in NON_EMBEDDABLE_SOP_CLASSES
    assert not is_embeddable_sop_class("1.2.840.10008.5.1.4.1.1.66")


def test_sop_class_clause_is_sql_safe_and_agrees_with_predicate():
    clause = embeddable_sop_class_clause("i.sop_class_uid")
    assert "i.sop_class_uid IS NULL" in clause
    assert "NOT IN" in clause
    # a known non-image class is in the blocklist literal...
    assert "'1.2.840.10008.5.1.4.1.1.66'" in clause  # Raw Data
    assert "'1.2.840.10008.5.1.4.1.1.66.4'" in clause  # Segmentation
    # ...an image class is not
    assert "'1.2.840.10008.5.1.4.1.1.2'" not in clause  # CT Image
    assert ";" not in clause  # injection-safe: no statement terminator


def test_sop_set_extends_no_pixel_set_with_seg():
    # Built by extension, never recopied: every no-pixel class stays
    # non-embeddable, plus SEG which the no-pixel set deliberately omits
    # (a SEG carries frames, so it is not "no pixel data" — it is the
    # TypeError-on-decode culprit instead).
    assert NO_PIXEL_DATA_SOP_CLASSES <= NON_EMBEDDABLE_SOP_CLASSES
    assert "1.2.840.10008.5.1.4.1.1.66.4" in NON_EMBEDDABLE_SOP_CLASSES
    assert "1.2.840.10008.5.1.4.1.1.66.4" not in NO_PIXEL_DATA_SOP_CLASSES


def test_modality_clause_is_sql_safe_and_agrees_with_predicate():
    clause = embeddable_modality_clause("s.modality")
    assert "s.modality IS NULL" in clause
    assert "NOT IN" in clause
    assert "'SR'" in clause and "'SEG'" in clause
    # image modalities are NOT in the blocklist literal
    assert "'CT'" not in clause and "'MR'" not in clause
    # injection-safe: no statement terminator, only the column + quoted tokens
    assert ";" not in clause


def test_series_not_embeddable_carries_reason():
    exc = SeriesNotEmbeddable("no_pixel_data")
    assert exc.reason == "no_pixel_data"
    assert isinstance(exc, Exception)
