"""Export BiomedCLIP's two towers to ONNX for CPU inference.

Run ONCE at image-build time (not at runtime). Requires the ``export``
extra::

    uv sync --extra export
    uv run python scripts/export_onnx.py --out-dir ./models

What it does
------------
1. Loads ``microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224`` via
   open_clip (``hf-hub:...``), exactly as the in-process worker path does
   (workers/.../embed_series.py), so the exported graphs are the same
   weights the rest of the system already uses.
2. Wraps each tower in a tiny ``nn.Module`` that calls ``encode_image`` /
   ``encode_text`` and returns the raw (un-normalised) features. L2
   normalisation is applied by the *service* at inference time
   (engine._l2_normalise), so the graph stays a pure encoder.
3. Exports both wrappers with ``torch.onnx.export`` to:
     - ``<out_dir>/biomedclip_image.onnx``  (input: pixel_values, NCHW fp32)
     - ``<out_dir>/biomedclip_text.onnx``   (input: input_ids,  N x L int64)
   Batch and (for text) sequence dims are marked dynamic.
4. Persists the text tokenizer as a HuggingFace ``tokenizer.json`` so the
   runtime can tokenise without torch/transformers. open_clip's
   HFTokenizer wraps a ``transformers`` fast tokenizer; we call
   ``save_pretrained`` / ``tokenizer.save`` to dump the JSON. If that
   handle is not reachable on the installed open_clip version the script
   fails loudly — the runtime tokenizer (engine._SimpleWordPieceTokenizer)
   depends on this file existing.

Validation
----------
This script is the *only* place torch + open_clip are needed. It is not
imported by the service. It cannot be exercised in CI without the ~500 MB
model download, so it is run in the Docker build (see
infra/dockerfiles/inference-svc.Dockerfile) where the download is cached.

The numbers below (image size 224, context length 256, embedding dim 512)
are fixed by the BiomedCLIP ViT-B/16-224 config and must stay in sync with
``bvinference.engine``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

MODEL_HUB_ID = "hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224"
IMAGE_SIZE = 224
CONTEXT_LENGTH = 256
EMBEDDING_DIM = 512


def _build_models():
    """Load BiomedCLIP and return (image_wrapper, text_wrapper, tokenizer)."""
    import open_clip
    from torch import nn

    model, _preprocess_train, _preprocess_val = open_clip.create_model_and_transforms(MODEL_HUB_ID)
    tokenizer = open_clip.get_tokenizer(MODEL_HUB_ID)
    model.eval()

    class ImageTower(nn.Module):
        def __init__(self, clip: nn.Module) -> None:
            super().__init__()
            self.clip = clip

        def forward(self, pixel_values):
            return self.clip.encode_image(pixel_values)

    class TextTower(nn.Module):
        def __init__(self, clip: nn.Module) -> None:
            super().__init__()
            self.clip = clip

        def forward(self, input_ids):
            return self.clip.encode_text(input_ids)

    return ImageTower(model).eval(), TextTower(model).eval(), tokenizer


def _export_image(image_tower, out_path: Path) -> None:
    import torch

    dummy = torch.randn(1, 3, IMAGE_SIZE, IMAGE_SIZE, dtype=torch.float32)
    with torch.no_grad():
        torch.onnx.export(
            image_tower,
            dummy,
            str(out_path),
            input_names=["pixel_values"],
            output_names=["image_features"],
            dynamic_axes={
                "pixel_values": {0: "batch"},
                "image_features": {0: "batch"},
            },
            opset_version=17,
            do_constant_folding=True,
        )


def _export_text(text_tower, tokenizer, out_path: Path) -> None:
    import torch

    # A real tokenised sample makes export trace the true int64 path.
    sample = tokenizer(["chest ct with pulmonary nodule"])
    if not isinstance(sample, torch.Tensor):
        sample = torch.as_tensor(sample)
    sample = sample[:, :CONTEXT_LENGTH].to(torch.int64)
    with torch.no_grad():
        torch.onnx.export(
            text_tower,
            sample,
            str(out_path),
            input_names=["input_ids"],
            output_names=["text_features"],
            dynamic_axes={
                "input_ids": {0: "batch", 1: "sequence"},
                "text_features": {0: "batch"},
            },
            opset_version=17,
            do_constant_folding=True,
        )


def _save_tokenizer(tokenizer, out_path: Path) -> None:
    """Persist the HF fast-tokenizer JSON consumed by the runtime.

    open_clip's HFTokenizer keeps the underlying ``transformers`` tokenizer
    on ``.tokenizer``. We dump the fast-tokenizer JSON so the service can
    re-tokenise without torch/transformers.
    """
    hf_tok = getattr(tokenizer, "tokenizer", None)
    if hf_tok is None:
        raise RuntimeError(
            "could not reach the underlying HF tokenizer on the open_clip "
            "tokenizer; cannot persist tokenizer.json. Check the open_clip "
            "version exposes `.tokenizer`."
        )
    # transformers fast tokenizers expose the rust tokenizer via
    # `_tokenizer` with a `.save(path)`; fall back to `save_pretrained`.
    backend = getattr(hf_tok, "_tokenizer", None)
    if backend is not None and hasattr(backend, "save"):
        backend.save(str(out_path))
        return
    if hasattr(hf_tok, "save_pretrained"):
        tmp_dir = out_path.parent / "_hf_tokenizer"
        hf_tok.save_pretrained(str(tmp_dir))
        src = tmp_dir / "tokenizer.json"
        if not src.exists():
            raise RuntimeError(
                f"save_pretrained did not produce {src}; the tokenizer may "
                "be slow-only. Install `tokenizers` so a fast tokenizer is "
                "available."
            )
        os.replace(src, out_path)
        return
    raise RuntimeError("no usable save path on the HF tokenizer")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export BiomedCLIP towers to ONNX.")
    parser.add_argument(
        "--out-dir",
        default=os.environ.get("BVP_INFERENCE_MODEL_DIR", "./models"),
        help="Directory to write the .onnx graphs + tokenizer.json into.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[export] loading {MODEL_HUB_ID} ...", flush=True)
    image_tower, text_tower, tokenizer = _build_models()

    image_path = out_dir / "biomedclip_image.onnx"
    text_path = out_dir / "biomedclip_text.onnx"
    tokenizer_path = out_dir / "tokenizer.json"

    print(f"[export] image tower -> {image_path}", flush=True)
    _export_image(image_tower, image_path)

    print(f"[export] text tower  -> {text_path}", flush=True)
    _export_text(text_tower, tokenizer, text_path)

    print(f"[export] tokenizer   -> {tokenizer_path}", flush=True)
    _save_tokenizer(tokenizer, tokenizer_path)

    meta = {
        "model_hub_id": MODEL_HUB_ID,
        "image_size": IMAGE_SIZE,
        "context_length": CONTEXT_LENGTH,
        "embedding_dim": EMBEDDING_DIM,
        "image_model_id": "biomedclip-v1",
        "text_model_id": "biomedclip-text-v1",
    }
    (out_dir / "export_meta.json").write_text(json.dumps(meta, indent=2))
    print("[export] done.", flush=True)


if __name__ == "__main__":
    main()
