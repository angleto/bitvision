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
import subprocess
import sys
from pathlib import Path

# PP-OCRv5 server/mobile detection model identifier as published by PaddleOCR.
# Pin the exact name/version at validation time.
_DET_MODEL_NAME = "PP-OCRv5_mobile_det"
_OUT_FILENAME = "ppocrv5_det.onnx"


def _download_det_model() -> Path:
    """Resolve the local path of the PP-OCRv5 detection *inference* model,
    downloading it via paddleocr if needed. Returns the inference-model dir
    (containing inference.pdmodel / inference.pdiparams)."""
    try:
        from paddleocr import PaddleOCR  # noqa: F401  (triggers the model download)
    except ImportError as exc:  # pragma: no cover - export extra only
        raise SystemExit(
            "paddleocr not installed; run `uv sync --extra export` first"
        ) from exc

    # Instantiating PaddleOCR with detection enabled downloads the det model to
    # ~/.paddleocr/. The precise on-disk layout depends on the paddleocr
    # version; resolve it explicitly at validation time and return that dir.
    raise SystemExit(
        "TODO(validate-on-arm): resolve the downloaded "
        f"{_DET_MODEL_NAME} inference-model directory and return it here. "
        "Kept explicit rather than guessed so this never silently exports the "
        "wrong graph."
    )


def export(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    model_dir = _download_det_model()
    out_path = out_dir / _OUT_FILENAME
    cmd = [
        sys.executable,
        "-m",
        "paddle2onnx",
        "--model_dir",
        str(model_dir),
        "--model_filename",
        "inference.pdmodel",
        "--params_filename",
        "inference.pdiparams",
        "--save_file",
        str(out_path),
        "--opset_version",
        "13",
        "--enable_onnx_checker",
        "True",
    ]
    subprocess.run(cmd, check=True)
    print(f"wrote {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="./models", type=Path)
    args = parser.parse_args()
    export(args.out_dir)


if __name__ == "__main__":
    main()
