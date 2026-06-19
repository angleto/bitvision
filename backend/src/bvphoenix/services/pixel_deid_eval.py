"""Synthetic hard-case generator + scorer for burned-in-pixel de-identification.

The development dataset for the pixel-redaction pipeline (M2). Public corpora
(TCIA Pseudo-PHI-DICOM-Data, MIDI-B) are pulled marker-gated in CI; this module
*generates* unlimited synthetic cases — clean DICOM frames with PHI text drawn
into the pixels at known bounding boxes — including Italian PHI (codice fiscale,
names, addresses, dates) that the public sets lack. The ground truth is the set
of drawn boxes, so a redactor can be scored by how much of each PHI box it masks.

Fixtures carry PHI ONLY in pixels (empty header) so the score isolates the
burned-in-pixel pipeline from the header engine.
"""

from __future__ import annotations

import io
import json
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

# US Image Storage / Secondary Capture — both classify high (services.pixel_deid).
US_SOP = "1.2.840.10008.5.1.4.1.1.6.1"
SC_SOP = "1.2.840.10008.5.1.4.1.1.7"

PhiCategory = str  # name | codice_fiscale | date | address | phone | email | mrn


@dataclass(frozen=True)
class GtBox:
    x: int
    y: int
    w: int
    h: int
    text: str
    category: PhiCategory


@dataclass
class FixtureCase:
    dicom: bytes
    gt: list[GtBox]
    modality: str
    sop_class: str
    difficulty: str = "synthetic"


def _font(size: int = 26) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Pillow >= 10.1 load_default(size=) is scalable; large enough for Tesseract.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


_SURNAMES = ("ROSSI", "BIANCHI", "FERRARI", "ESPOSITO", "RUSSO", " COLOMBO ".strip())
_GIVENS = ("MARIO", "GIULIA", "LUCA", "ANNA", "FRANCESCA", "PAOLO")
_STREETS = ("VIA ROMA", "VIALE EUROPA", "PIAZZA GARIBALDI", "CORSO ITALIA")


def _rand_cf(rng: random.Random) -> str:
    # Format-valid Italian codice fiscale: matches deidentify._TAX_ID_RE
    # (6 letters, 2 digits, letter, 2 digits, letter, 3 digits, letter). The
    # check char is not validated — the fixtures exercise the regex shape.
    let = lambda: rng.choice("ABCDEFGHILMNOPRSTUVZ")  # noqa: E731
    dig = lambda: rng.choice("0123456789")  # noqa: E731
    return (
        "".join(let() for _ in range(6))
        + dig()
        + dig()
        + let()
        + dig()
        + dig()
        + let()
        + dig()
        + dig()
        + dig()
        + let()
    )


def italian_phi(rng: random.Random) -> list[tuple[str, PhiCategory]]:
    name = f"{rng.choice(_SURNAMES)} {rng.choice(_GIVENS)}"
    return [
        (name, "name"),
        (_rand_cf(rng), "codice_fiscale"),
        (f"{rng.randint(1, 28):02d}/{rng.randint(1, 12):02d}/19{rng.randint(30, 70)}", "date"),
        (f"{rng.choice(_STREETS)} {rng.randint(1, 199)}", "address"),
        (f"+39 3{rng.randint(10, 99)} {rng.randint(1000000, 9999999)}", "phone"),
        (f"MRN-{rng.randint(100000, 999999)}", "mrn"),
    ]


def _build_image_dicom(
    arr: np.ndarray, *, modality: str, sop_class: str, photometric: str
) -> bytes:
    ds = Dataset()
    # No header PHI: the test isolates the burned-in-PIXEL pipeline.
    ds.PatientName = ""
    ds.PatientID = ""
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = UID(sop_class)
    ds.Modality = modality
    ds.Rows = int(arr.shape[0])
    ds.Columns = int(arr.shape[1])
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    if photometric == "RGB":
        ds.SamplesPerPixel = 3
        ds.PlanarConfiguration = 0
        ds.PhotometricInterpretation = "RGB"
    else:
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = photometric
    ds.PixelData = arr.tobytes()

    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = UID(sop_class)
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm

    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def synthesize_case(
    *,
    seed: int = 0,
    modality: str = "US",
    sop_class: str = US_SOP,
    size: tuple[int, int] = (420, 560),
    photometric: str = "MONOCHROME2",
    phi_items: list[tuple[str, PhiCategory]] | None = None,
    background: int = 48,
) -> FixtureCase:
    """Generate one synthetic burned-in-PHI DICOM + its ground-truth boxes.

    Deterministic for a given ``seed``. PHI is drawn as bright text on a dark
    background (a US-banner / secondary-capture style overlay), top-left, one
    line per item; the returned boxes are the exact drawn extents.
    """
    rng = random.Random(seed)
    h, w = size
    items = phi_items if phi_items is not None else italian_phi(rng)
    arr = np.full((h, w), background, dtype=np.uint8)
    img = Image.fromarray(arr, mode="L")
    draw = ImageDraw.Draw(img)
    font = _font()

    gt: list[GtBox] = []
    y = 8
    for text, category in items:
        left, top, right, bottom = (int(v) for v in draw.textbbox((8, y), text, font=font))
        draw.text((8, y), text, fill=255, font=font)
        gt.append(
            GtBox(x=left, y=top, w=right - left, h=bottom - top, text=text, category=category)
        )
        y = bottom + 8

    out_arr = np.asarray(img, dtype=np.uint8)
    dicom = _build_image_dicom(
        out_arr, modality=modality, sop_class=sop_class, photometric=photometric
    )
    return FixtureCase(dicom=dicom, gt=gt, modality=modality, sop_class=sop_class)


# --- scoring ----------------------------------------------------------------

Box = tuple[int, int, int, int]  # (x, y, w, h)


def _xywh(box: GtBox | Box) -> Box:
    if isinstance(box, GtBox):
        return (box.x, box.y, box.w, box.h)
    return box


def _coverage_fraction(gt: Box, masked: list[Box]) -> float:
    """Fraction of the GT box area covered by the UNION of masked boxes.

    Rasterised so per-word OCR masks (which each cover only part of a multi-word
    PHI line) jointly count — a per-box max would under-report a fully-masked
    line split across several word boxes.
    """
    x, y, w, h = gt
    if w <= 0 or h <= 0:
        return 1.0
    cov = np.zeros((h, w), dtype=bool)
    for mx, my, mw, mh in masked:
        ix1, iy1 = max(x, mx), max(y, my)
        ix2, iy2 = min(x + w, mx + mw), min(y + h, my + mh)
        if ix2 > ix1 and iy2 > iy1:
            cov[iy1 - y : iy2 - y, ix1 - x : ix2 - x] = True
    return float(cov.mean())


@dataclass
class RedactionScore:
    recall: float
    covered: int
    total: int
    missed: list[str] = field(default_factory=list)


def score_redaction(
    gt: list[GtBox], masked: list[GtBox | Box], *, coverage: float = 0.8
) -> RedactionScore:
    """Recall = fraction of ground-truth PHI boxes whose area is masked by at
    least ``coverage`` (default 80%). A miss means residual PHI on the image."""
    masked_xywh = [_xywh(m) for m in masked]
    covered = 0
    missed: list[str] = []
    for g in gt:
        if _coverage_fraction(_xywh(g), masked_xywh) >= coverage:
            covered += 1
        else:
            missed.append(g.text)
    recall = covered / len(gt) if gt else 1.0
    return RedactionScore(recall=recall, covered=covered, total=len(gt), missed=missed)


def load_public_corpus(root: str | Path) -> Iterator[FixtureCase]:
    """Yield a FixtureCase per DICOM under ``root`` — a marker-gated, synced copy
    of a public evaluation corpus (TCIA Pseudo-PHI-DICOM-Data CC-BY-4.0 /
    MIDI-B). Ground-truth boxes come from an optional ``answer_key.json``
    (filename -> [{x,y,w,h,text,category}]).

    Yields nothing when ``root`` is absent, so CI skips the public corpus when it
    has not been synced (it is tracked, not a hard gate — TCIA pixel labels are
    noisy; the synthetic set is the hard recall gate). The pull itself follows
    the model-sync pattern (a dataset bucket → local dir), never baked into
    images or committed to git.
    """
    root = Path(root)
    if not root.exists():
        return
    answers: dict = {}
    key = root / "answer_key.json"
    if key.exists():
        answers = json.loads(key.read_text())
    for path in sorted(root.rglob("*.dcm")):
        gt = [
            GtBox(
                x=int(b["x"]),
                y=int(b["y"]),
                w=int(b["w"]),
                h=int(b["h"]),
                text=str(b.get("text", "")),
                category=str(b.get("category", "unknown")),
            )
            for b in answers.get(path.name, [])
        ]
        yield FixtureCase(
            dicom=path.read_bytes(), gt=gt, modality="", sop_class="", difficulty="public"
        )


__all__ = [
    "SC_SOP",
    "US_SOP",
    "FixtureCase",
    "GtBox",
    "RedactionScore",
    "italian_phi",
    "load_public_corpus",
    "score_redaction",
    "synthesize_case",
]
