"""In-house DICOM PS3.15 Basic Application Confidentiality Profile engine.

Table-driven (``profile_table``) + action executor (``actions``) + project
operators (``operators``: salted UID remap, per-patient date shift) + coded
provenance (``provenance``) + a fail-closed verification pass (``verify``).
Public entry point: :func:`bvphoenix.services.deid.engine.scrub_dicom_bytes`,
which the ``services.deidentify`` facade delegates to.
"""

from __future__ import annotations

from bvphoenix.services.deid.engine import scrub_dicom_bytes

__all__ = ["scrub_dicom_bytes"]
