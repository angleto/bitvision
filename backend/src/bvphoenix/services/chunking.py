"""Generic text chunker for sub-document Q&A retrieval.

The Q&A orchestrator answers questions like "qual'è la classificazione
del tumore secondo l'istologico?" by retrieving small textual passages
and showing them as citations. Per-document embeddings are too coarse:
a 30-page histology report has one vector that does not localise the
specific paragraph that contains the answer. This module produces
contiguous sub-document slices ("chunks") that are then embedded and
indexed independently.

Strategy v1: ``sliding-v1-w800-o100-it``
    A simple sliding window over the whole document text, ~800
    characters per chunk with ~100 character overlap. The window is
    word-aligned so a chunk never starts or ends in the middle of a
    word; the actual character count fluctuates within ±~80 chars
    around the target. Page boundaries (when known via a
    ``page_offsets`` array) are honoured: a chunk never spans two
    pages, so cross-page concepts may be split (acceptable trade-off
    for the precision of citations).

Future strategies (e.g. a section-aware chunker for SR-style reports
with explicit "Findings" / "Impression" headings) will live as
sibling functions tagged with their own ``chunker_version`` so old and
new chunks coexist during rollout. Callers MUST persist the version
they used so the search layer can filter by it.
"""

from __future__ import annotations

from dataclasses import dataclass

from bvphoenix.db.models.text_chunks import DEFAULT_CHUNKER_VERSION

__all__ = [
    "DEFAULT_CHUNKER_VERSION",
    "OVERLAP_CHARS",
    "TARGET_CHARS",
    "Chunk",
    "chunk_document_text",
]


TARGET_CHARS = 800
OVERLAP_CHARS = 100
MIN_CHUNK_CHARS = 64


@dataclass(frozen=True)
class Chunk:
    """A contiguous slice of document text.

    ``char_start`` / ``char_end`` index into the original text so the
    caller can re-resolve the passage against the source for
    highlighting (the OCR ``bbox_words`` array uses the same indexing).

    ``page`` is set when a ``page_offsets`` array is supplied and the
    chunk falls entirely within one page; otherwise it is ``None``.
    """

    char_start: int
    char_end: int
    text: str
    page: int | None


def chunk_document_text(
    text: str,
    *,
    page_offsets: list[int] | None = None,
    target_chars: int = TARGET_CHARS,
    overlap_chars: int = OVERLAP_CHARS,
) -> list[Chunk]:
    """Split ``text`` into overlapping word-aligned chunks.

    Parameters
    ----------
    text:
        The full extracted document text. Whitespace is preserved.
    page_offsets:
        Optional list of character offsets where each page starts. If
        supplied, chunks are constrained to not span page boundaries
        and each chunk's ``page`` is populated. ``page_offsets[0]``
        must be ``0`` (start of page 1); the implicit end-of-text
        sentinel is ``len(text)``.
    target_chars, overlap_chars:
        Target window size and overlap. Defaults match
        ``DEFAULT_CHUNKER_VERSION``; pass other values only when
        introducing a new ``chunker_version``.

    Determinism: same input → same output, byte-for-byte. No locale
    or randomness involved.
    """

    if not text:
        return []
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be in [0, target_chars)")

    # Normalise page boundaries into a list of (start, end, page_no)
    # spans. A document with no page_offsets is treated as a single
    # span over the whole text with page=None.
    if page_offsets is None:
        spans: list[tuple[int, int, int | None]] = [(0, len(text), None)]
    else:
        if not page_offsets or page_offsets[0] != 0:
            raise ValueError("page_offsets must start at 0")
        spans = []
        for idx, start in enumerate(page_offsets):
            end = page_offsets[idx + 1] if idx + 1 < len(page_offsets) else len(text)
            if end < start:
                raise ValueError("page_offsets must be monotonically non-decreasing")
            if end > start:
                spans.append((start, end, idx + 1))

    chunks: list[Chunk] = []
    for span_start, span_end, page in spans:
        chunks.extend(
            _chunk_span(
                text,
                span_start=span_start,
                span_end=span_end,
                page=page,
                target_chars=target_chars,
                overlap_chars=overlap_chars,
            )
        )
    return chunks


def _chunk_span(
    text: str,
    *,
    span_start: int,
    span_end: int,
    page: int | None,
    target_chars: int,
    overlap_chars: int,
) -> list[Chunk]:
    """Slice a single contiguous span into overlapping chunks."""

    span_len = span_end - span_start
    if span_len <= 0:
        return []
    if span_len <= target_chars:
        # Single chunk for the whole span; trim leading/trailing
        # whitespace boundaries so the stored text is clean but the
        # offsets still reference the original document.
        start, end = _trim_whitespace_offsets(text, span_start, span_end)
        if end - start < MIN_CHUNK_CHARS and span_len < MIN_CHUNK_CHARS:
            # Tiny page (e.g. a near-blank scan); keep it anyway —
            # losing the page entirely would hurt recall on documents
            # whose only meaningful content is short.
            start, end = span_start, span_end
        return [Chunk(char_start=start, char_end=end, text=text[start:end], page=page)]

    chunks: list[Chunk] = []
    cursor = span_start
    stride = target_chars - overlap_chars
    while cursor < span_end:
        target_end = min(cursor + target_chars, span_end)
        # Snap end to a word boundary if not already at the span end.
        end = target_end if target_end == span_end else _snap_back_to_word(text, target_end, cursor)
        # Snap start to a word boundary going forward (skip leading
        # space that overlap may have included).
        start = _snap_forward_to_word(text, cursor, end)
        if end - start >= MIN_CHUNK_CHARS:
            chunks.append(
                Chunk(
                    char_start=start,
                    char_end=end,
                    text=text[start:end],
                    page=page,
                )
            )
        if target_end == span_end:
            break
        cursor += stride
    return chunks


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _snap_back_to_word(text: str, end: int, lower_bound: int) -> int:
    """Move ``end`` left until the previous char is a word boundary.

    Never crosses ``lower_bound``: if no boundary is found within the
    window, returns the original ``end`` so we still emit a chunk.
    """

    if end <= 0 or end >= len(text):
        return end
    cursor = end
    while cursor > lower_bound and _is_word_char(text[cursor - 1]) and _is_word_char(text[cursor]):
        cursor -= 1
    return cursor if cursor > lower_bound else end


def _snap_forward_to_word(text: str, start: int, upper_bound: int) -> int:
    """Move ``start`` right until it lands on a word/boundary char."""

    cursor = start
    while cursor < upper_bound and text[cursor].isspace():
        cursor += 1
    return cursor


def _trim_whitespace_offsets(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
