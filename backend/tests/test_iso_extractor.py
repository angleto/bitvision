"""Smoke tests for the server-side ISO 9660 / UDF extractor.

The pure-UDF fallback path requires a real UDF DVD image to fully
exercise. We don't ship one in-tree (clinical DVDs are sensitive
data even when synthetic), so the UDF tests here run against the
listing-parser and the binary-discovery surface; the end-to-end UDF
path is left to a manual QA fixture.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest

pycdlib = pytest.importorskip("pycdlib")

from bvphoenix.services.iso_extractor import (
    _find_seven_zip,
    _parse_seven_zip_listing,
    _pycdlib_can_walk,
    extract_iso,
    iso_kind,
)


def _build_iso_with_files(files: list[tuple[str, bytes]]) -> str:
    """Build a minimal Joliet-bearing ISO containing ``files``.

    ``files`` is a list of ``(iso_path, data)`` pairs. ``iso_path`` must
    start with ``/`` and is interpreted as the ISO-9660 short name; the
    Joliet long name is the same string so callers can predict the
    extracted basenames easily. Returns the path to a temp ISO file
    the caller is responsible for unlinking.
    """
    out = pycdlib.PyCdlib()
    out.new(joliet=3)
    for iso_path, data in files:
        out.add_fp(
            io.BytesIO(data),
            len(data),
            iso_path=iso_path,
            joliet_path=iso_path,
        )
    fd, path = tempfile.mkstemp(suffix=".iso")
    os.close(fd)
    out.write(path)
    out.close()
    return path


class TestExtractIso:
    def test_extracts_files_with_long_names(self) -> None:
        path = _build_iso_with_files(
            [
                ("/REFERTO.PDF;1", b"%PDF-1.4\n%fake\n"),
                ("/IMG_0001.DCM;1", b"\x00" * 128 + b"DICM"),
            ]
        )
        try:
            entries = list(extract_iso(path))
            names = sorted(rel for rel, _ in entries)
            assert names == ["IMG_0001.DCM", "REFERTO.PDF"]
            payloads = dict(entries)
            assert payloads["REFERTO.PDF"].startswith(b"%PDF")
            assert payloads["IMG_0001.DCM"].startswith(b"\x00" * 16)
            assert payloads["IMG_0001.DCM"][128:132] == b"DICM"
        finally:
            os.unlink(path)

    def test_skips_files_above_max_size(self) -> None:
        path = _build_iso_with_files(
            [
                ("/SMALL.TXT;1", b"hello"),
                ("/BIG.BIN;1", b"x" * 1024),
            ]
        )
        try:
            small_only = list(extract_iso(path, max_file_bytes=512))
            kept = sorted(rel for rel, _ in small_only)
            assert kept == ["SMALL.TXT"]
        finally:
            os.unlink(path)

    def test_iso_kind_detects_joliet(self) -> None:
        path = _build_iso_with_files([("/A.TXT;1", b"a")])
        try:
            iso = pycdlib.PyCdlib()
            iso.open(path)
            try:
                # Joliet beats plain ISO-9660 in the kind ranking — the
                # extractor will use the Joliet facade for filenames.
                assert iso_kind(iso) == "joliet"
            finally:
                iso.close()
        finally:
            os.unlink(path)

    def test_pycdlib_can_walk_true_for_real_iso(self) -> None:
        path = _build_iso_with_files([("/A.TXT;1", b"a")])
        try:
            assert _pycdlib_can_walk(path) is True
        finally:
            os.unlink(path)

    def test_pycdlib_can_walk_false_for_non_iso(self, tmp_path: Path) -> None:
        # Random bytes — pycdlib refuses to open. The probe must
        # return False so the extractor falls back to 7z (or raises
        # if 7z is also unavailable).
        bogus = tmp_path / "not-an-iso.iso"
        bogus.write_bytes(b"NOT_AN_ISO_AT_ALL" * 1024)
        assert _pycdlib_can_walk(str(bogus)) is False

    def test_extract_iso_raises_when_unreadable_and_no_seven_zip(self, tmp_path: Path) -> None:
        bogus = tmp_path / "junk.iso"
        bogus.write_bytes(b"definitely not iso 9660")
        with mock.patch("bvphoenix.services.iso_extractor._find_seven_zip", return_value=None):
            with pytest.raises(RuntimeError, match="ISO is unreadable"):
                list(extract_iso(str(bogus)))

    def test_extract_iso_raises_oserror_for_missing_path(self) -> None:
        with pytest.raises(OSError, match="ISO file not found"):
            list(extract_iso("/nonexistent/path/foo.iso"))


class TestSevenZipListingParser:
    def test_parses_files_skipping_directories(self) -> None:
        # Excerpt of ``7z l -slt`` output for an ISO with one
        # directory and two files inside it. Header lines (--, -- )
        # and per-file blocks separated by blank lines.
        sample = (
            "\n"
            "----------\n"
            "Path = /\n"
            "Size = 0\n"
            "Folder = +\n"
            "Attributes = D\n"
            "\n"
            "Path = /DICOMDIR\n"
            "Size = 4096\n"
            "Folder = -\n"
            "Attributes = A\n"
            "\n"
            "Path = /STUDY/IMG_0001.DCM\n"
            "Size = 1048576\n"
            "Folder = -\n"
            "Attributes = A\n"
            "\n"
        )
        out = _parse_seven_zip_listing(sample)
        assert out == [
            ("/DICOMDIR", 4096),
            ("/STUDY/IMG_0001.DCM", 1048576),
        ]

    def test_parses_record_without_trailing_blank(self) -> None:
        sample = "Path = /A.TXT\nSize = 5\nFolder = -\nAttributes = A\n"
        out = _parse_seven_zip_listing(sample)
        assert out == [("/A.TXT", 5)]

    def test_skips_records_with_unparseable_size(self) -> None:
        sample = (
            "Path = /BAD.TXT\n"
            "Size = not-a-number\n"
            "Folder = -\n"
            "Attributes = A\n"
            "\n"
            "Path = /GOOD.TXT\n"
            "Size = 7\n"
            "Folder = -\n"
            "Attributes = A\n"
            "\n"
        )
        out = _parse_seven_zip_listing(sample)
        assert out == [("/GOOD.TXT", 7)]

    def test_ignores_header_lines_without_kv(self) -> None:
        sample = (
            "7-Zip 17.04 (x64) : Copyright (c) 1999-2021 Igor Pavlov\n"
            "Listing archive: /tmp/foo.iso\n"
            "\n"
            "--\n"
            "Path = /tmp/foo.iso\n"
            "Type = Iso\n"
            "\n"
            "Path = /A.TXT\n"
            "Size = 1\n"
            "Folder = -\n"
            "Attributes = A\n"
            "\n"
        )
        out = _parse_seven_zip_listing(sample)
        # Archive-level record has no Size; only the actual file is
        # emitted.
        assert out == [("/A.TXT", 1)]


class TestFindSevenZip:
    def test_returns_none_when_no_binary_in_path(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            assert _find_seven_zip() is None

    def test_returns_first_resolved_binary(self) -> None:
        def fake_which(name: str) -> str | None:
            return "/opt/homebrew/bin/7zz" if name == "7zz" else None

        with mock.patch("shutil.which", side_effect=fake_which):
            assert _find_seven_zip() == "/opt/homebrew/bin/7zz"


@pytest.mark.skipif(
    shutil.which("7z") is None and shutil.which("7zz") is None and shutil.which("7za") is None,
    reason="no 7z-compatible binary on PATH",
)
class TestSevenZipEndToEnd:
    """Round-trip test: pycdlib-built ISO read via the 7z fallback path.

    This guards against regressions in the listing parser /
    extraction wiring. A real UDF-only fixture is out of scope (would
    require ``mkudffs`` and a sensitive DVD layout).
    """

    def test_seven_zip_fallback_reads_pycdlib_iso(self) -> None:
        from bvphoenix.services import iso_extractor

        # Flat-path fixture: ``_build_iso_with_files`` does not add
        # parent directories, and pycdlib refuses to add a file under
        # a directory that hasn't been declared first. Two top-level
        # files are sufficient to exercise the listing-parser +
        # streaming-extraction wiring of the 7z fallback.
        path = _build_iso_with_files(
            [
                ("/HELLO.TXT;1", b"hello"),
                ("/WORLD.BIN;1", b"\x01\x02\x03"),
            ]
        )
        try:
            # Force the fallback even though pycdlib can read the ISO.
            with mock.patch.object(iso_extractor, "_pycdlib_can_walk", return_value=False):
                entries = sorted((rel, data) for rel, data in extract_iso(path))
            names = [rel for rel, _ in entries]
            # Both files came through; exact path casing depends on
            # the 7z facade pick (typically Joliet long names).
            assert any("HELLO" in n.upper() for n in names)
            assert any("WORLD" in n.upper() for n in names)
        finally:
            os.unlink(path)
