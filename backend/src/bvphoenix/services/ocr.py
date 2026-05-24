"""OCR pipeline (Sprint 3, ADR 0007).

The pipeline is two-stage:

1. **pdfminer text-layer**: cheap, deterministic, no rasterisation.
   Best for born-digital PDFs where the text layer is present (most
   modern reports, prescriptions, lab results).
2. **Tesseract italian fallback**: for image-only scans (paper
   documents photographed with a phone, faxed reports). Pages are
   rasterised via ``pypdfium2`` and fed to Tesseract per page; the
   ``bbox_words`` field is populated from the TSV output so the
   frontend can render highlighters.

Engine versioning is encoded as a single string ``ocr_engine_version``
that varies whenever the upstream library or configuration changes —
bumping it invalidates the ``document_ocr`` cache.

The service is callable both inline (single-file API request) and from
the worker (long-running ingestions). Failures bubble up; the caller
decides whether to swallow.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# Bumping any of these forces a re-OCR on next read of every cached
# row that pinned the older string. Match the major version of the
# underlying library so the cache invalidates whenever we upgrade.
PDFMINER_ENGINE_VERSION = "pdfminer-2024.07"
# v3 = full European pack installed (24 EU official languages + non-EU
# European). The default ``BVP_OCR_LANGUAGES`` set still loads the
# conservative 4-lang subset, but the engine_version bump invalidates
# v2 cache rows so a re-read after this deploy can re-extract under
# any language the agent now picks (an Italian assistant that
# classifies a Polish referral as ``language="pol"`` will not be
# stuck with an old Italian-only OCR result).
TESSERACT_ENGINE_VERSION = "tesseract-5.x-multi-v3"


@dataclass(slots=True)
class OCRResult:
    text: str
    engine: str
    engine_version: str
    page_count: int | None = None
    bbox_words: list[dict[str, Any]] | None = None
    sha256: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _try_pdf_text_layer(data: bytes) -> tuple[str, int | None] | None:
    """Return ``(text, page_count)`` extracted from the PDF text layer,
    or ``None`` if the layer is empty / extraction failed.
    """
    try:
        from pdfminer.high_level import extract_text
        from pdfminer.pdfpage import PDFPage
    except ImportError:  # pragma: no cover - pdfminer is a hard dep now
        return None
    try:
        text = extract_text(io.BytesIO(data)) or ""
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("pdfminer extract_text failed: %s", exc)
        return None
    text = text.strip()
    if not text:
        return None
    try:
        page_count = sum(1 for _ in PDFPage.get_pages(io.BytesIO(data)))
    except Exception:  # pragma: no cover - rare
        page_count = None
    return text, page_count


def _default_languages() -> str:
    """Pull the configured Tesseract language tag.

    Wrapped in a function so unit tests can monkeypatch the settings
    without poking at module-level state. Importing ``get_settings`` at
    call time also avoids a hard import cycle when ``services.ocr`` is
    imported from the worker before the FastAPI settings are bound.
    """
    try:
        from bvphoenix.config import get_settings

        return get_settings().ocr_languages or "ita+eng+deu+fra"
    except Exception:  # pragma: no cover - defensive (e.g. settings unloadable)
        return "ita+eng+deu+fra"


def _tesseract_fallback(
    data: bytes,
    *,
    mime: str,
    language: str | None = None,
) -> OCRResult | None:
    """Rasterise + OCR via Tesseract.

    ``language`` defaults to ``settings.ocr_languages`` (ita+eng+deu+fra)
    so multilingual clinical scans (terminology in English, cross-border
    documents in German/French) all OCR in one pass. Caller can override
    when a single language is known a priori.

    Returns ``None`` when the binary cannot be rasterised. Failures
    inside Tesseract (missing language pack, missing binary) raise so
    the caller can short-circuit.
    """
    if language is None:
        language = _default_languages()
    try:
        import pytesseract
    except ImportError:  # pragma: no cover - pytesseract is a hard dep now
        return None

    pages: list[dict[str, Any]] = []
    bbox_words: list[dict[str, Any]] = []
    text_chunks: list[str] = []

    if mime == "application/pdf":
        try:
            import pypdfium2 as pdfium
        except ImportError:  # pragma: no cover
            return None
        pdf = pdfium.PdfDocument(data)
        try:
            for page_idx in range(len(pdf)):
                page = pdf[page_idx]
                # 200 DPI is a sane default for clinical scans;
                # higher costs RAM, lower hurts accuracy.
                bitmap = page.render(scale=200 / 72).to_pil()
                page_text = pytesseract.image_to_string(bitmap, lang=language)
                text_chunks.append(page_text)
                # TSV output for per-word bboxes (slow on large pages,
                # but the agent UI needs it for highlighters).
                tsv = pytesseract.image_to_data(
                    bitmap,
                    lang=language,
                    output_type=pytesseract.Output.DICT,
                )
                for i, word in enumerate(tsv.get("text", [])):
                    if not word or not word.strip():
                        continue
                    bbox_words.append(
                        {
                            "page": page_idx,
                            "x": int(tsv["left"][i]),
                            "y": int(tsv["top"][i]),
                            "w": int(tsv["width"][i]),
                            "h": int(tsv["height"][i]),
                            "text": word,
                            "conf": int(tsv.get("conf", [0] * len(tsv["text"]))[i] or 0),
                        }
                    )
                pages.append({"page": page_idx, "n_words": len(text_chunks[-1].split())})
        finally:
            pdf.close()
    elif mime.startswith("image/"):
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover - Pillow is a hard dep
            return None
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                page_text = pytesseract.image_to_string(img, lang=language)
                text_chunks.append(page_text)
                tsv = pytesseract.image_to_data(
                    img,
                    lang=language,
                    output_type=pytesseract.Output.DICT,
                )
                for i, word in enumerate(tsv.get("text", [])):
                    if not word or not word.strip():
                        continue
                    bbox_words.append(
                        {
                            "page": 0,
                            "x": int(tsv["left"][i]),
                            "y": int(tsv["top"][i]),
                            "w": int(tsv["width"][i]),
                            "h": int(tsv["height"][i]),
                            "text": word,
                            "conf": int(tsv.get("conf", [0] * len(tsv["text"]))[i] or 0),
                        }
                    )
                pages.append({"page": 0, "n_words": len(text_chunks[-1].split())})
        except Exception as exc:  # pragma: no cover
            log.warning("PIL/Tesseract pipeline failed: %s", exc)
            return None
    else:
        return None

    return OCRResult(
        text="\n\n".join(t.strip() for t in text_chunks).strip(),
        engine="tesseract",
        engine_version=TESSERACT_ENGINE_VERSION,
        page_count=len(pages) if pages else None,
        bbox_words=bbox_words or None,
        extra={"pages": pages, "language": language},
    )


def run_ocr(
    data: bytes,
    *,
    mime: str | None,
    language: str | None = None,
) -> OCRResult:
    """Run the OCR pipeline on a binary payload.

    Order:

    1. ``application/pdf`` → try the text layer; if non-empty, return
       a pdfminer-tagged result. ``language`` is irrelevant here — the
       text layer is whatever the producer embedded.
    2. Otherwise, fall back to Tesseract (raster). ``language`` controls
       the traineddata Tesseract loads:

       * ``None`` or ``"auto"`` (default) — multilingual mode using
         every language listed in ``settings.ocr_languages``
         (``ita+eng+deu+fra`` out of the box). Tesseract picks the best
         match per region, so mixed-language clinical documents work in
         a single pass without the caller knowing the language.
       * a single tag (``"ita"``, ``"eng"``, ``"deu"``, ``"fra"``) —
         restricts Tesseract to that language. Faster and slightly more
         accurate when the document language is known a priori, e.g.
         the agent has classified the doc as a German referral.
       * a custom ``+``-joined tag (``"ita+eng"``) — explicit subset.

    Raises :class:`RuntimeError` when neither path produces output.
    """
    sha256 = _sha256_hex(data)
    mime = (mime or "application/octet-stream").lower()

    if mime == "application/pdf":
        layer = _try_pdf_text_layer(data)
        if layer is not None:
            text, page_count = layer
            return OCRResult(
                text=text,
                engine="pdfminer",
                engine_version=PDFMINER_ENGINE_VERSION,
                page_count=page_count,
                bbox_words=None,
                sha256=sha256,
            )

    resolved_lang = _default_languages() if language is None or language == "auto" else language
    fallback = _tesseract_fallback(data, mime=mime, language=resolved_lang)
    if fallback is None or not fallback.text.strip():
        raise RuntimeError(
            f"OCR pipeline produced no text for mime={mime!r} lang={resolved_lang!r}; "
            "neither the PDF text layer nor Tesseract recovered content."
        )
    fallback.sha256 = sha256
    return fallback


__all__ = [
    "PDFMINER_ENGINE_VERSION",
    "TESSERACT_ENGINE_VERSION",
    "OCRResult",
    "run_ocr",
]
