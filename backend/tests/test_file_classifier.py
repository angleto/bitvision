"""Unit tests for the universal file classifier.

We exercise every magic-byte branch with a deliberately tiny 32-byte
blob (padded to the signature's minimum length when needed) so the
tests stay fast and the intent is obvious. Tier priorities (magic >
content-type > extension) are covered by the conflict tests at the
bottom of the file.
"""

from __future__ import annotations

import pytest

from bvphoenix.services.file_classifier import (
    ClassifiedFile,
    FileKind,
    classify_file,
)

# A filler byte that has no meaning for any format we support — safe
# padding to reach the minimum buffer length a given magic needs.
_PAD = b"\x00"


def _pad(sig: bytes, size: int = 32) -> bytes:
    """Right-pad ``sig`` with NULs so the resulting blob is at least
    ``size`` bytes. Makes buffers long enough to pass length checks
    without affecting the signature."""
    if len(sig) >= size:
        return sig
    return sig + _PAD * (size - len(sig))


# ---------------------------------------------------------------------------
# Magic-byte detection
# ---------------------------------------------------------------------------


def test_dicom_magic_at_offset_128() -> None:
    # 128-byte preamble of NULs + "DICM" + padding — the canonical
    # DICOM file layout. Confidence must be 1.0 (magic tier).
    blob = (_PAD * 128) + b"DICM" + (_PAD * 32)
    result = classify_file(blob)
    assert result.kind is FileKind.DICOM
    assert result.confidence == 1.0
    assert result.mime_type == "application/dicom"


def test_pdf_magic() -> None:
    result = classify_file(_pad(b"%PDF-1.7"))
    assert result.kind is FileKind.PDF
    assert result.mime_type == "application/pdf"
    assert result.confidence == 1.0


def test_jpeg_magic() -> None:
    result = classify_file(_pad(b"\xff\xd8\xff\xe0"))
    assert result.kind is FileKind.IMAGE
    assert result.mime_type == "image/jpeg"


def test_png_magic() -> None:
    result = classify_file(_pad(b"\x89PNG\r\n\x1a\n"))
    assert result.kind is FileKind.IMAGE
    assert result.mime_type == "image/png"


def test_gif_87a_magic() -> None:
    result = classify_file(_pad(b"GIF87a"))
    assert result.kind is FileKind.IMAGE
    assert result.mime_type == "image/gif"


def test_gif_89a_magic() -> None:
    result = classify_file(_pad(b"GIF89a"))
    assert result.kind is FileKind.IMAGE
    assert result.mime_type == "image/gif"


def test_tiff_little_endian_magic() -> None:
    result = classify_file(_pad(b"II*\x00"))
    assert result.kind is FileKind.IMAGE
    assert result.mime_type == "image/tiff"


def test_tiff_big_endian_magic() -> None:
    result = classify_file(_pad(b"MM\x00*"))
    assert result.kind is FileKind.IMAGE
    assert result.mime_type == "image/tiff"


def test_bmp_magic() -> None:
    # BMP signature is short ("BM"); we require the 14-byte file header
    # before committing, so pad accordingly.
    result = classify_file(_pad(b"BM" + b"\x00" * 12))
    assert result.kind is FileKind.IMAGE
    assert result.mime_type == "image/bmp"


def test_webp_magic() -> None:
    # RIFF container with WEBP form tag at offset 8.
    blob = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
    result = classify_file(blob)
    assert result.kind is FileKind.IMAGE
    assert result.mime_type == "image/webp"


def test_nifti_v1_header() -> None:
    # NIfTI-1 header has sizeof_hdr==348 in the first 4 bytes.
    blob = (348).to_bytes(4, "little") + b"\x00" * 28
    result = classify_file(blob)
    assert result.kind is FileKind.IMAGE
    assert result.mime_type == "application/x-nifti"


def test_nifti_v2_header() -> None:
    blob = (540).to_bytes(4, "little") + b"\x00" * 28
    result = classify_file(blob)
    assert result.kind is FileKind.IMAGE


def test_zip_magic() -> None:
    result = classify_file(_pad(b"PK\x03\x04"))
    assert result.kind is FileKind.ARCHIVE
    assert result.mime_type == "application/zip"


def test_gzip_magic() -> None:
    result = classify_file(_pad(b"\x1f\x8b\x08\x00"))
    assert result.kind is FileKind.ARCHIVE
    assert result.mime_type == "application/gzip"


def test_mp3_id3_magic() -> None:
    result = classify_file(_pad(b"ID3\x03\x00"))
    assert result.kind is FileKind.AUDIO
    assert result.mime_type == "audio/mpeg"


def test_mp3_frame_magic() -> None:
    result = classify_file(_pad(b"\xff\xfb"))
    assert result.kind is FileKind.AUDIO
    assert result.mime_type == "audio/mpeg"


def test_ogg_magic() -> None:
    result = classify_file(_pad(b"OggS"))
    assert result.kind is FileKind.AUDIO
    assert result.mime_type == "audio/ogg"


def test_flac_magic() -> None:
    result = classify_file(_pad(b"fLaC"))
    assert result.kind is FileKind.AUDIO
    assert result.mime_type == "audio/flac"


def test_wav_magic() -> None:
    # RIFF container with WAVE form tag.
    blob = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20
    result = classify_file(blob)
    assert result.kind is FileKind.AUDIO
    assert result.mime_type == "audio/wav"


def test_mp4_ftyp_magic() -> None:
    # The "ftyp" atom sits at bytes 4..8 in ISO BMFF files.
    blob = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 20
    result = classify_file(blob)
    assert result.kind is FileKind.VIDEO
    assert result.mime_type == "video/mp4"


def test_matroska_magic() -> None:
    result = classify_file(_pad(b"\x1a\x45\xdf\xa3"))
    assert result.kind is FileKind.VIDEO


# ---------------------------------------------------------------------------
# Extension fallback — buffer is random bytes so only extension can decide.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("scan.dcm", FileKind.DICOM),
        ("scan.dicom", FileKind.DICOM),
        ("report.pdf", FileKind.PDF),
        ("photo.jpg", FileKind.IMAGE),
        ("photo.JPEG", FileKind.IMAGE),  # case-insensitive
        ("diagram.png", FileKind.IMAGE),
        ("loop.gif", FileKind.IMAGE),
        ("scan.tiff", FileKind.IMAGE),
        ("icon.bmp", FileKind.IMAGE),
        ("hero.webp", FileKind.IMAGE),
        ("notes.txt", FileKind.TEXT),
        ("README.md", FileKind.TEXT),
        ("data.csv", FileKind.TEXT),
        ("config.json", FileKind.TEXT),
        ("feed.xml", FileKind.TEXT),
        ("song.mp3", FileKind.AUDIO),
        ("clip.wav", FileKind.AUDIO),
        ("master.flac", FileKind.AUDIO),
        ("voicenote.m4a", FileKind.AUDIO),
        ("podcast.ogg", FileKind.AUDIO),
        ("movie.mp4", FileKind.VIDEO),
        ("movie.mov", FileKind.VIDEO),
        ("movie.avi", FileKind.VIDEO),
        ("movie.mkv", FileKind.VIDEO),
        ("movie.webm", FileKind.VIDEO),
        ("bundle.zip", FileKind.ARCHIVE),
        ("bundle.tar", FileKind.ARCHIVE),
        ("bundle.gz", FileKind.ARCHIVE),
    ],
)
def test_extension_fallback(filename: str, expected: FileKind) -> None:
    # 32 bytes of random-looking content that match no signature.
    blob = bytes(range(32))
    result = classify_file(blob, filename=filename)
    assert result.kind is expected
    assert result.confidence == 0.5


def test_unknown_extension_returns_unknown() -> None:
    result = classify_file(bytes(range(32)), filename="mystery.xyz")
    assert result.kind is FileKind.UNKNOWN
    assert result.confidence == 0.0
    assert result.mime_type is None


def test_no_filename_no_magic_is_unknown() -> None:
    result = classify_file(b"\x00" * 16)
    assert result.kind is FileKind.UNKNOWN


# ---------------------------------------------------------------------------
# MIME-type hint tier
# ---------------------------------------------------------------------------


def test_mime_hint_resolves_when_magic_inconclusive() -> None:
    # Bytes look like nothing; only the MIME hint says "it's text".
    result = classify_file(b"\x00" * 16, content_type_hint="text/plain")
    assert result.kind is FileKind.TEXT
    assert result.confidence == 0.7


def test_mime_hint_with_parameters() -> None:
    result = classify_file(b"\x00" * 16, content_type_hint="text/plain; charset=utf-8")
    assert result.kind is FileKind.TEXT


def test_mime_family_fallback_for_audio() -> None:
    # Exact subtype unknown but "audio/*" is enough to classify.
    result = classify_file(b"\x00" * 16, content_type_hint="audio/weird-codec")
    assert result.kind is FileKind.AUDIO


# ---------------------------------------------------------------------------
# Priority: magic > content_type > extension
# ---------------------------------------------------------------------------


def test_magic_beats_lying_content_type() -> None:
    # Client claims "text/plain" but the bytes are obviously a PDF.
    # Magic wins; the client cannot force a misclassification.
    blob = _pad(b"%PDF-1.7")
    result = classify_file(blob, filename="evil.txt", content_type_hint="text/plain")
    assert result.kind is FileKind.PDF
    assert result.confidence == 1.0


def test_content_type_beats_extension() -> None:
    # No magic match. MIME hint says audio, extension says text — MIME wins.
    result = classify_file(b"\x00" * 16, filename="hello.txt", content_type_hint="audio/mpeg")
    assert result.kind is FileKind.AUDIO
    assert result.confidence == 0.7


def test_extension_used_only_when_other_signals_silent() -> None:
    result = classify_file(b"\x00" * 16, filename="hello.txt")
    assert result.kind is FileKind.TEXT
    assert result.confidence == 0.5


# ---------------------------------------------------------------------------
# suggested_document_type hook (U4 is optional)
# ---------------------------------------------------------------------------


def test_suggested_document_type_none_when_u4_missing() -> None:
    # U4's guess_document_type has not landed; the import should fail
    # silently and suggested_document_type should be None.
    blob = _pad(b"%PDF-1.7")
    result = classify_file(blob, filename="report.pdf")
    assert result.kind is FileKind.PDF
    assert result.suggested_document_type is None


def test_suggested_document_type_called_for_pdf_text_image(monkeypatch) -> None:
    # Install a stub ``document_type`` module and verify classify_file
    # reaches for it for PDF/TEXT/IMAGE kinds.
    import sys
    import types

    calls: list[str] = []

    def fake_guess(filename: str) -> str:
        calls.append(filename)
        return "referto_rx"

    module = types.ModuleType("bvphoenix.services.document_type")
    module.guess_document_type = fake_guess  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bvphoenix.services.document_type", module)

    for blob, name, expected_kind in [
        (_pad(b"%PDF-1.7"), "report.pdf", FileKind.PDF),
        (_pad(b"\x89PNG\r\n\x1a\n"), "xray.png", FileKind.IMAGE),
        (b"\x00" * 16, "notes.txt", FileKind.TEXT),
    ]:
        result = classify_file(blob, filename=name)
        assert result.kind is expected_kind
        assert result.suggested_document_type == "referto_rx"

    assert calls == ["report.pdf", "xray.png", "notes.txt"]


def test_suggested_document_type_not_called_for_dicom(monkeypatch) -> None:
    # DICOM goes through its own pipeline; we should not invoke U4 for it
    # even if the module is present.
    import sys
    import types

    called = False

    def fake_guess(filename: str) -> str:
        nonlocal called
        called = True
        return "x"

    module = types.ModuleType("bvphoenix.services.document_type")
    module.guess_document_type = fake_guess  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bvphoenix.services.document_type", module)

    blob = (_PAD * 128) + b"DICM" + (_PAD * 32)
    result = classify_file(blob, filename="study.dcm")
    assert result.kind is FileKind.DICOM
    assert result.suggested_document_type is None
    assert called is False


# ---------------------------------------------------------------------------
# Dataclass shape sanity check
# ---------------------------------------------------------------------------


def test_result_is_classified_file() -> None:
    # Smoke test: make sure the public dataclass stays a dataclass and
    # the enum members are string-valued (so they serialise cleanly).
    result = classify_file(_pad(b"%PDF"))
    assert isinstance(result, ClassifiedFile)
    assert result.kind.value == "pdf"
