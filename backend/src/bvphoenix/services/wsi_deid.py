"""WSI de-identification helpers.

The high-risk PHI surface on a whole-slide image is the **label**
associated image: the photo of the printed glass-slide label, which
on most lab outputs carries patient name + MRN + date of birth in
handwritten or printed form (see ``docs/pathology_wsi_spike.md`` §6).

Step-1 policy: the label is *never* written to S3 by default. The
``s3_label_key`` column on ``pathology_slides`` stays NULL and
``label_redacted=true``. The macro image (1× whole-slide overview)
is normally clean but occasionally carries a lab barcode tying back
to the LIS; we strip the bottom 12 % strip via PIL as a low-cost
safety net before upload.

OpenSlide's ``slide.properties`` exposes scanner-injected metadata.
Most of it (mpp, magnification, scanner make/model) is harmless and
useful; a handful of vendor-specific fields (operator name,
acquisition workstation) can leak identifiers. ``safe_properties``
returns a curated subset rather than the full dictionary so the
ingest service does not stash raw vendor strings in the DB.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

# Properties we explicitly keep when reading openslide.properties.
# Anything not on this list is dropped — defence in depth against a
# vendor-specific PHI tag landing under e.g. ``aperio.User`` or
# ``hamamatsu.Created``. Add to this list deliberately, never via a
# wildcard.
_SAFE_PROPERTY_PREFIXES: tuple[str, ...] = (
    "openslide.mpp-x",
    "openslide.mpp-y",
    "openslide.objective-power",
    "openslide.vendor",
    "openslide.level-count",
    "openslide.bounds-",
    "openslide.background-color",
    "openslide.level[0].downsample",
    "openslide.level[0].height",
    "openslide.level[0].width",
    "openslide.icc-size",
    # Aperio / Hamamatsu / Leica scanner-model markers — make/model
    # only; explicitly NOT the User / Workstation / Date fields.
    "aperio.AppMag",
    "aperio.MPP",
    "aperio.ScanScope",
    "hamamatsu.SourceLens",
    "hamamatsu.NDP.S/N",
)


def safe_properties(properties: dict[str, str] | Any) -> dict[str, str]:
    """Return only the openslide property keys on the allow-list above.

    Vendor strings outside the curated set are dropped to avoid
    accidentally persisting operator names / workstation IDs / lab
    barcodes embedded in scanner metadata.
    """
    out: dict[str, str] = {}
    for k, v in properties.items():
        if any(k == p or k.startswith(p) for p in _SAFE_PROPERTY_PREFIXES):
            out[k] = str(v)
    return out


def extract_macro_jpeg(slide: Any, *, max_dim: int = 1024, quality: int = 85) -> bytes | None:
    """Return a JPEG bytestream of the macro overview, or None if absent.

    Most scanners embed a 1× whole-slide overview under
    ``slide.associated_images['macro']``. We strip the bottom 12 %
    (where a lab barcode often sits) and downscale to ``max_dim`` on
    the longest side. The result is safe to expose as a card preview.
    """
    macro = slide.associated_images.get("macro")
    if macro is None:
        return None

    # PIL Image. Pixmeo + Aperio always produce RGB(A); convert
    # defensively before crop / save.
    img = macro.convert("RGB") if macro.mode != "RGB" else macro
    w, h = img.size
    crop_h = int(h * 0.88)
    img = img.crop((0, 0, w, crop_h))

    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim))

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def generate_thumbnail_jpeg(slide: Any, *, max_dim: int = 512, quality: int = 85) -> bytes:
    """Return a JPEG bytestream of the slide thumbnail.

    Uses OpenSlide's native ``get_thumbnail`` which walks down to the
    smallest pyramid level that satisfies the requested size and
    blends to RGB. No PHI surface here — the thumbnail is downsampled
    tissue, not the slide label.
    """
    thumb = slide.get_thumbnail((max_dim, max_dim))
    if thumb.mode != "RGB":
        thumb = thumb.convert("RGB")
    buf = BytesIO()
    thumb.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
