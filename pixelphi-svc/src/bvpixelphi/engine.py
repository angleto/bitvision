"""PaddleOCR PP-OCRv5 ONNX text-region detector for burned-in-pixel redaction.

This is the "hard case" tier behind the backend's
``services.pixel_phi_engine.HttpPixelPhiEngine``. The cheap Tesseract tier in
``services.pixel_deid.clean_pixel_data`` handles the common case; when it finds
NO text on a high-risk frame (dense overlay / low contrast / non-Latin script,
the case Tesseract misses) this service is consulted for additional regions to
mask.

It returns text *bounding boxes*, never a redacted image: the backend blacks the
boxes out. Over-redaction is the policy. The backend masks every returned box
without trusting any per-box "is this clinical?" judgement here, and fails
closed to whole-frame masking if this service is unreachable or returns nothing
on a frame the backend already considers suspicious.

CPU-only via onnxruntime (ARM/x86 manylinux wheels). The detector graph is the
Apache-2.0 PP-OCRv5 mobile detection model exported to ONNX at build time and
synced from object storage at deploy: weights are never baked into the image.
"""

from __future__ import annotations

import logging
import os

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger("bvpixelphi")

MODEL_ID = "pixelphi-ppocrv5-det-v1"

# ImageNet-style normalisation used by PaddleOCR detection preprocessing.
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

Box = tuple[int, int, int, int, float]  # x, y, w, h, conf


class DetectorEngine:
    """Lazy-loading ONNX DBNet detector. One session per process, shared by all
    requests. Absent model → ``detect`` returns no boxes (and ``model_loaded``
    is False) so the backend fails closed to over-redaction."""

    def __init__(self, settings):
        self._settings = settings
        self._session = None

    @property
    def model_path(self) -> str:
        return os.path.join(self._settings.model_dir, self._settings.detector_model)

    @property
    def model_loaded(self) -> bool:
        return os.path.exists(self.model_path)

    def _session_or_load(self):
        if self._session is not None:
            return self._session
        import onnxruntime as ort  # heavy; runtime-only import

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self._settings.onnx_intra_op_threads
        opts.inter_op_num_threads = self._settings.onnx_inter_op_threads
        self._session = ort.InferenceSession(
            self.model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        logger.info("loaded pixelphi detector graph from %s", self.model_path)
        return self._session

    def warmup(self) -> None:
        if self.model_loaded:
            self._session_or_load()

    # -- preprocessing -------------------------------------------------------
    def _preprocess(self, img: Image.Image):
        rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
        h0, w0 = rgb.shape[:2]
        # Downscale very large frames before detection (bounds CPU/RAM); boxes
        # are mapped back to original coordinates via the ratios below.
        max_side = float(self._settings.max_image_side)
        longest = float(max(h0, w0))
        pre_scale = max_side / longest if longest > max_side else 1.0
        # DBNet needs H and W as multiples of 32.
        h1 = max(32, int(round(h0 * pre_scale / 32.0)) * 32)
        w1 = max(32, int(round(w0 * pre_scale / 32.0)) * 32)
        resized = cv2.resize(rgb, (w1, h1), interpolation=cv2.INTER_LINEAR)
        norm = (resized / 255.0 - _MEAN) / _STD
        chw = np.transpose(norm, (2, 0, 1))[np.newaxis, ...].astype(np.float32)
        return chw, (w0 / float(w1), h0 / float(h1))

    # -- post-processing -----------------------------------------------------
    def _boxes_from_prob(self, prob: np.ndarray, ratios) -> list[Box]:
        s = self._settings
        ratio_w, ratio_h = ratios
        ph, pw = prob.shape[:2]
        bitmap = (prob > s.det_db_thresh).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes: list[Box] = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if w < 3 or h < 3:
                continue
            # Box score: mean probability inside the bounding rect (DBNet's
            # fast box-score). Drop low-confidence regions.
            score = float(prob[y : y + h, x : x + w].mean())
            if score < s.det_db_box_thresh:
                continue
            # Dilate outward (a proportional approximation of DBNet's pyclipper
            # "unclip") so glyph extents are not clipped. Exact box tightness is
            # not critical: the backend over-redacts the returned region.
            pad_x = int(round(w * (s.det_db_unclip_ratio - 1.0) / 2.0))
            pad_y = int(round(h * (s.det_db_unclip_ratio - 1.0) / 2.0))
            x0 = max(0, x - pad_x)
            y0 = max(0, y - pad_y)
            x1 = min(pw, x + w + pad_x)
            y1 = min(ph, y + h + pad_y)
            ox = int(round(x0 * ratio_w))
            oy = int(round(y0 * ratio_h))
            ow = int(round((x1 - x0) * ratio_w))
            oh = int(round((y1 - y0) * ratio_h))
            if ow <= 0 or oh <= 0:
                continue
            boxes.append((ox, oy, ow, oh, score))
        return boxes

    def detect(self, img: Image.Image) -> list[Box]:
        if not self.model_loaded:
            logger.warning(
                "pixelphi detector model missing at %s; returning no boxes "
                "(backend fails closed to over-redaction)",
                self.model_path,
            )
            return []
        session = self._session_or_load()
        chw, ratios = self._preprocess(img)
        input_name = session.get_inputs()[0].name
        out = session.run(None, {input_name: chw})[0]
        # PP-OCRv5 detection output is (1, 1, H, W) probability map.
        prob = np.asarray(out)[0, 0]
        return self._boxes_from_prob(prob, ratios)
