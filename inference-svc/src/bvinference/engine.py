"""ONNX inference engine for the BiomedCLIP dual encoder.

Two independent ONNX graphs (visual tower, text tower) exported from the
open_clip BiomedCLIP model by ``scripts/export_onnx.py``. This module
loads them on demand via onnxruntime (CPU), reproduces the open_clip
preprocessing / tokenization in pure numpy, runs inference, and returns
L2-normalised 512-dim vectors.

Why reproduce preprocessing here instead of in the graph: keeping the
graph as the pure encoder (and doing deterministic numpy resize + CLIP
normalisation + tokenisation outside it) means the .onnx files stay small,
portable, and trivially comparable to the torch reference the workers use.
The numpy preprocessing mirrors open_clip ``preprocess_val`` for the
ViT-B/16-224 BiomedCLIP config: resize shortest side to 224 (bicubic),
center-crop 224x224, scale to [0,1], normalise with the OpenAI CLIP
mean/std. The torch path uses exactly these constants, so vectors produced
here live in the same latent space as the in-process worker path.
"""

from __future__ import annotations

import json
import os
import threading

import numpy as np
import onnxruntime as ort
from PIL import Image

from bvinference.config import Settings

# Model identity. Must match workers/.../embed_series.py + embed_text.py so
# the registry buckets line up. Image tower → biomedclip-v1, text tower →
# biomedclip-text-v1; both emit the same 512-d space.
IMAGE_MODEL_ID = "biomedclip-v1"
TEXT_MODEL_ID = "biomedclip-text-v1"
EMBEDDING_DIM = 512

# Input geometry for the BiomedCLIP ViT-B/16-224 config.
IMAGE_SIZE = 224

# OpenAI CLIP normalisation constants (open_clip default for this config).
_CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

# PubMedBERT context length used by BiomedCLIP's text tower.
_TEXT_CONTEXT_LENGTH = 256


def _l2_normalise(mat: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation; rows with zero norm are left as zeros."""
    norms = np.linalg.norm(mat, axis=-1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return mat / norms


def _resize_shortest(img: Image.Image, size: int) -> Image.Image:
    """Resize so the shorter side == ``size`` (bicubic), preserving ratio."""
    w, h = img.size
    if w <= h:
        new_w = size
        new_h = max(size, round(h * size / w))
    else:
        new_h = size
        new_w = max(size, round(w * size / h))
    return img.resize((new_w, new_h), Image.BICUBIC)


def _center_crop(img: Image.Image, size: int) -> Image.Image:
    w, h = img.size
    left = (w - size) // 2
    top = (h - size) // 2
    return img.crop((left, top, left + size, top + size))


def _preprocess_image(img: Image.Image) -> np.ndarray:
    """open_clip ``preprocess_val`` equivalent → CHW float32 tensor."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = _resize_shortest(img, IMAGE_SIZE)
    img = _center_crop(img, IMAGE_SIZE)
    arr = np.asarray(img, dtype=np.float32) / 255.0  # HWC, [0,1]
    arr = (arr - _CLIP_MEAN) / _CLIP_STD
    return np.transpose(arr, (2, 0, 1))  # CHW


class _SimpleWordPieceTokenizer:
    """Minimal WordPiece tokenizer reading a HF ``tokenizer.json``.

    BiomedCLIP's text tower uses a PubMedBERT WordPiece vocabulary. We
    persist the HF fast-tokenizer JSON at export time and reimplement the
    encode path (basic + wordpiece) in pure Python so the runtime image
    avoids a transformers/torch dependency. This is intentionally a small
    subset: lowercase basic tokenisation + greedy longest-match wordpiece,
    which is exactly what BERT-family uncased tokenizers do for clinical
    free text.
    """

    def __init__(self, tokenizer_json_path: str) -> None:
        with open(tokenizer_json_path, encoding="utf-8") as fh:
            spec = json.load(fh)
        model = spec.get("model", {})
        self.vocab: dict[str, int] = model.get("vocab", {})
        self.unk_token = model.get("unk_token", "[UNK]")
        self.continuing_prefix = model.get("continuing_subword_prefix", "##")
        # Special-token ids; fall back to BERT conventions if absent.
        self.cls_id = self.vocab.get("[CLS]", 101)
        self.sep_id = self.vocab.get("[SEP]", 102)
        self.pad_id = self.vocab.get("[PAD]", 0)
        self.unk_id = self.vocab.get(self.unk_token, 100)
        lowercase = True
        for norm in _iter_normalizers(spec.get("normalizer")):
            if norm.get("type") == "Lowercase":
                lowercase = True
            if norm.get("type") == "BertNormalizer":
                lowercase = norm.get("lowercase", True)
        self.lowercase = lowercase

    def _basic_tokens(self, text: str) -> list[str]:
        if self.lowercase:
            text = text.lower()
        out: list[str] = []
        token: list[str] = []
        for ch in text:
            if ch.isspace():
                if token:
                    out.append("".join(token))
                    token = []
            elif not ch.isalnum():
                # Punctuation splits into its own token (BERT behaviour).
                if token:
                    out.append("".join(token))
                    token = []
                out.append(ch)
            else:
                token.append(ch)
        if token:
            out.append("".join(token))
        return out

    def _wordpiece(self, word: str) -> list[int]:
        if word in self.vocab:
            return [self.vocab[word]]
        ids: list[int] = []
        start = 0
        n = len(word)
        while start < n:
            end = n
            cur_id: int | None = None
            while start < end:
                piece = word[start:end]
                if start > 0:
                    piece = self.continuing_prefix + piece
                if piece in self.vocab:
                    cur_id = self.vocab[piece]
                    break
                end -= 1
            if cur_id is None:
                return [self.unk_id]
            ids.append(cur_id)
            start = end
        return ids

    def encode(self, text: str, context_length: int) -> np.ndarray:
        ids = [self.cls_id]
        for word in self._basic_tokens(text):
            ids.extend(self._wordpiece(word))
        ids.append(self.sep_id)
        ids = ids[:context_length]
        if len(ids) < context_length:
            ids = ids + [self.pad_id] * (context_length - len(ids))
        return np.asarray(ids, dtype=np.int64)


def _iter_normalizers(normalizer: dict | None):
    """Yield each normalizer spec, flattening a ``Sequence`` wrapper."""
    if not normalizer:
        return
    if normalizer.get("type") == "Sequence":
        yield from normalizer.get("normalizers", [])
    else:
        yield normalizer


class InferenceEngine:
    """Thread-safe lazy-loading holder for the two ONNX sessions.

    Sessions and the tokenizer are constructed on first use (or eagerly at
    startup) and reused for the process lifetime. ``encode_image`` /
    ``encode_text`` are pure functions of their inputs — no patient state,
    no I/O beyond the local model files.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._image_session: ort.InferenceSession | None = None
        self._text_session: ort.InferenceSession | None = None
        self._tokenizer: _SimpleWordPieceTokenizer | None = None

    # -- session construction ------------------------------------------
    def _session_options(self) -> ort.SessionOptions:
        opts = ort.SessionOptions()
        if self._settings.onnx_intra_op_threads > 0:
            opts.intra_op_num_threads = self._settings.onnx_intra_op_threads
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return opts

    def _path(self, name: str) -> str:
        return os.path.join(self._settings.model_dir, name)

    def _ensure_image(self) -> ort.InferenceSession:
        if self._image_session is None:
            with self._lock:
                if self._image_session is None:
                    self._image_session = ort.InferenceSession(
                        self._path(self._settings.image_onnx_name),
                        sess_options=self._session_options(),
                        providers=["CPUExecutionProvider"],
                    )
        return self._image_session

    def _ensure_text(self) -> tuple[ort.InferenceSession, _SimpleWordPieceTokenizer]:
        if self._text_session is None or self._tokenizer is None:
            with self._lock:
                if self._text_session is None:
                    self._text_session = ort.InferenceSession(
                        self._path(self._settings.text_onnx_name),
                        sess_options=self._session_options(),
                        providers=["CPUExecutionProvider"],
                    )
                if self._tokenizer is None:
                    self._tokenizer = _SimpleWordPieceTokenizer(
                        self._path(self._settings.tokenizer_name)
                    )
        assert self._text_session is not None
        assert self._tokenizer is not None
        return self._text_session, self._tokenizer

    def warmup(self) -> None:
        """Eagerly construct both sessions (used when eager_load is set)."""
        self._ensure_image()
        self._ensure_text()

    @property
    def loaded(self) -> dict[str, bool]:
        """Which sessions are currently resident (for /healthz)."""
        return {
            "image": self._image_session is not None,
            "text": self._text_session is not None,
        }

    # -- inference ------------------------------------------------------
    def encode_image(self, images: list[Image.Image]) -> np.ndarray:
        """Run the visual tower on a batch of PIL images → (N, 512)."""
        session = self._ensure_image()
        batch = np.stack([_preprocess_image(im) for im in images]).astype(np.float32)
        input_name = session.get_inputs()[0].name
        (out,) = session.run(None, {input_name: batch})
        return _l2_normalise(np.asarray(out, dtype=np.float32))

    def encode_text(self, texts: list[str]) -> np.ndarray:
        """Run the text tower on a batch of strings → (N, 512)."""
        session, tokenizer = self._ensure_text()
        tokens = np.stack([tokenizer.encode(t, _TEXT_CONTEXT_LENGTH) for t in texts]).astype(
            np.int64
        )
        input_name = session.get_inputs()[0].name
        (out,) = session.run(None, {input_name: tokens})
        return _l2_normalise(np.asarray(out, dtype=np.float32))
