"""CLI: score the pixel redactor's recall over a corpus and persist the run.

Iterates a ``load_public_corpus`` dir (synthetic / public TCIA / curated real),
runs ``clean_pixel_data`` per case, aggregates ``score_redaction``, and inserts
one ``deid_recall_runs`` row — the tracked-over-time counterpart to the M6c
per-instance ``gt-score`` endpoint. Prints a one-line, greppable summary
(mirrors the embed-coverage monitor) so a CronJob's logs stay auditable.

    bvphoenix-deid-recall --corpus-kind curated --corpus-root /secure/corpus
    bvphoenix-deid-recall --corpus-kind curated --corpus-s3 s3://bvphoenix-datasets-private/curated/v1
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import click
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import CORPUS_KINDS, DeidRecallRun
from bvphoenix.services.pixel_deid import clean_pixel_data
from bvphoenix.services.pixel_deid_eval import load_public_corpus, score_redaction


def engine_fingerprint() -> dict:
    """The config the recall was scored under — trend queries group by it."""
    s = get_settings()
    return {
        "app_version": s.app_version,
        "deid_method_version": s.deid_method_version,
        "redaction_mode": s.pixel_deid_redaction_mode,
        "vlm_enabled": bool(s.pixel_phi_vlm_enabled),
        "tesseract": shutil.which("tesseract") is not None,
    }


def _sync_from_s3(uri: str) -> Path:
    """Download an ``s3://bucket/prefix`` corpus to a temp dir (model-sync style,
    the dedicated datasets key is expected in the environment)."""
    from bvphoenix.storage import get_s3_storage

    assert uri.startswith("s3://")
    bucket, _, prefix = uri[5:].partition("/")
    storage = get_s3_storage()
    dest = Path(tempfile.mkdtemp(prefix="deid-recall-corpus-"))
    for key, _size in storage.list_objects(bucket=bucket, prefix=prefix):
        rel = key[len(prefix) :].lstrip("/")
        if not rel:
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(storage.get_object_bytes(bucket=bucket, key=key))
    return dest


def evaluate_corpus(root: Path, *, coverage: float) -> tuple[dict, list[dict]]:
    """Aggregate recall over ``root``. Returns (totals, missed_sample)."""
    covered = total = cases = 0
    missed: list[dict] = []
    for case in load_public_corpus(root):
        if not case.gt:
            continue
        cases += 1
        res = clean_pixel_data(case.dicom)
        masked = [(m["x"], m["y"], m["w"], m["h"]) for m in res.redactions]
        score = score_redaction(case.gt, masked, coverage=coverage)
        covered += score.covered
        total += score.total
        for text in score.missed:
            if len(missed) < 200:
                missed.append({"text": text})
    recall = covered / total if total else 1.0
    return (
        {"recall": recall, "covered": covered, "total": total, "cases": cases},
        missed,
    )


@click.command()
@click.option("--corpus-kind", type=click.Choice(CORPUS_KINDS), required=True)
@click.option("--corpus-root", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--corpus-s3", default=None, help="s3://bucket/prefix (synced to a temp dir).")
@click.option("--corpus-version", default=None, help="Free-text version label recorded on the run.")
@click.option("--coverage", default=0.8, show_default=True)
def main(
    corpus_kind: str,
    corpus_root: Path | None,
    corpus_s3: str | None,
    corpus_version: str | None,
    coverage: float,
) -> None:
    if not corpus_root and not corpus_s3:
        raise click.UsageError("one of --corpus-root / --corpus-s3 is required")
    tmp: Path | None = None
    root = corpus_root
    if corpus_s3:
        tmp = _sync_from_s3(corpus_s3)
        root = tmp
    assert root is not None

    key_path = root / "answer_key.json"
    corpus_hash = hashlib.sha256(key_path.read_bytes()).hexdigest() if key_path.exists() else None
    version = corpus_version
    meta_path = root / "corpus.json"
    if version is None and meta_path.exists():
        version = json.loads(meta_path.read_text()).get("kind")

    try:
        totals, missed = evaluate_corpus(root, coverage=coverage)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    settings = get_settings()
    db_engine = create_engine(settings.database_url_sync, future=True)
    with Session(db_engine) as db:
        run = DeidRecallRun(
            corpus_kind=corpus_kind,
            corpus_version=version,
            corpus_hash=corpus_hash,
            engine=engine_fingerprint(),
            coverage=coverage,
            recall=totals["recall"],
            covered=totals["covered"],
            total=totals["total"],
            cases=totals["cases"],
            missed={"sample": missed} if missed else None,
        )
        db.add(run)
        db.commit()
        run_id = str(run.id)

    click.echo(
        f"deid-recall kind={corpus_kind} cases={totals['cases']} "
        f"recall={totals['recall']:.4f} covered={totals['covered']}/{totals['total']} "
        f"missed={len(missed)} run={run_id}"
    )


if __name__ == "__main__":
    main()
