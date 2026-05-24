"""Universal file-type classifier for uploads.

The ingestion path accepts many media families — DICOM studies, PDF
referti, rasterised scans, free-text notes, audio dictations, video
clips — and each one fans out to a different downstream pipeline. This
module is the single choke point that inspects a byte buffer plus the
caller-supplied hints and decides which ``FileKind`` it is.

Signal priority (first non-``UNKNOWN`` wins):

1. **Magic bytes** — authoritative when present. The client cannot
   forge the header without actually producing the right payload, so
   this is the strongest signal. DICOM is special-cased because its
   preamble is 128 NUL bytes + ``DICM`` at offset 128.
2. **Content-Type hint** — what the HTTP layer declared. Useful when
   magic is ambiguous (e.g. the file is too short) but we still
   distrust it, which is why it loses to magic.
3. **Filename extension** — last resort, cheap and often wrong but
   good enough for plain-text formats where magic bytes do not exist.

When none of the three produce a match, ``FileKind.UNKNOWN`` is
returned with ``confidence=0.0`` — the caller decides whether to
reject the upload or fall back to generic binary handling.

``suggested_document_type`` is populated only for kinds where the
downstream pipeline cares about subtype classification (PDF / TEXT /
IMAGE) and only if Unit U4's ``guess_document_type`` helper is
importable. The import is deferred to runtime so this module stays
useful even when U4 has not landed yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ClassifiedFile",
    "FileKind",
    "classify_file",
]


class FileKind(StrEnum):
    """High-level file family used to pick the ingestion pipeline."""

    DICOM = "dicom"
    PDF = "pdf"
    IMAGE = "image"
    TEXT = "text"
    AUDIO = "audio"
    VIDEO = "video"
    ARCHIVE = "archive"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ClassifiedFile:
    """Result of classifying a buffer.

    ``confidence`` reflects which tier the decision came from:

    - ``1.0`` — magic bytes matched (strongest)
    - ``0.7`` — resolved via the caller-supplied content-type hint
    - ``0.5`` — resolved via filename extension only
    - ``0.0`` — unknown
    """

    kind: FileKind
    confidence: float
    mime_type: str | None
    suggested_document_type: str | None


# ---------------------------------------------------------------------------
# Magic-byte catalog
# ---------------------------------------------------------------------------
#
# Keep these as plain bytes constants so inspection is obvious. For the
# few formats whose signature is not a simple prefix (DICOM, WAV, MP4,
# NIfTI) we use a dedicated helper further down.


_PDF_MAGIC = b"%PDF"
_JPEG_MAGIC = b"\xff\xd8"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_TIFF_MAGICS = (b"II*\x00", b"MM\x00*")
_BMP_MAGIC = b"BM"
_WEBP_TAG = b"WEBP"
_ZIP_MAGIC = b"PK\x03\x04"
_GZIP_MAGIC = b"\x1f\x8b"
_TAR_MAGIC_OFFSET = 257  # "ustar" at offset 257 in POSIX tar
_TAR_MAGIC = b"ustar"
# ID3v2 tag prefix plus the three common MPEG-1/2 Layer III frame-sync
# patterns (bitrate/sampling-rate bits vary, but these cover the vast
# majority of files we see in the wild).
_MP3_MAGICS = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
_OGG_MAGIC = b"OggS"
_FLAC_MAGIC = b"fLaC"
_WAV_RIFF = b"RIFF"
_WAV_TAG = b"WAVE"
_MP4_FTYP = b"ftyp"
_MKV_MAGIC = b"\x1a\x45\xdf\xa3"  # EBML header, covers .mkv and .webm


# MIME-type hints we accept from the HTTP layer. We deliberately keep
# the list small and canonical; unknown subtypes fall back to the
# family-level match (``image/*``, ``audio/*``, ``video/*``, ``text/*``).
_MIME_TO_KIND: dict[str, FileKind] = {
    "application/dicom": FileKind.DICOM,
    "application/pdf": FileKind.PDF,
    "application/zip": FileKind.ARCHIVE,
    "application/x-tar": FileKind.ARCHIVE,
    "application/gzip": FileKind.ARCHIVE,
    "application/x-gzip": FileKind.ARCHIVE,
}


# Filename extensions — used only when nothing stronger is available.
_EXT_TO_KIND: dict[str, FileKind] = {
    # DICOM
    ".dcm": FileKind.DICOM,
    ".dicom": FileKind.DICOM,
    # PDF
    ".pdf": FileKind.PDF,
    # Images
    ".jpg": FileKind.IMAGE,
    ".jpeg": FileKind.IMAGE,
    ".png": FileKind.IMAGE,
    ".gif": FileKind.IMAGE,
    ".tif": FileKind.IMAGE,
    ".tiff": FileKind.IMAGE,
    ".bmp": FileKind.IMAGE,
    ".webp": FileKind.IMAGE,
    # Text
    ".txt": FileKind.TEXT,
    ".md": FileKind.TEXT,
    ".csv": FileKind.TEXT,
    ".json": FileKind.TEXT,
    ".xml": FileKind.TEXT,
    # Audio
    ".mp3": FileKind.AUDIO,
    ".wav": FileKind.AUDIO,
    ".flac": FileKind.AUDIO,
    ".m4a": FileKind.AUDIO,
    ".ogg": FileKind.AUDIO,
    # Video
    ".mp4": FileKind.VIDEO,
    ".mov": FileKind.VIDEO,
    ".avi": FileKind.VIDEO,
    ".mkv": FileKind.VIDEO,
    ".webm": FileKind.VIDEO,
    # Archives
    ".zip": FileKind.ARCHIVE,
    ".tar": FileKind.ARCHIVE,
    ".gz": FileKind.ARCHIVE,
}


# Canonical MIME string returned for each ``FileKind`` when we fall
# through to the extension tier and the extension alone is not enough
# to pick a specific subtype.
_KIND_TO_DEFAULT_MIME: dict[FileKind, str] = {
    FileKind.DICOM: "application/dicom",
    FileKind.PDF: "application/pdf",
    FileKind.IMAGE: "image/*",
    FileKind.TEXT: "text/plain",
    FileKind.AUDIO: "audio/*",
    FileKind.VIDEO: "video/*",
    FileKind.ARCHIVE: "application/zip",
}


def classify_file(
    content: bytes,
    filename: str | None = None,
    content_type_hint: str | None = None,
) -> ClassifiedFile:
    """Classify ``content`` by magic bytes, then content-type, then extension.

    The three tiers run in strict priority order so a lying client
    cannot flip the decision by setting an attractive-sounding
    ``Content-Type`` — the magic layer always wins. Only when magic
    gives up do the weaker signals get a vote.
    """
    # Tier 1: magic bytes — authoritative.
    kind, mime = _match_by_magic(content)
    if kind is not FileKind.UNKNOWN:
        return _build_result(kind, confidence=1.0, mime=mime, filename=filename)

    # Tier 2: HTTP content-type hint.
    kind, mime = _match_by_mime(content_type_hint)
    if kind is not FileKind.UNKNOWN:
        return _build_result(kind, confidence=0.7, mime=mime, filename=filename)

    # Tier 3: filename extension.
    kind = _match_by_extension(filename)
    if kind is not FileKind.UNKNOWN:
        return _build_result(
            kind,
            confidence=0.5,
            mime=_KIND_TO_DEFAULT_MIME.get(kind),
            filename=filename,
        )

    return ClassifiedFile(
        kind=FileKind.UNKNOWN,
        confidence=0.0,
        mime_type=None,
        suggested_document_type=None,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_result(
    kind: FileKind,
    *,
    confidence: float,
    mime: str | None,
    filename: str | None,
) -> ClassifiedFile:
    """Pack a ``ClassifiedFile`` and populate ``suggested_document_type``
    for the families that have subtype classification downstream."""
    suggested = None
    if kind in (FileKind.PDF, FileKind.TEXT, FileKind.IMAGE) and filename:
        suggested = _maybe_guess_document_type(filename)
    return ClassifiedFile(
        kind=kind,
        confidence=confidence,
        mime_type=mime,
        suggested_document_type=suggested,
    )


def _match_by_magic(content: bytes) -> tuple[FileKind, str | None]:
    """Check the byte signatures defined in the module-level catalog.

    Returns ``(kind, mime)``. ``mime`` is the most specific canonical
    MIME we can emit from the signature alone; callers that want finer
    subtype detection should plug in a dedicated sniffer.
    """
    if not content:
        return FileKind.UNKNOWN, None

    # DICOM: 128-byte preamble + "DICM" at offset 128. Buffers shorter
    # than 132 bytes cannot be DICOM, so skip the check entirely.
    if len(content) >= 132 and content[128:132] == b"DICM":
        return FileKind.DICOM, "application/dicom"

    # NIfTI: first 4 bytes interpreted as int32 (native endian agnostic
    # — try both) equal 348 (NIfTI-1) or 540 (NIfTI-2). We treat NIfTI
    # as IMAGE because downstream handles volumetric medical images via
    # the same pipeline as other raster formats.
    if len(content) >= 4 and _is_nifti_header(content[:4]):
        return FileKind.IMAGE, "application/x-nifti"

    if content.startswith(_PDF_MAGIC):
        return FileKind.PDF, "application/pdf"

    if content.startswith(_PNG_MAGIC):
        return FileKind.IMAGE, "image/png"

    if content.startswith(_JPEG_MAGIC):
        return FileKind.IMAGE, "image/jpeg"

    if content.startswith(_GIF_MAGICS):
        return FileKind.IMAGE, "image/gif"

    if content.startswith(_TIFF_MAGICS):
        return FileKind.IMAGE, "image/tiff"

    if content.startswith(_BMP_MAGIC) and len(content) >= 14:
        # BMP's "BM" is short and prone to false positives. Require at
        # least the 14-byte file header before committing.
        return FileKind.IMAGE, "image/bmp"

    # RIFF container hosts both WAV and WebP — disambiguate via the
    # 4-byte form tag at offset 8.
    if content.startswith(_WAV_RIFF) and len(content) >= 12:
        form = content[8:12]
        if form == _WAV_TAG:
            return FileKind.AUDIO, "audio/wav"
        if form == _WEBP_TAG:
            return FileKind.IMAGE, "image/webp"

    # MP4 family: the ``ftyp`` atom sits at bytes 4..8 for ISO BMFF.
    if len(content) >= 12 and content[4:8] == _MP4_FTYP:
        return FileKind.VIDEO, "video/mp4"

    # Matroska / WebM share the EBML magic; we report video/* since
    # we cannot tell them apart without the DocType element.
    if content.startswith(_MKV_MAGIC):
        return FileKind.VIDEO, "video/x-matroska"

    if content.startswith(_MP3_MAGICS):
        return FileKind.AUDIO, "audio/mpeg"

    if content.startswith(_OGG_MAGIC):
        return FileKind.AUDIO, "audio/ogg"

    if content.startswith(_FLAC_MAGIC):
        return FileKind.AUDIO, "audio/flac"

    if content.startswith(_ZIP_MAGIC):
        # Note: .docx / .xlsx / .pptx are ZIP archives with an OPC
        # manifest inside. We report them as ARCHIVE here; a dedicated
        # Office sniffer can be added later without breaking callers.
        return FileKind.ARCHIVE, "application/zip"

    if content.startswith(_GZIP_MAGIC):
        return FileKind.ARCHIVE, "application/gzip"

    if (
        len(content) >= _TAR_MAGIC_OFFSET + len(_TAR_MAGIC)
        and content[_TAR_MAGIC_OFFSET : _TAR_MAGIC_OFFSET + len(_TAR_MAGIC)] == _TAR_MAGIC
    ):
        return FileKind.ARCHIVE, "application/x-tar"

    return FileKind.UNKNOWN, None


def _is_nifti_header(prefix: bytes) -> bool:
    """NIfTI's ``sizeof_hdr`` field is 348 (v1) or 540 (v2) and lives in
    the first 4 bytes. Endianness isn't encoded — both little- and
    big-endian files are valid — so we try both and accept either."""
    if len(prefix) < 4:
        return False
    le = int.from_bytes(prefix, "little", signed=False)
    be = int.from_bytes(prefix, "big", signed=False)
    return le in (348, 540) or be in (348, 540)


def _match_by_mime(content_type_hint: str | None) -> tuple[FileKind, str | None]:
    """Resolve the HTTP content-type hint to a ``FileKind``.

    Accepts parametrised types (``text/plain; charset=utf-8``). Falls
    back to family-level matching for ``image/*``, ``audio/*``,
    ``video/*``, ``text/*`` so novel subtypes still classify sanely.
    """
    if not content_type_hint:
        return FileKind.UNKNOWN, None
    media_type = content_type_hint.split(";", 1)[0].strip().lower()
    if not media_type:
        return FileKind.UNKNOWN, None

    if media_type in _MIME_TO_KIND:
        return _MIME_TO_KIND[media_type], media_type

    family, _, _ = media_type.partition("/")
    if family == "image":
        return FileKind.IMAGE, media_type
    if family == "audio":
        return FileKind.AUDIO, media_type
    if family == "video":
        return FileKind.VIDEO, media_type
    if family == "text":
        return FileKind.TEXT, media_type

    return FileKind.UNKNOWN, None


def _match_by_extension(filename: str | None) -> FileKind:
    """Map the last dotted suffix of ``filename`` to a ``FileKind``.

    Matching is case-insensitive; anything not in the table returns
    ``UNKNOWN``.
    """
    if not filename:
        return FileKind.UNKNOWN
    # ``rpartition`` keeps us correct on names like ``archive.tar.gz``
    # by honouring the last suffix only (``.gz``).
    _, dot, ext = filename.rpartition(".")
    if not dot:
        return FileKind.UNKNOWN
    return _EXT_TO_KIND.get("." + ext.lower(), FileKind.UNKNOWN)


def _maybe_guess_document_type(filename: str) -> str | None:
    """Call Unit U4's ``guess_document_type`` if the module is importable.

    The import happens here (not at module top) so ``file_classifier``
    keeps working when U4 has not been delivered yet — classifier users
    just see ``suggested_document_type=None``.
    """
    try:
        from bvphoenix.services.document_type import (  # type: ignore[import-not-found]
            guess_document_type,
        )
    except Exception:
        return None
    try:
        return guess_document_type(filename)
    except Exception:
        return None
