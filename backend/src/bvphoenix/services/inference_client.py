"""Client for the out-of-process BiomedCLIP inference microservice.

Phase E of the search overhaul moves BiomedCLIP out of every FastAPI web
worker into a dedicated CPU ONNX service (see ``inference-svc/``). This
module is the backend-side client.

In-process fallback contract
----------------------------
The service is *optional*. Its base URL is read from the environment
variable ``BVP_INFERENCE_SVC_URL`` at call time:

* If the variable is **unset** (or blank), both helpers return ``None``
  immediately, without any network call. Callers MUST treat ``None`` as
  "service not available, fall back to the in-process encoder". This keeps
  the system working unchanged when the inference service is not deployed.
* If the variable is set but the call fails for any reason (timeout,
  connection error, non-2xx, malformed body), the helper logs a warning
  and returns ``None`` — again triggering the caller's in-process
  fallback. A degraded inference service must never take search down.

The service is storage-isolated: it receives only text strings / decoded
image bytes and returns vectors, so this client never sends patient
identifiers, S3 keys, or DB rows.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Env var holding the in-cluster base URL of the inference service, e.g.
# ``http://bvphoenix-inference-svc:80``. Unset == feature off.
_ENV_URL = "BVP_INFERENCE_SVC_URL"

# Short timeout: the encoder runs on CPU but a single text/image encode is
# sub-second to a couple of seconds. We would rather fall back to the
# in-process path than block a search request waiting on a slow pod.
_TIMEOUT = httpx.Timeout(connect=2.0, read=8.0, write=2.0, pool=2.0)

# Expected output dimensionality of the BiomedCLIP towers. Used only as a
# light sanity check on the response.
_EXPECTED_DIM = 512


def _base_url() -> str | None:
    """Return the configured base URL, or None when the feature is off."""
    raw = os.environ.get(_ENV_URL, "").strip()
    return raw.rstrip("/") or None


async def _encode_one(modality: str, value: str) -> list[float] | None:
    """POST a single input to /encode and return its vector, or None.

    Returns None when the service is unconfigured or any error occurs, so
    every caller can uniformly fall back to in-process encoding.
    """
    base = _base_url()
    if base is None:
        return None

    payload = {"modality": modality, "inputs": [value]}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{base}/encode", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError covers a non-JSON body from resp.json().
        logger.warning("inference service call failed (%s); falling back: %s", modality, exc)
        return None

    vectors = data.get("vectors") if isinstance(data, dict) else None
    if not vectors or not isinstance(vectors, list):
        logger.warning("inference service returned no vectors (%s); falling back", modality)
        return None

    vec = vectors[0]
    if not isinstance(vec, list) or len(vec) != _EXPECTED_DIM:
        logger.warning(
            "inference service returned unexpected vector shape (%s); falling back",
            modality,
        )
        return None

    return [float(x) for x in vec]


async def encode_text(q: str) -> list[float] | None:
    """Encode a text query into a 512-dim BiomedCLIP vector via the service.

    Returns None when ``BVP_INFERENCE_SVC_URL`` is unset or the call fails;
    the caller then encodes in-process.
    """
    return await _encode_one("text", q)


async def encode_image_b64(png_b64: str) -> list[float] | None:
    """Encode a base64 PNG image into a 512-dim BiomedCLIP vector.

    ``png_b64`` is the base64-encoded PNG bytes of the image to encode.
    Returns None when ``BVP_INFERENCE_SVC_URL`` is unset or the call fails;
    the caller then encodes in-process.
    """
    return await _encode_one("image", png_b64)
