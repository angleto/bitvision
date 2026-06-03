"""Which DICOM series are embeddable by BiomedCLIP — single source of truth.

Shared by every embed enqueue path (the ``embed_series`` worker, the
``bvphoenix-backfill embed`` CLI, the ``POST /api/series/{id}/embed``
endpoint — and through it the MCP ``embed_series`` tool — plus the admin
``embed-missing`` / ``retry-failed`` endpoints), so the "should we embed
this?" policy lives in exactly one place.

BiomedCLIP embeds diagnostic raster images. Two classes of DICOM object
must never be enqueued for it:

* No-pixel objects — Structured Reports, Presentation States, Key Object
  Selection, Encapsulated PDF / CDA. These have no ``PixelData`` and blow
  up on decode.
* Non-image raster-shaped objects — Segmentation (binary label maps),
  RT Structure Set / Plan / Dose, Spatial Registration. A SEG decodes to
  a frame layout PIL cannot handle, and embedding a mask with BiomedCLIP
  is semantically meaningless.

We filter on *both* the DICOM Modality code (cheap, at enqueue time, whole
series) and the SOP Class UID (per instance, in the worker — the backstop
for mixed-SOP or mislabeled series). The Modality check is a BLOCKLIST,
not an allowlist: an unrecognised modality is *let through* to the worker,
which skips it terminally if it turns out to have no usable pixels. That
way a real image with an unusual Modality code is never silently dropped,
while a non-image that slips through costs exactly one cheap worker skip
(no retry storm) and never a poisoned ``embedding_errors`` row.

The SOP-class set is built by EXTENDING the canonical
``NO_PIXEL_DATA_SOP_CLASSES`` from :mod:`bvphoenix.services.thumbnails`
(never recopying its UIDs) with the SEG / RT / REG classes that carry
non-diagnostic frames.
"""

from __future__ import annotations

from bvphoenix.services.thumbnails import NO_PIXEL_DATA_SOP_CLASSES

# DICOM Modality codes that are never a diagnostic raster image. Compared
# case-folded + stripped against the ``series.modality`` column. These are
# the unambiguous non-image modalities; anything not listed (CT, MR, DX,
# CR, PT, US, NM, XA, MG, OT, SC, ...) is treated as embeddable and left to
# the worker's pixel-decode backstop.
NON_EMBEDDABLE_MODALITIES: frozenset[str] = frozenset(
    {
        "SR",  # Structured Report
        "PR",  # Presentation State
        "KO",  # Key Object Selection
        "SEG",  # Segmentation (binary label map)
        "REG",  # Spatial / deformable Registration
        "RTSTRUCT",  # RT Structure Set
        "RTPLAN",  # RT Plan
        "RTDOSE",  # RT Dose
        "RTRECORD",  # RT Treatment Record
        "DOC",  # Encapsulated document (PDF / CDA)
        "AU",  # Audio
        "ECG",  # Electrocardiography waveform
        "EPS",  # Cardiac electrophysiology waveform
        "HD",  # Hemodynamic waveform
        "RESP",  # Respiratory waveform
        "FID",  # Fiducials
        "PLAN",  # Plan
        "RWV",  # Real World Value Map
        "STAIN",  # Automatic Slide Stain
        "M3D",  # Model for 3D manufacturing
    }
)

# These tokens are inlined verbatim into SQL by ``embeddable_modality_clause``;
# the assertion guarantees they stay injection-safe (uppercase alphanumerics
# only) even if the set is edited later.
assert all(m.isalnum() and m.isupper() for m in NON_EMBEDDABLE_MODALITIES)

# Non-image raster-shaped SOP classes on top of the no-pixel ones. SEG / RT
# / REG technically carry frames or structured data but are not diagnostic
# images. Extends the canonical no-pixel set; does not recopy it.
_SEG_RT_REG_SOP_CLASSES: frozenset[str] = frozenset(
    {
        "1.2.840.10008.5.1.4.1.1.66.1",  # Spatial Registration
        "1.2.840.10008.5.1.4.1.1.66.3",  # Deformable Spatial Registration
        "1.2.840.10008.5.1.4.1.1.66.4",  # Segmentation
        "1.2.840.10008.5.1.4.1.1.66.5",  # Surface Segmentation
        "1.2.840.10008.5.1.4.1.1.481.2",  # RT Dose
        "1.2.840.10008.5.1.4.1.1.481.3",  # RT Structure Set
        "1.2.840.10008.5.1.4.1.1.481.4",  # RT Beams Treatment Record
        "1.2.840.10008.5.1.4.1.1.481.5",  # RT Plan
    }
)

NON_EMBEDDABLE_SOP_CLASSES: frozenset[str] = NO_PIXEL_DATA_SOP_CLASSES | _SEG_RT_REG_SOP_CLASSES


class SeriesNotEmbeddable(Exception):
    """A series BiomedCLIP cannot / should not embed.

    Raised on the embed path to signal a TERMINAL SKIP: the worker returns
    a ``skipped`` status instead of writing an ``embedding_errors`` row or
    re-raising (which would make arq retry forever). Carries a short
    machine-readable ``reason`` for the worker's return payload and logs.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_embeddable_modality(modality: str | None) -> bool:
    """True unless the Modality code is a known non-image one.

    Blocklist semantics: ``None`` / unknown is let through (the worker's
    per-instance SOP + pixel-decode check is the backstop). Case-folded and
    stripped so ``" sr "`` / ``"Sr"`` are caught like ``"SR"``.
    """
    if not modality:
        return True
    return modality.strip().upper() not in NON_EMBEDDABLE_MODALITIES


def is_embeddable_sop_class(sop_class_uid: str | None) -> bool:
    """True unless the SOP Class UID is a no-pixel or non-image-raster one.

    ``None`` / unknown is let through (legacy data with no recorded SOP
    class); the pixel-decode step is the final backstop.
    """
    if not sop_class_uid:
        return True
    return sop_class_uid not in NON_EMBEDDABLE_SOP_CLASSES


def embeddable_modality_clause(column: str) -> str:
    """A SQL boolean predicate selecting rows whose Modality is embeddable.

    ``column`` is the SQL expression for the modality column (e.g.
    ``"s.modality"``). Blocklist semantics matching
    :func:`is_embeddable_modality`: ``NULL`` / unknown passes; a known
    non-image modality is excluded. The blocklist tokens are inlined as a
    literal ``IN (...)`` (they are code-defined uppercase alphanumerics —
    see the assertion above — so there is no injection surface, and this
    avoids array-bind quirks across the sync CLI driver and asyncpg).
    """
    blocked = ", ".join(f"'{m}'" for m in sorted(NON_EMBEDDABLE_MODALITIES))
    return f"({column} IS NULL OR UPPER(TRIM({column})) NOT IN ({blocked}))"
