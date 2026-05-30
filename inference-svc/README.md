# bvinference — CPU ONNX BiomedCLIP inference microservice

Phase E of the search overhaul. Moves BiomedCLIP
(`microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224`) out of every
FastAPI web worker into a dedicated, horizontally scalable CPU service so
the ~500 MB model is loaded once per inference pod instead of once per web
worker process.

## What it does

A small FastAPI app exposing:

- `POST /encode` — body `{"modality": "image"|"text", "inputs": [...]}`.
  - `text`: `inputs` are plain strings.
  - `image`: `inputs` are base64-encoded PNG bytes (decoded to PIL,
    preprocessed exactly like the in-process worker path, run through the
    visual tower).
  - Returns `{"vectors": [[float, ...], ...], "model_id": ..., "dim": 512}`
    with L2-normalised vectors. `model_id` is `biomedclip-v1` for image and
    `biomedclip-text-v1` for text — matching the worker `MODEL_ID`s so the
    vectors land in the same latent space and registry bucket.
- `GET /healthz` — liveness/readiness probe; reports which ONNX sessions
  are loaded.

The ONNX session(s) are lazy-loaded once per process on first request (or
eagerly via `BVP_INFERENCE_EAGER_LOAD=1`).

## Storage isolation

The service receives only decoded pixel arrays / text strings and returns
vectors. It NEVER touches S3 or the patient database. It holds no patient
identifiers and persists nothing.

## ONNX graphs

The runtime image does **not** depend on torch / open_clip. The two ONNX
graphs are produced once, at build time, by `scripts/export_onnx.py`
(installed via the `export` extra). See that script's docstring for the
export procedure and the expected output paths:

- `$BVP_INFERENCE_MODEL_DIR/biomedclip_image.onnx`
- `$BVP_INFERENCE_MODEL_DIR/biomedclip_text.onnx`
- `$BVP_INFERENCE_MODEL_DIR/tokenizer.json` (text tower)

## Local run (once graphs exist)

```sh
uv sync
BVP_INFERENCE_MODEL_DIR=/path/to/onnx uv run uvicorn bvinference.app:app --port 8090
```

## In-process fallback contract

When `BVP_INFERENCE_SVC_URL` is unset on the backend, the backend client
(`bvphoenix.services.inference_client`) returns `None` and callers fall
back to the in-process encoder. Deploying this service is therefore purely
additive — nothing breaks if it is absent.
