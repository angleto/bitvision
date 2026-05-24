"""Unit tests for the generic document chunker."""

from __future__ import annotations

import itertools

import pytest

from bvphoenix.services.chunking import (
    OVERLAP_CHARS,
    TARGET_CHARS,
    Chunk,
    chunk_document_text,
)


def test_empty_input_returns_no_chunks() -> None:
    assert chunk_document_text("") == []


def test_short_text_yields_single_chunk() -> None:
    text = "Referto anatomopatologico breve, una sola pagina."
    chunks = chunk_document_text(text)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.char_start == 0
    assert chunk.char_end == len(text)
    assert chunk.text == text
    assert chunk.page is None


def test_long_text_produces_overlapping_chunks() -> None:
    # ~3000 chars: with 800-target/100-overlap stride 700, expect ~5 chunks.
    text = ("Carcinoma duttale infiltrante della mammella destra. " * 60).strip()
    chunks = chunk_document_text(text)
    assert len(chunks) >= 4
    # offsets must be monotonically non-decreasing
    for prev, curr in itertools.pairwise(chunks):
        assert curr.char_start >= prev.char_start
        # overlap: each chunk start sits inside the previous chunk's range
        assert curr.char_start < prev.char_end
    # every chunk fits within the target+slack window
    for c in chunks:
        assert c.char_end - c.char_start <= TARGET_CHARS + 32


def test_chunks_are_word_aligned() -> None:
    text = "alpha bravo charlie delta echo foxtrot golf hotel " * 50
    chunks = chunk_document_text(text)
    for c in chunks:
        # never start with whitespace
        assert not c.text.startswith(" ")
        # never split a word at the boundary (start char must be word-start)
        if c.char_start > 0:
            assert text[c.char_start - 1].isspace() or not text[c.char_start].isalnum()


def test_page_offsets_are_honoured_and_no_chunk_spans_pages() -> None:
    page_a = "Pagina uno. " * 80  # ~960 chars → 2 chunks
    page_b = "Pagina due. " * 80
    text = page_a + page_b
    page_offsets = [0, len(page_a)]

    chunks = chunk_document_text(text, page_offsets=page_offsets)

    pages = {c.page for c in chunks}
    assert pages == {1, 2}
    for c in chunks:
        if c.page == 1:
            assert c.char_end <= len(page_a)
        else:
            assert c.char_start >= len(page_a)


def test_page_offsets_must_start_at_zero() -> None:
    with pytest.raises(ValueError, match="page_offsets must start at 0"):
        chunk_document_text("abc", page_offsets=[5])


def test_chunker_is_deterministic() -> None:
    text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 40
    a = chunk_document_text(text)
    b = chunk_document_text(text)
    assert a == b


def test_returns_chunk_dataclass_instances() -> None:
    chunks = chunk_document_text("breve nota clinica.")
    assert all(isinstance(c, Chunk) for c in chunks)


def test_overlap_default_matches_module_constant() -> None:
    # A 1500-char paragraph should overlap by approximately OVERLAP_CHARS
    # between consecutive chunks (modulo word-snap rounding ≤ ~32).
    text = "x" * 1500
    chunks = chunk_document_text(text)
    if len(chunks) >= 2:
        actual_overlap = chunks[0].char_end - chunks[1].char_start
        assert abs(actual_overlap - OVERLAP_CHARS) <= 32
