"""File-extension helpers shared by the export builder and the
document-download routes.

Kept as a dependency-free leaf module so both ``api/*`` routers and
``services/patient_export`` can import it without pulling in heavy
deps (stream_zip, boto3, ...) or risking an import cycle.

The single source of truth for "what extension does this blob get"
lives here. Two consumers:

* the Fascicolo export names every member file, and
* the per-document download endpoints set the ``Content-Disposition``
  filename.

Both must agree, and both must give the user a file whose name ends
in the RIGHT extension exactly once — the bug the helper fixes was a
referto saved as ``referto`` (no extension) or ``referto.pdf.pdf``
(doubled) depending on whether the title already carried it.
"""

from __future__ import annotations

# MIME-to-extension mapping for report / document blobs. Used to give
# a downloaded / exported file a sensible extension when the canonical
# S3 key does not already carry one.
_MIME_EXT: dict[str, str] = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
    "text/plain": "txt",
    "text/html": "html",
    "application/json": "json",
    "application/zip": "zip",
    "application/dicom": "dcm",
}


def ext_for(content_type: str | None, s3_key: str | None) -> str:
    """Best-effort file extension for a blob.

    Precedence: the extension already on the stored S3 key (the object
    was uploaded with a real filename) wins; otherwise map the MIME
    type; otherwise ``"bin"``.
    """
    if s3_key and "." in s3_key.rsplit("/", 1)[-1]:
        return s3_key.rsplit(".", 1)[-1].lower()
    if content_type and content_type in _MIME_EXT:
        return _MIME_EXT[content_type]
    return "bin"


def ensure_extension(
    name: str | None,
    *,
    content_type: str | None = None,
    s3_key: str | None = None,
) -> str:
    """Return ``name`` guaranteed to end in the correct extension once.

    * ``referto`` + ``application/pdf`` -> ``referto.pdf``
    * ``referto.pdf`` + ``application/pdf`` -> ``referto.pdf`` (kept)
    * ``referto.PDF`` -> ``referto.PDF`` (case-insensitive match, kept)

    The resolved extension comes from :func:`ext_for` (S3 key suffix
    first, then MIME). When the type is genuinely unknown the helper
    appends ``.bin`` only if the name has no suffix at all, so a name
    that already looks like a file ("scan.tiff") is never mangled.
    """
    base = (name or "").strip() or "document"
    ext = ext_for(content_type, s3_key)
    if base.casefold().endswith(f".{ext}".casefold()):
        return base
    if ext == "bin" and "." in base.rsplit("/", 1)[-1]:
        # Unknown type but the name already carries some extension —
        # leave it; slapping ``.bin`` on ``foo.xyz`` would be worse.
        return base
    return f"{base}.{ext}"


__all__ = ["ensure_extension", "ext_for"]
