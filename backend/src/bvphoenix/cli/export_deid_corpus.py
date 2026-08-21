"""CLI: export reviewer-labelled GT boxes to a private de-id regression corpus.

The M6c review UI persists ground-truth burned-in-PHI boxes on
``submissions.gt_boxes`` (per instance). This turns those labels into the
``load_public_corpus`` on-disk format (``{instance_id}.dcm`` + ``answer_key.json``
+ ``corpus.json``) so the pixel redactor's recall can be measured on REAL PHI,
not only synthetic fixtures.

REAL PHI: the exported instances carry burned-in PHI. Keep the local dir on an
encrypted disk, delete it after syncing to the restricted private bucket, and
NEVER let it enter git / an image / the OpenData library. The private bucket
(``bvphoenix-datasets-private``) has a deny-by-default policy + a dedicated IAM
key (not the runtime key).

    bvphoenix-export-deid-corpus --out /secure/curated-corpus
    bvphoenix-export-deid-corpus --out /secure/c --frame0-extract   # incl. multi-frame

Multi-frame instances are SKIPPED by default: the GT boxes are labelled against
frame 0 (``render.png?frame=0``) but ``clean_pixel_data`` flattens redactions
across all frames, so scoring a multi-frame instance can spuriously "cover" a
frame-0 box with a mask found on another frame. ``--frame0-extract`` rewrites
them to a single-frame copy so the scoring stays aligned.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import click
import pydicom
from sqlalchemy import select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import Submission
from bvphoenix.services.pixel_deid import reencode_pixel_data
from bvphoenix.storage import get_s3_storage


def _manifest_index(sub: Submission) -> dict[str, dict]:
    return {
        str(i.get("instance_id")): i
        for i in (sub.manifest or {}).get("instances", [])
        if i.get("instance_id") and i.get("s3_key")
    }


def _extract_frame0(blob: bytes) -> bytes:
    """Rewrite a multi-frame instance to a single-frame (frame 0) copy so the
    corpus stays aligned with the frame-0 GT labels."""
    ds = pydicom.dcmread(io.BytesIO(blob))
    import numpy as np

    arr = np.asarray(ds.pixel_array)
    frame0 = arr[0] if arr.ndim >= 3 and int(getattr(ds, "NumberOfFrames", 1) or 1) > 1 else arr
    ds.NumberOfFrames = 1
    reencode_pixel_data(ds, np.ascontiguousarray(frame0))
    out = io.BytesIO()
    ds.save_as(out, write_like_original=False)
    return out.getvalue()


def export_labeled_corpus(
    db: Session, storage, root: Path, *, frame0_extract: bool
) -> dict[str, int]:
    """Write every reviewer-labelled instance + ``answer_key.json`` + ``corpus.json``
    into ``root``. Pure over its (db, storage) inputs so it round-trips in tests."""
    settings = get_settings()
    root.mkdir(parents=True, exist_ok=True)
    answer_key: dict[str, list[dict]] = {}
    stats = {"instances": 0, "skipped_multiframe": 0, "skipped_no_blob": 0, "submissions": 0}

    subs = db.execute(select(Submission).where(Submission.gt_boxes.isnot(None))).scalars().all()
    for sub in subs:
        boxes_by_instance = sub.gt_boxes or {}
        if not boxes_by_instance:
            continue
        stats["submissions"] += 1
        index = _manifest_index(sub)
        for instance_id, boxes in boxes_by_instance.items():
            entry = index.get(str(instance_id))
            if entry is None:
                stats["skipped_no_blob"] += 1
                continue
            try:
                blob = storage.get_object_bytes(
                    bucket=entry.get("s3_bucket") or settings.s3_bucket_raw,
                    key=entry["s3_key"],
                )
            except Exception as exc:
                click.echo(f"  ! {instance_id}: blob unavailable ({exc})", err=True)
                stats["skipped_no_blob"] += 1
                continue
            nframes = int(
                getattr(
                    pydicom.dcmread(io.BytesIO(blob), stop_before_pixels=True, force=True),
                    "NumberOfFrames",
                    1,
                )
                or 1
            )
            if nframes > 1:
                if not frame0_extract:
                    stats["skipped_multiframe"] += 1
                    continue
                blob = _extract_frame0(blob)
            name = f"{instance_id}.dcm"
            (root / name).write_bytes(blob)
            answer_key[name] = [
                {
                    "x": int(b["x"]),
                    "y": int(b["y"]),
                    "w": int(b["w"]),
                    "h": int(b["h"]),
                    "text": str(b.get("text", "")),
                    "category": str(b.get("category", "unknown")),
                }
                for b in boxes
            ]
            stats["instances"] += 1

    key_bytes = json.dumps(answer_key, indent=2, ensure_ascii=False).encode("utf-8")
    (root / "answer_key.json").write_bytes(key_bytes)
    (root / "corpus.json").write_text(
        json.dumps(
            {
                "kind": "curated",
                "cases": stats["instances"],
                "answer_key_sha256": hashlib.sha256(key_bytes).hexdigest(),
                "exported_at": datetime.now(UTC).isoformat(),
                "app_version": settings.app_version,
            },
            indent=2,
        )
    )
    return stats


@click.command()
@click.option("--out", "out_dir", required=True, type=click.Path(), help="Corpus directory.")
@click.option(
    "--frame0-extract",
    is_flag=True,
    help="Include multi-frame instances by extracting frame 0 (else skip them).",
)
def main(out_dir: str, frame0_extract: bool) -> None:
    from bvphoenix.db.engine import make_sync_engine

    root = Path(out_dir)
    engine = make_sync_engine(get_settings().database_url_sync)
    with Session(engine) as db:
        stats = export_labeled_corpus(db, get_s3_storage(), root, frame0_extract=frame0_extract)
    click.echo(
        f"exported {stats['instances']} case(s) from {stats['submissions']} submission(s) "
        f"to {root} (skipped {stats['skipped_multiframe']} multi-frame, "
        f"{stats['skipped_no_blob']} without a blob)"
    )
    click.echo(
        "REAL PHI on disk — keep encrypted, sync to bvphoenix-datasets-private, then delete."
    )


if __name__ == "__main__":
    main()
