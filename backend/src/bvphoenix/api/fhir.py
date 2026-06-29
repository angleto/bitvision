"""FHIR R4 conformance surface: ``GET /api/fhir/metadata``.

The standard FHIR "capabilities" interaction. It returns a
``CapabilityStatement`` advertising that bitvision speaks FHIR R4 — it
produces export Bundles (Patient / ImagingStudy / DiagnosticReport /
DocumentReference) and serves imaging over DICOMweb. PHI-free by
construction (pure capability metadata), so it is public and unauthenticated
like the rest of the standards discovery surface.

It is the falsifiable proof behind the "open standard" claim: a client GETs
this endpoint and validates a real CapabilityStatement, and the CI gate
(tests/test_fhir_capability.py) round-trips it through the FHIR R4B model so
the claim can never silently rot.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Response

from bvphoenix.config import get_settings
from bvphoenix.services import fhir_export as fx

router = APIRouter(prefix="/fhir", tags=["fhir"])

_FHIR_JSON = "application/fhir+json"


@router.get("/metadata")
async def fhir_capabilities() -> Response:
    """FHIR R4 CapabilityStatement (the ``/metadata`` interaction)."""
    settings = get_settings()
    origin = settings.public_frontend_url.rstrip("/")
    statement = fx.capability_statement(
        software_version=settings.app_version or "dev",
        fhir_base_url=f"{origin}/api/fhir",
        dicomweb_base_url=fx.dicomweb_base_url(settings.public_frontend_url),
        date_iso=datetime.now(UTC).date().isoformat(),
    )
    return Response(content=json.dumps(statement), media_type=_FHIR_JSON)
