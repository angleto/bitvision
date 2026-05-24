"""Patient-document → JPEG thumbnail conversion for the grid card.

PDFs are rasterised to the first page via ``pypdfium2`` (no system
poppler / qpdf dependency). Raster images (PNG / JPG / WEBP / TIFF /
GIF) are downscaled with PIL preserving aspect ratio. Anything else
raises ``UnsupportedThumbnailKindError`` so the caller can return a
404 and the frontend falls back to a generic icon.

Both conversions cap the long edge at ``max_side`` and emit JPEG.
Quality stays low (75) by default; thumbnails are decorative, not
diagnostic.
"""

from __future__ import annotations

import io

from PIL import Image


class UnsupportedThumbnailKindError(Exception):
    pass


def _normalise_to_rgb(img: Image.Image) -> Image.Image:
    """Return an RGB copy of ``img`` so JPEG encoding can't trip on
    palette / RGBA / 1-bit modes. Transparent pixels get composited on
    a white background, matching the document-on-paper convention.
    """
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def _downscale(img: Image.Image, max_side: int) -> Image.Image:
    """Shrink ``img`` so the long edge is ≤ ``max_side``.

    Uses ``Image.thumbnail`` (in-place) which preserves aspect ratio
    and only ever shrinks; small images are returned unchanged so we
    don't upscale a tiny document into something blurry.
    """
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


def image_to_thumbnail(
    img_bytes: bytes,
    *,
    quality: int = 75,
    max_side: int = 512,
) -> bytes:
    """Render a raster image to a JPEG thumbnail."""
    with Image.open(io.BytesIO(img_bytes)) as img:
        img.load()
        img = _normalise_to_rgb(img)
        img = _downscale(img, max_side)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()


def pdf_first_page_to_jpeg(
    pdf_bytes: bytes,
    *,
    quality: int = 75,
    max_side: int = 512,
) -> bytes:
    """Render the first page of a PDF to a JPEG thumbnail.

    The PDF is rasterised at a scale chosen so the longer edge of the
    final image matches ``max_side`` after a single downscale pass.
    Raises ``UnsupportedThumbnailKindError`` when the PDF has zero
    pages (rare but possible for malformed uploads).
    """
    # Imported lazily so the rest of the module doesn't pay the
    # startup cost when the caller only ever rasterises raster
    # images. ``pypdfium2`` brings its own bundled libpdfium.
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        if len(pdf) == 0:
            raise UnsupportedThumbnailKindError("PDF has no pages")
        page = pdf[0]
        # pypdfium2 takes a "scale" multiplier applied to the page's
        # native size (PDF points: 72 dpi). Pick a scale that lands
        # roughly at ``max_side`` on the longer edge so the LANCZOS
        # downscale below has minimal work to do.
        width_pt, height_pt = page.get_size()
        long_pt = max(width_pt, height_pt)
        # 72 pt = 1 inch; render at ~max_side pixels on the long edge.
        scale = max(0.5, min(4.0, max_side / long_pt))
        bitmap = page.render(scale=scale)
        img = bitmap.to_pil()
        bitmap.close()
        page.close()
    finally:
        pdf.close()

    img = _normalise_to_rgb(img)
    img = _downscale(img, max_side)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def is_supported_thumbnail_mime(content_type: str | None, filename: str | None) -> bool:
    """Cheap pre-check the API layer can use to skip 404s.

    True when the document is a PDF or a raster image we know how to
    render. Mirrors the dispatch in :func:`render_document_thumbnail`.
    """
    ct = (content_type or "").lower()
    ext = (filename or "").lower().rsplit(".", 1)[-1] if filename else ""
    if ct == "application/pdf" or ext == "pdf":
        return True
    if ct.startswith("image/"):
        return True
    return ext in ("png", "jpg", "jpeg", "webp", "gif", "tif", "tiff")


def render_document_thumbnail(
    body: bytes,
    *,
    content_type: str | None,
    filename: str | None,
    max_side: int = 512,
    quality: int = 75,
) -> bytes:
    """Dispatch to the right renderer based on MIME / extension.

    Raises ``UnsupportedThumbnailKindError`` for content the layer
    can't handle (text, markdown, archives, octet-stream, ...).
    """
    ct = (content_type or "").lower()
    ext = (filename or "").lower().rsplit(".", 1)[-1] if filename else ""
    if ct == "application/pdf" or ext == "pdf":
        return pdf_first_page_to_jpeg(body, quality=quality, max_side=max_side)
    if ct.startswith("image/") or ext in ("png", "jpg", "jpeg", "webp", "gif", "tif", "tiff"):
        return image_to_thumbnail(body, quality=quality, max_side=max_side)
    raise UnsupportedThumbnailKindError(
        f"no thumbnail renderer for content_type={content_type!r} ext={ext!r}"
    )
