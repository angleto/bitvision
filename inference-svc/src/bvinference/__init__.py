"""bvinference — CPU ONNX BiomedCLIP inference microservice.

Out-of-process inference for the BiomedCLIP dual encoder so the model is
loaded once per inference pod instead of once per FastAPI web worker.

The service is intentionally stateless and storage-isolated: it accepts
decoded pixel arrays / text strings and returns L2-normalised 512-dim
vectors. It never touches S3 or the patient database.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.0.1"
