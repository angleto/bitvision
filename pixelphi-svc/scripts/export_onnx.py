"""Export the PaddleOCR PP-OCRv5 text-detection model to ONNX for CPU inference.

Run OUT-OF-BAND (not at image-build time, not at runtime), then upload the
result to ``s3://bvphoenix-models-prod/pixelphi/v1/``. At deploy the model-sync
init container pulls it into ``BVP_PIXELPHI_MODEL_DIR``, weights are never
baked into the runtime image (mirrors the model-sync pattern already used for
the embedding model). Requires the ``export`` extra::

    uv sync --extra export
    uv run python scripts/export_onnx.py --out-dir ./models

What it does
------------
1. Downloads the PP-OCRv5 mobile *detection* inference model via paddleocr
   (Apache-2.0). The detection model is all this service needs: it emits a text
   probability map; box extraction (DBNet post-process) happens in the service
   (``engine.DetectorEngine._boxes_from_prob``), and recognition is intentionally
   NOT run here (the backend over-redacts every detected box, so reading the text
   is unnecessary and would only add a re-identification surface).
2. Converts the Paddle inference model to ONNX with paddle2onnx and writes it to
   ``<out_dir>/ppocrv5_det.onnx`` (the filename ``BVP_PIXELPHI_DETECTOR_MODEL``
   defaults to).

NOTE / uncertainty: the exact PP-OCRv5 model name and the paddle2onnx invocation
must be smoke-tested on the target ARM64 toolchain before enabling the tier in
production (see the plan's risk note: "Export ONNX PaddleOCR su ARM"). docTR
(Apache-2.0) is the documented fallback detector if the Paddle→ONNX path proves
brittle on aarch64. This script encodes the intended path; it is not a substitute
for that validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

# PP-OCRv5 mobile detection model identifier as published by PaddleOCR 3.x.
_DET_MODEL_NAME = "PP-OCRv5_mobile_det"
_OUT_FILENAME = "ppocrv5_det.onnx"


def _download_det_model() -> Path:
    """Download + resolve the PP-OCRv5 detection *inference* model dir.

    PaddleOCR 3.x downloads official models to
    ``~/.paddlex/official_models/<name>/``; instantiating the text-detection
    predictor with the model name triggers the pull. Returns the dir holding
    the inference graph + params (PIR ``inference.json`` on Paddle 3.x, or the
    legacy ``inference.pdmodel`` on 2.x — :func:`_resolve_model_files` handles
    both)."""
    try:
        from paddleocr import TextDetection
    except ImportError as exc:  # pragma: no cover - export extra only
        raise SystemExit("paddleocr not installed; run `uv sync --extra export`") from exc

    # Instantiating the predictor downloads the model if absent.
    TextDetection(model_name=_DET_MODEL_NAME)

    base = Path.home() / ".paddlex" / "official_models" / _DET_MODEL_NAME
    if not base.exists():
        # Older/newer layouts: search the paddlex cache for the model dir.
        cache = Path.home() / ".paddlex"
        hits = [p.parent for p in cache.rglob("inference.*") if _DET_MODEL_NAME in str(p)]
        if not hits:
            raise SystemExit(
                f"could not locate the downloaded {_DET_MODEL_NAME} inference dir under {cache}; "
                "inspect the paddlex cache and pass the dir explicitly"
            )
        base = hits[0]
    return base


def _resolve_model_files(model_dir: Path) -> list[str]:
    """paddle2onnx args for the model, autodetecting PIR (Paddle 3.x,
    ``inference.json``) vs the legacy ``inference.pdmodel`` (Paddle 2.x).
    paddle2onnx >= 1.3 also accepts ``--model_dir`` alone, but being explicit
    avoids picking up the wrong file."""
    if (model_dir / "inference.json").exists():
        model_filename = "inference.json"
    elif (model_dir / "inference.pdmodel").exists():
        model_filename = "inference.pdmodel"
    else:
        raise SystemExit(f"no inference.json / inference.pdmodel in {model_dir}")
    return [
        "--model_dir",
        str(model_dir),
        "--model_filename",
        model_filename,
        "--params_filename",
        "inference.pdiparams",
    ]


def export(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    model_dir = _download_det_model()
    out_path = out_dir / _OUT_FILENAME
    cmd = [
        sys.executable,
        "-m",
        "paddle2onnx",
        *_resolve_model_files(model_dir),
        "--save_file",
        str(out_path),
        "--opset_version",
        "13",
        "--enable_onnx_checker",
        "True",
    ]
    subprocess.run(cmd, check=True)

    # Record provenance next to the graph (consumed by the model-sync + the
    # recall-run engine fingerprint). ``.complete`` is the marker the workers
    # model-sync init container asserts before starting the service.
    sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
    (out_dir / "version.json").write_text(
        json.dumps(
            {"model_id": "pixelphi-ppocrv5-det-v1", "source": _DET_MODEL_NAME, "sha256": sha},
            indent=2,
        )
    )
    (out_dir / ".complete").write_text("")
    print(f"wrote {out_path} (sha256={sha[:12]}...) + version.json + .complete")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="./models", type=Path)
    args = parser.parse_args()
    export(args.out_dir)


if __name__ == "__main__":
    main()
