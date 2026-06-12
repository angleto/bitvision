"""Shared archive-safety guards (zip-slip, size caps, zip-bomb).

Extracted from ``api/bulk_upload.py`` so the same defenses protect every
path that accepts attacker-supplied archives: the bulk-upload unpackers
and the review-queue staging checks (``services/review_queue/plugins``).
The constants keep the values the bulk pipeline has always enforced;
callers that need different bounds (e.g. a public-contribution profile
with tighter caps) pass explicit arguments instead of redefining them.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

# Keep per-file payloads bounded so a runaway upload can't eat the whole
# worker's memory — matches the cap advertised in the task spec (500 MB).
MAX_FILE_BYTES = 500 * 1024 * 1024

# Hospital DVDs frequently land at 1–4 GiB; lift the per-file cap for
# ``.iso`` images so the server-side fallback (``services.iso_extractor``)
# can take a shot at parsing them when the client-side ISO 9660 reader
# in the browser fails. The cap stays bounded so a malicious upload
# can't eat the whole worker's disk.
MAX_ISO_BYTES = 5 * 1024 * 1024 * 1024

# Cap the recursion depth on nested ZIP archives. Two levels is enough
# for the realistic "archive of studies" use case without opening a
# zip-bomb amplification vector.
MAX_ZIP_DEPTH = 2

# Zip-bomb heuristics for ``scan_zip_safety``. A legitimate clinical
# archive (DICOM, PDF scans) rarely compresses better than ~50:1; the
# canonical 42.zip-style bombs sit at 10^3..10^6:1. The ratio alone
# would flag tiny highly-compressible files (an empty text file), so it
# only trips when the declared uncompressed payload is also large.
ZIP_BOMB_RATIO = 200
ZIP_BOMB_MIN_UNCOMPRESSED = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_TOTAL_UNCOMPRESSED = 8 * 1024 * 1024 * 1024


def is_safe_archive_member_name(name: str) -> bool:
    """Return True iff ``name`` is safe to use as a relative path below a
    staging root. Defends against zip-slip (CWE-22): a malicious archive
    can carry members like ``../../etc/passwd`` or ``/etc/passwd``; while
    we currently store members as opaque bytes under ``_ingest_jobs/<id>/``
    (so the OS filesystem never sees them), the worker eventually composes
    full paths via ``relative_path`` to feed DICOMDIR matching and S3
    keys. A traversal-laced filename can poison downstream key derivation
    or, worse, escape into a future writer that does touch the FS.
    Reject:
      * empty names
      * absolute paths (POSIX ``/`` or Windows ``C:`` drive letter)
      * any path component equal to ``..`` or ``.``
      * NUL bytes (filesystem terminator surprise)
      * backslashes treated separately from ``/`` (Windows-style escapes)
    """
    if not name:
        return False
    if "\x00" in name:
        return False
    normalised = name.replace("\\", "/")
    if normalised.startswith("/"):
        return False
    if len(normalised) >= 2 and normalised[1] == ":":
        return False
    return all(part not in ("..", ".") for part in normalised.split("/"))


@dataclass(frozen=True, slots=True)
class ZipSafetyReport:
    """Outcome of a metadata-only ZIP inspection (no member is extracted).

    ``hostile`` collects findings that mark the archive as actively
    malicious (traversal members, bomb-grade compression); ``suspicious``
    collects findings that need a human eye but are not proof of attack
    (encrypted members we cannot scan, too many members). Both empty ⇒
    the archive is safe to stage.
    """

    member_count: int = 0
    total_uncompressed: int = 0
    total_compressed: int = 0
    hostile: tuple[str, ...] = field(default=())
    suspicious: tuple[str, ...] = field(default=())

    @property
    def is_hostile(self) -> bool:
        return bool(self.hostile)

    @property
    def is_clean(self) -> bool:
        return not self.hostile and not self.suspicious


def scan_zip_safety(
    blob: bytes,
    *,
    max_member_bytes: int = MAX_FILE_BYTES,
    max_members: int = MAX_ZIP_MEMBERS,
    max_total_uncompressed: int = MAX_ZIP_TOTAL_UNCOMPRESSED,
    bomb_ratio: int = ZIP_BOMB_RATIO,
    bomb_min_uncompressed: int = ZIP_BOMB_MIN_UNCOMPRESSED,
) -> ZipSafetyReport:
    """Inspect a ZIP from its central directory only — nothing is
    decompressed, so the scan itself cannot be the amplification vector.

    Single-pass over ``infolist``; findings are human-readable strings so
    the review queue can surface them verbatim in ``auto_checks``.
    """
    hostile: list[str] = []
    suspicious: list[str] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        return ZipSafetyReport(hostile=(f"unreadable zip: {exc}",))

    total_unc = 0
    total_cmp = 0
    for info in infos:
        if not is_safe_archive_member_name(info.filename):
            hostile.append(f"unsafe member path: {info.filename!r}")
        if info.flag_bits & 0x1:
            suspicious.append(f"encrypted member (cannot be scanned): {info.filename!r}")
        if info.file_size > max_member_bytes:
            hostile.append(
                f"member {info.filename!r} declares {info.file_size} bytes (cap {max_member_bytes})"
            )
        # Per-member bomb heuristic: declared size huge vs stored bytes.
        if (
            info.file_size >= bomb_min_uncompressed
            and info.compress_size > 0
            and info.file_size // info.compress_size >= bomb_ratio
        ):
            hostile.append(
                f"bomb-grade compression on {info.filename!r} "
                f"({info.file_size}:{info.compress_size})"
            )
        total_unc += info.file_size
        total_cmp += info.compress_size

    if len(infos) > max_members:
        suspicious.append(f"{len(infos)} members (cap {max_members})")
    if total_unc > max_total_uncompressed:
        hostile.append(
            f"declared uncompressed total {total_unc} bytes (cap {max_total_uncompressed})"
        )
    # Whole-archive bomb heuristic — catches bombs spread across many
    # small members where no single member trips the per-member check.
    if (
        total_unc >= bomb_min_uncompressed
        and total_cmp > 0
        and total_unc // total_cmp >= bomb_ratio
    ):
        hostile.append(f"bomb-grade overall compression ({total_unc}:{total_cmp})")

    return ZipSafetyReport(
        member_count=len(infos),
        total_uncompressed=total_unc,
        total_compressed=total_cmp,
        hostile=tuple(hostile),
        suspicious=tuple(suspicious),
    )


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_ISO_BYTES",
    "MAX_ZIP_DEPTH",
    "MAX_ZIP_MEMBERS",
    "MAX_ZIP_TOTAL_UNCOMPRESSED",
    "ZIP_BOMB_MIN_UNCOMPRESSED",
    "ZIP_BOMB_RATIO",
    "ZipSafetyReport",
    "is_safe_archive_member_name",
    "scan_zip_safety",
]
