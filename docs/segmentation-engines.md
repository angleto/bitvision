# Segmentation engines

The viewer ships three integration points for producing organ / lesion
masks. They share the same on-disk format (raw `uint8` per-voxel,
x-fastest, stored under `segmentations/{series_id}/{label}.bin` in the
derivatives bucket) so a mask produced by any engine is immediately
consumable by the volume renderer's `setSegmentationMask` path.

| Engine             | Mode              | Endpoint                                                | Worker task           | Notes                          |
|--------------------|-------------------|---------------------------------------------------------|-----------------------|--------------------------------|
| TotalSegmentator   | automatic batch   | `POST /api/series/{id}/segmentations/auto`              | `segment_auto`        | 117 ROIs, CPU 5-15 min/scan    |
| MedSAM-2 (SAM-2)   | interactive 2D    | `POST /api/series/{id}/segmentations/interactive/predict` | `medsam_predict_2d`   | per-click, CPU 3-10 s/click    |
| MONAI Label        | external server   | `GET /api/segmentations/monai_label/info`               | (proxy only)          | requires standalone MONAI host |

All three are gated behind the `write:annotations` permission and
share the existing `SegmentationImporter` UI as the surface for
managing the resulting labels.

## TotalSegmentator (`seg` extra)

Apache-2.0, 117 anatomical structures pre-trained on multi-source CT.
Model weights are fetched on first use and cached under
`~/.totalsegmentator`.

**Install on the worker host:**

```bash
cd workers && uv sync --extra seg
```

The first run downloads ~600 MB of model weights — keep the worker
host's `$HOME` writable.

**Trigger a job from the viewer:**

The "Auto-segment (TotalSegmentator)" card in the segmentation panel
exposes three preset buttons:

- **Fegato** — `roi_subset=["liver"]`, fastest path.
- **Addome** — liver + kidneys + spleen + pancreas.
- **Tutto** — the full default subset (16 ROIs covering abdomen + thorax).

The frontend polls `GET /api/series/{id}/segmentations` every 5s
until all expected ROIs appear, with a hard 20 min client cap; longer
than that and it stops polling, the worker continues in the
background and the labels show up on the next manual refresh.

CPU is the only supported device for now. The `fast` flag on the
worker forces the 3 mm model — required to keep CPU runtimes
reasonable; switch to GPU + the 1.5 mm model if you have NVIDIA
hardware available.

## MedSAM-2 / SAM-2 (manual install)

Apache-2.0, weight checkpoint configurable via `BVP_MEDSAM_CKPT` and
`BVP_MEDSAM_CFG` env vars on the worker host. Defaults to vanilla
SAM-2 tiny — works for high-contrast structures (liver, vessels) but
loses precision on muscle / fat / soft-tissue boundaries; replace with
a MedSAM-fine-tuned checkpoint for production use.

**Install** (kept manual because there's no single canonical PyPI
release — different forks publish under `sam2`, `segment-anything-2`,
`medsam`):

```bash
cd workers
uv pip install --no-deps git+https://github.com/facebookresearch/sam2.git
```

The worker task imports `sam2` lazily; if the package is missing the
endpoint returns 502 with a clear "not installed" hint instead of
crashing.

**Wire format:** the frontend posts a list of click coordinates in
slice voxel space plus the axis (0/1/2) and slice index. The backend
enqueues an arq job, awaits the result with a 60 s timeout, and
returns the 2D mask base64-encoded in the response. When `label` is
also supplied, the 2D mask is embedded into a zero-padded full-volume
binary and persisted under the standard segmentations prefix so the
viewer can apply it via `setSegmentationMask`.

**Latency note:** the first call per worker process pays the model-
load tax (~5-10 s). Subsequent calls are inference-only (~3-5 s on
CPU for a 512x512 slice). Consider running the worker with a longer
job timeout if you expect bursts of clicks.

## MONAI Label (external server)

The platform doesn't bundle a MONAI Label server — point at one
running externally via `BVP_MONAI_LABEL_URL`. The backend proxy
exposes `GET /info` so the frontend can list the upstream models;
extend with additional proxied routes (`/next_sample`, `/infer`,
`/train`) as workflows demand.

**Run a MONAI Label server alongside the viewer:**

```bash
pip install monailabel
monailabel apps --download --name radiology --output ~/monailabel-apps
monailabel start_server --app ~/monailabel-apps/radiology --studies /path/to/dicom
```

Then on the bvphoenix backend host:

```bash
export BVP_MONAI_LABEL_URL=http://localhost:8000
```

Restart the backend; the proxy is now live. The frontend won't show
MONAI Label affordances unless the proxy returns a 200 from `/info`.
