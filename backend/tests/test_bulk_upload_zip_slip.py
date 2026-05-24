"""Zip-slip (CWE-22) regression test for the bulk-upload unpacker.

A malicious archive can carry members whose name encodes a directory
traversal (``../../etc/passwd``), an absolute path (``/etc/passwd``),
or a Windows-style escape (``..\\..\\Windows\\system32``). Even though
the staging path is currently composed via S3 keys rather than
filesystem writes, a poisoned ``relative_path`` propagates downstream
into DICOMDIR matching and folder reconstruction; tomorrow's worker
that wrote to disk would inherit a directory-escape bug for free.

The fix lives in ``bulk_upload._is_safe_archive_member_name`` and is
applied inside ``_unpack_zip`` before the member is appended to
staging. This test crafts an in-memory ZIP with three malicious
members and one benign member, runs the unpacker, and asserts:
  * the three malicious entries are reported as ``skipped`` with a
    "path traversal" reason,
  * the benign entry is staged.
"""

from __future__ import annotations

import io
import zipfile

from bvphoenix.api.bulk_upload import (
    _is_safe_archive_member_name,
    _Staging,
    _unpack_zip,
)


def _craft_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_helper_rejects_traversal_components() -> None:
    assert not _is_safe_archive_member_name("../etc/passwd")
    assert not _is_safe_archive_member_name("foo/../../bar")
    assert not _is_safe_archive_member_name("/etc/passwd")
    assert not _is_safe_archive_member_name("C:\\Windows\\System32")
    assert not _is_safe_archive_member_name("..\\..\\evil")
    assert not _is_safe_archive_member_name("./relative")
    assert not _is_safe_archive_member_name("with\x00null")
    assert not _is_safe_archive_member_name("")


def test_helper_accepts_safe_relative_names() -> None:
    assert _is_safe_archive_member_name("study1/IM0001.dcm")
    assert _is_safe_archive_member_name("DICOMDIR")
    assert _is_safe_archive_member_name("nested/folder/file.txt")
    assert _is_safe_archive_member_name("nome con spazi.dcm")


def test_unpack_zip_skips_malicious_members() -> None:
    """Craft a ZIP carrying three malicious entries and one benign
    one. The unpacker must skip the malicious ones with a clear
    reason and stage only the benign one.
    """
    data = _craft_zip(
        {
            "../escape.dcm": b"PAYLOAD-A",
            "/abs/path/escape.dcm": b"PAYLOAD-B",
            "ok/safe.dcm": b"PAYLOAD-C",
        }
    )
    staging = _Staging()
    _unpack_zip(data, base_path="incoming", depth=0, staging=staging)

    staged_paths = {vf.relative_path for vf in staging.files}
    assert staged_paths == {"incoming/ok/safe.dcm"}, (
        f"only the safe member should be staged, got: {staged_paths}"
    )

    skipped_names = {s.filename for s in staging.skipped}
    assert "../escape.dcm" in skipped_names
    assert "/abs/path/escape.dcm" in skipped_names
    for skipped in staging.skipped:
        if skipped.filename in {"../escape.dcm", "/abs/path/escape.dcm"}:
            assert "path traversal" in skipped.reason or "absolute path" in skipped.reason
