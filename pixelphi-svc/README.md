# pixelphi-svc

CPU ONNX **burned-in-pixel PHI text-region detector** for bitvision phoenix.

This is the "hard case" tier of the anonymizer (milestone M5). The backend's
cheap Tesseract tier (`backend/.../services/pixel_deid.py`) handles the common
case; when it finds **no** text on a frame the header gate already flagged
high-risk (dense overlay, low contrast, non-Latin script, the case Tesseract
misses), the backend's `HttpPixelPhiEngine`
(`backend/.../services/pixel_phi_engine.py`) consults this service for
additional regions to mask.

## Contract

- `POST /detect`, body `{"image_png_b64": "<base64 PNG of one grayscale
  frame>"}`; returns `{"boxes": [{"x","y","w","h","conf"}], "model_id",
  "model_loaded"}`. **Coordinates only, never image bytes**: the backend blacks
  the boxes out. This service never emits a redacted image and never makes a
  "this text is clinical, keep it" decision, the backend over-redacts every
  returned box.
- `GET /healthz`, `{"status","model_loaded","model_id"}`.

## Safety posture

- **Storage isolation.** The backend only ever POSTs here over an in-cluster,
  host-allowlisted URL (`BVP_PIXEL_PHI_ALLOWED_HOSTS`). A PHI-bearing crop
  never leaves the cluster. This service touches no S3, no database, and
  persists nothing; a frame lives in memory only for the request.
- **Fail closed.** If the detector model is absent, or this service is
  unreachable, or it returns no boxes on a frame the backend already considers
  suspicious, the backend masks the **whole frame**. A missing model degrades
  to over-redaction, never to a leak.
- **Human review still mandatory** (MIDI-B): no automated method reaches 100%
  on pixel PHI, so every high-risk public contribution still passes through the
  human review quarantine. This tier raises recall; it does not replace review.

## Model

Apache-2.0 **PP-OCRv5 mobile detection** graph, exported to ONNX out-of-band by
`scripts/export_onnx.py` and uploaded to
`s3://bvphoenix-models-prod/pixelphi/v1/`. Weights are **not baked** into the
image; a model-sync init container pulls them into `BVP_PIXELPHI_MODEL_DIR` at
deploy. Recognition is intentionally not run (only detection), reading the
text is unnecessary when every box is masked, and would add a re-identification
surface.

DBNet box extraction (threshold → contours → unclip approximation) runs in
`engine.DetectorEngine`. A GPU model drops in behind the same `/detect`
contract without touching the backend.

## Config (env, prefix `BVP_PIXELPHI_`)

| var | default | meaning |
|-----|---------|---------|
| `MODEL_DIR` | `/app/models` | dir holding the synced ONNX graph |
| `DETECTOR_MODEL` | `ppocrv5_det.onnx` | detector graph filename |
| `ONNX_INTRA_OP_THREADS` | `2` | onnxruntime intra-op threads |
| `DET_DB_THRESH` / `DET_DB_BOX_THRESH` / `DET_DB_UNCLIP_RATIO` | `0.3` / `0.5` / `1.6` | DBNet post-process |
| `MAX_IMAGE_SIDE` | `4096` | downscale longest side before detect |
| `EAGER_LOAD` | `false` | warm the ONNX session at startup |
| `HOST` / `PORT` | `0.0.0.0` / `8091` | bind address |

## Build

```sh
# from repo root
docker build -f infra/dockerfiles/pixelphi-svc.Dockerfile -t bvphoenix-pixelphi-svc .
```

The tier is gated off by default (`BVP_PIXEL_PHI_VLM_ENABLED=false` on the
backend); enable it only after the ONNX export is smoke-tested on ARM64 and the
model is synced.
