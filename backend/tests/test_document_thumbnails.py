"""Unit tests for the document → JPEG thumbnail service.

Pure functions, no DB / S3 / FastAPI involved. Covers the dispatch
logic + a small synthesised PDF and a small synthesised PNG so the
JPEG output is verifiable end to end.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bvphoenix.services.document_thumbnails import (
    UnsupportedThumbnailKindError,
    image_to_thumbnail,
    is_supported_thumbnail_mime,
    pdf_first_page_to_jpeg,
    render_document_thumbnail,
)


def _png_bytes(size: tuple[int, int] = (320, 240), color=(80, 120, 200)) -> bytes:
    img = Image.new("RGB", size, color)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _rgba_png_bytes() -> bytes:
    img = Image.new("RGBA", (40, 40), (255, 0, 0, 128))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _one_page_pdf_bytes() -> bytes:
    """Synthesise a tiny 1-page PDF via pypdfium2 helpers.

    pypdfium2 doesn't expose a "create a PDF from scratch" API in the
    pure-Python layer, so we build the smallest valid PDF by hand.
    The resulting file is a single A4 page with no rendered content;
    pypdfium2 still rasters it (white page) which is enough to assert
    the rendering pipeline runs end-to-end.
    """
    # Minimal valid PDF (taken from the PDF 1.4 spec example, 4-object
    # cross-reference table with one empty page).
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
        b" /Resources << >> >>\nendobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000111 00000 n \n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
        b"startxref\n195\n%%EOF\n"
    )


def test_is_supported_thumbnail_mime() -> None:
    assert is_supported_thumbnail_mime("application/pdf", "report.pdf")
    assert is_supported_thumbnail_mime("image/png", "scan.png")
    assert is_supported_thumbnail_mime("image/jpeg", "scan.jpg")
    # Filename fallback when the MIME type is missing.
    assert is_supported_thumbnail_mime(None, "scan.JPG")
    assert is_supported_thumbnail_mime("", "report.pdf")
    # Negatives.
    assert not is_supported_thumbnail_mime("text/plain", "note.txt")
    assert not is_supported_thumbnail_mime("application/octet-stream", "blob")
    assert not is_supported_thumbnail_mime(None, None)


def test_image_thumbnail_downscales_and_outputs_jpeg() -> None:
    src = _png_bytes(size=(800, 600))
    out = image_to_thumbnail(src, max_side=128)
    img = Image.open(io.BytesIO(out))
    img.load()
    assert img.format == "JPEG"
    assert max(img.size) <= 128
    # Aspect ratio preserved.
    assert img.size == (128, 96)


def test_image_thumbnail_handles_rgba() -> None:
    src = _rgba_png_bytes()
    out = image_to_thumbnail(src, max_side=64)
    img = Image.open(io.BytesIO(out))
    img.load()
    assert img.format == "JPEG"
    assert img.mode == "RGB"


def test_image_thumbnail_does_not_upscale() -> None:
    # A 32x32 source must not be upscaled to 128x128.
    src = _png_bytes(size=(32, 32))
    out = image_to_thumbnail(src, max_side=128)
    img = Image.open(io.BytesIO(out))
    img.load()
    assert max(img.size) == 32


def test_pdf_thumbnail_renders_first_page() -> None:
    out = pdf_first_page_to_jpeg(_one_page_pdf_bytes(), max_side=200)
    img = Image.open(io.BytesIO(out))
    img.load()
    assert img.format == "JPEG"
    assert max(img.size) <= 200
    # A4 portrait at 595x842 pt downscaled to 200 long edge stays in
    # portrait orientation (height ≥ width).
    assert img.size[1] >= img.size[0]


def test_render_document_dispatch() -> None:
    pdf_out = render_document_thumbnail(
        _one_page_pdf_bytes(),
        content_type="application/pdf",
        filename="x.pdf",
        max_side=128,
    )
    assert Image.open(io.BytesIO(pdf_out)).format == "JPEG"

    img_out = render_document_thumbnail(
        _png_bytes(),
        content_type="image/png",
        filename="x.png",
        max_side=128,
    )
    assert Image.open(io.BytesIO(img_out)).format == "JPEG"


def test_render_document_unsupported_raises() -> None:
    with pytest.raises(UnsupportedThumbnailKindError):
        render_document_thumbnail(
            b"plain text body",
            content_type="text/plain",
            filename="note.txt",
        )
