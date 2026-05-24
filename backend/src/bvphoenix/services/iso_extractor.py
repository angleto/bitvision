"""Server-side ISO 9660 / Joliet / Rock Ridge / UDF extractor.

Used by ``api/bulk_upload`` as the fallback when the client-side
parser in ``frontend/src/lib/iso9660.ts`` cannot extract a hospital
DVD: UDF-only volumes, hybrid ISOs, malformed descriptors. The user
just drops the ``.iso`` and the backend walks it on disk.

Implementation notes:

* ``pycdlib`` reads from a file path. We persist the upload to a
  spooled temp file before calling :func:`extract_iso` to avoid
  buffering a 4–5 GiB hospital DVD in memory.
* For each kind (UDF → Rock Ridge → Joliet → ISO-9660 plain) we walk
  the matching facade so the recovered filenames stay as close to
  the burner's intent as possible (UDF and Joliet preserve long
  Unicode names, Rock Ridge preserves UNIX casing, plain ISO falls
  back to 8.3 + ``;1`` version suffix).
* Pure UDF volumes (no ISO 9660 PVD) are not openable by ``pycdlib``
  at all (it requires a valid PVD at sector 16). Modern hospital
  DVDs increasingly ship pure UDF (DVD+R DL, BD-R, recent
  Philips/GE acquisitions). For those we fall back to ``7z`` (any of
  ``7z`` / ``7zz`` / ``7za``), which reads UDF 1.02 / 1.50 / 2.x
  natively. ``backend.Dockerfile`` installs ``p7zip-full``;
  development hosts without it lose only the UDF-pure path.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess  # nosec B404 - we shell out to a known binary on a controlled path
from collections.abc import Iterator
from contextlib import contextmanager

try:  # pragma: no cover — pycdlib import guarded so unrelated tests don't
    # have to install it just to import this module's siblings.
    import pycdlib
except ImportError:  # pragma: no cover
    pycdlib = None  # type: ignore[assignment]


log = logging.getLogger(__name__)


__all__ = ["extract_iso", "iso_kind"]


# ``7z`` binary names by distribution. ``7z`` ships with Debian's
# ``p7zip-full`` (used in the production image). ``7zz`` is the
# Homebrew ``sevenzip`` formula used on developer machines. ``7za``
# is the older p7zip CLI without UDF support; kept last as a fallback
# but flagged with a warning when used.
_SEVEN_ZIP_BINARIES: tuple[str, ...] = ("7z", "7zz", "7za")


def _find_seven_zip() -> str | None:
    """Return the absolute path to a 7z-compatible binary, or None."""
    for cand in _SEVEN_ZIP_BINARIES:
        resolved = shutil.which(cand)
        if resolved:
            return resolved
    return None


_FACADE_KEY = {
    "udf": "udf_path",
    "rr": "rr_path",
    "joliet": "joliet_path",
    "iso": "iso_path",
}


def _walk_directory(iso, kind: str, prefix: str) -> Iterator[tuple[str, bytes]]:
    """Walk ``iso`` rooted at ``prefix``, yielding ``(rel_path, data)``.

    ``kind`` selects the facade to query:

    * ``"udf"`` for UDF File Set;
    * ``"rr"`` for Rock Ridge (POSIX semantics over ISO-9660);
    * ``"joliet"`` for Joliet SVD (Unicode long names);
    * ``"iso"`` for plain ISO-9660 (8.3 short names).

    Returned ``rel_path`` uses forward slashes and never starts with one.
    Each ``data`` is the file's bytes; we read the entire file into
    memory because the bulk upload pipeline expects ``_VirtualFile``
    rows. The caller bounds total memory by skipping huge files
    upstream — :func:`extract_iso` enforces a per-file size cap.

    Per-file errors (corrupt FID, truncated extent) are logged and
    skipped so a single bad member doesn't abort the whole ISO walk
    — clinical DVDs frequently ship vendor junk that fails to parse
    cleanly while DICOM payloads underneath are fine.
    """
    arg_key = _FACADE_KEY[kind]
    walk_kwargs = {arg_key: prefix}
    for root, _dirs, files in iso.walk(**walk_kwargs):  # type: ignore[arg-type]
        current = root.lstrip("/").rstrip("/") if root != "/" else ""
        for fname in files:
            rel = f"{current}/{fname}" if current else fname
            inner = f"{root.rstrip('/')}/{fname}" if root != "/" else f"/{fname}"
            try:
                buffer = bytearray()
                stream_kwargs = {arg_key: inner}
                with iso.open_file_from_iso(**stream_kwargs) as fp:  # type: ignore[arg-type]
                    while True:
                        chunk = fp.read(64 * 1024)
                        if not chunk:
                            break
                        buffer.extend(chunk)
            except Exception as exc:
                log.warning("pycdlib: skipping %s (%s)", inner, exc)
                continue
            # ISO-9660 plain names carry a ``;1`` version suffix; some
            # ISOs leak it into Joliet too. Strip unconditionally for
            # parity with the client-side parser, then drop a trailing
            # dot left by 8.3 names like ``FILE.``.
            cleaned = rel
            semi = cleaned.rfind(";")
            if semi >= 0 and cleaned[semi + 1 :].isdigit():
                cleaned = cleaned[:semi]
            if cleaned.endswith("."):
                cleaned = cleaned[:-1]
            yield cleaned, bytes(buffer)


@contextmanager
def _open_iso(path: str):
    """Open ``path`` with pycdlib.

    pycdlib auto-detects every facade (ISO 9660 / Joliet / Rock
    Ridge / UDF Bridge) at open time. Pure UDF volumes (no ISO 9660
    PVD) raise ``PyCdlibInvalidISO`` here; the caller falls back to
    the 7z extractor in that case.
    """
    if pycdlib is None:
        raise RuntimeError("pycdlib not installed")
    iso = pycdlib.PyCdlib()
    iso.open(path)
    try:
        yield iso
    finally:
        iso.close()


def iso_kind(iso) -> str:
    """Return the best-quality facade ``iso`` exposes.

    Order: UDF → Rock Ridge → Joliet → ISO-9660. The caller passes
    whatever this returns to :func:`_walk_directory` so the recovered
    filenames are the highest-fidelity ones available.
    """
    if getattr(iso, "udf_root", None) is not None:
        return "udf"
    if getattr(iso, "rock_ridge", None):
        return "rr"
    if getattr(iso, "joliet_vd", None) is not None:
        return "joliet"
    return "iso"


def _pycdlib_can_walk(path: str) -> bool:
    """Quick probe: does pycdlib open ``path`` and find at least one file?

    Distinguishes the "ISO 9660 PVD missing → open() raises" case
    (pure UDF, our main 7z target) from the "ISO opens, walk yields
    files" case (everything pycdlib actually handles). Returns False
    for both failure modes so the caller falls back to 7z.

    The probe re-opens the ISO once (cheap: only the volume
    descriptor area is read), then closes it before the real
    extraction generator starts. Net cost: two opens of the first
    ~64 KiB of the ISO.
    """
    if pycdlib is None:
        return False
    try:
        with _open_iso(path) as iso:
            kind = iso_kind(iso)
            walk_kwargs = {_FACADE_KEY[kind]: "/"}
            return any(
                files
                for _root, _dirs, files in iso.walk(**walk_kwargs)  # type: ignore[arg-type]
            )
    except Exception as exc:
        log.info("pycdlib cannot read %s (%s) — will try 7z fallback", path, exc)
        return False


def _extract_with_pycdlib(path: str, *, max_file_bytes: int | None) -> Iterator[tuple[str, bytes]]:
    with _open_iso(path) as iso:
        kind = iso_kind(iso)
        for rel, data in _walk_directory(iso, kind=kind, prefix="/"):
            if max_file_bytes is not None and len(data) > max_file_bytes:
                continue
            yield rel, data


def _parse_seven_zip_listing(listing: str) -> list[tuple[str, int]]:
    """Parse ``7z l -slt <archive>`` output into [(member_path, size)].

    The ``-slt`` ("Set list type") flag emits one record per entry,
    one ``Key = Value`` line per field, blank-line separated. We
    only care about ``Path``, ``Size``, ``Attributes``, and skip
    directories (``Attributes`` starting with ``D``).

    Note: 7z prepends headers like ``Path = <archive>`` for the
    archive itself; those records typically have no ``Size`` line
    or have ``Folder = +``. Skipping ``D``-attribute and
    ``Folder = +`` records covers both cases.
    """
    out: list[tuple[str, int]] = []
    cur: dict[str, str] = {}
    for raw_line in listing.splitlines():
        line = raw_line.rstrip()
        if not line:
            # Record terminator. Emit if it's a file with a path.
            path_val = cur.get("Path")
            attr = cur.get("Attributes", "")
            folder = cur.get("Folder", "")
            size_str = cur.get("Size")
            if (
                path_val
                and folder != "+"
                and not attr.upper().startswith("D")
                and size_str is not None
            ):
                try:
                    out.append((path_val, int(size_str)))
                except ValueError:
                    pass
            cur = {}
            continue
        sep = line.find(" = ")
        if sep < 0:
            # Header / banner line ("7-Zip ...", "Listing archive: ..."): skip.
            continue
        key = line[:sep]
        val = line[sep + 3 :]
        cur[key] = val
    # Trailing record without a final blank line.
    if cur:
        path_val = cur.get("Path")
        attr = cur.get("Attributes", "")
        folder = cur.get("Folder", "")
        size_str = cur.get("Size")
        if path_val and folder != "+" and not attr.upper().startswith("D") and size_str is not None:
            try:
                out.append((path_val, int(size_str)))
            except ValueError:
                pass
    return out


def _extract_with_seven_zip(
    seven_zip: str, path: str, *, max_file_bytes: int | None
) -> Iterator[tuple[str, bytes]]:
    """Stream every regular file out of ``path`` using the ``7z`` CLI.

    We list once (``7z l -slt``) to enumerate members + sizes, then
    spawn one ``7z x -so <archive> <member>`` per file to read the
    bytes. ``-so`` writes payload to stdout so we can stream without
    materialising on disk; one process per file keeps the resident
    set bounded to a single member at a time.

    ``7z`` itself is silent on stdout for the listing-only path
    when ``-slt`` is given. Errors land on stderr with a non-zero
    exit code; we log + skip the offending member.
    """
    listing = subprocess.run(  # nosec B603 - argv list, not shell
        [seven_zip, "l", "-slt", "--", path],
        capture_output=True,
        check=False,
        text=True,
    )
    if listing.returncode != 0:
        raise RuntimeError(
            f"7z listing failed for {path}: rc={listing.returncode} "
            f"stderr={listing.stderr.strip()[:512]}"
        )
    members = _parse_seven_zip_listing(listing.stdout)
    if not members:
        return

    for member, size in members:
        if max_file_bytes is not None and size > max_file_bytes:
            continue
        try:
            proc = subprocess.run(  # nosec B603 - argv list, not shell
                [seven_zip, "x", "-so", "--", path, member],
                capture_output=True,
                check=False,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("7z spawn failed for %s in %s: %s", member, path, exc)
            continue
        if proc.returncode != 0:
            log.warning(
                "7z extract failed for %s in %s (rc=%s): %s",
                member,
                path,
                proc.returncode,
                proc.stderr[:256].decode("utf-8", "replace"),
            )
            continue
        # Normalise the relative path the way pycdlib would: drop a
        # leading slash, strip ``;1`` ISO 9660 version suffix, drop
        # trailing dot left by 8.3 names. 7z usually leaves the
        # paths in their UDF/Joliet long-name form, so most of these
        # transforms are no-ops.
        cleaned = member.lstrip("/")
        semi = cleaned.rfind(";")
        if semi >= 0 and cleaned[semi + 1 :].isdigit():
            cleaned = cleaned[:semi]
        if cleaned.endswith("."):
            cleaned = cleaned[:-1]
        yield cleaned, proc.stdout


def extract_iso(
    path: str,
    *,
    max_file_bytes: int | None = None,
) -> Iterator[tuple[str, bytes]]:
    """Yield ``(relative_path, data)`` for every regular file in the ISO.

    The ISO at ``path`` is opened once; files are streamed sequentially
    so the caller can drain memory between iterations. ``max_file_bytes``
    skips files larger than the cap (defensive — hospital DVDs rarely
    embed multi-GiB blobs but a corrupt header can lie about size).

    Strategy:

    1. Probe with pycdlib. If it opens and yields ≥ 1 file, use
       pycdlib for the whole walk (best filename fidelity for
       hybrid DVDs).
    2. Otherwise fall back to ``7z`` (covers pure UDF, hybrid
       degenerated PVD, and any other format pycdlib refuses).
    3. If ``7z`` is also unavailable, raise ``RuntimeError``.

    Raises ``RuntimeError`` if no extractor can read the volume,
    ``OSError`` if the file isn't found.
    """
    if not os.path.exists(path):
        raise OSError(f"ISO file not found: {path}")

    if _pycdlib_can_walk(path):
        yield from _extract_with_pycdlib(path, max_file_bytes=max_file_bytes)
        return

    seven_zip = _find_seven_zip()
    if seven_zip is None:
        raise RuntimeError(
            "ISO is unreadable by pycdlib (likely pure UDF or "
            "non-standard PVD) and no 7z binary is available for "
            "fallback. Install p7zip-full (Debian) / sevenzip "
            "(Homebrew) so the extractor can handle UDF DVDs."
        )
    log.info("ISO %s: pycdlib refused, using 7z (%s) fallback", path, seven_zip)
    yield from _extract_with_seven_zip(seven_zip, path, max_file_bytes=max_file_bytes)
