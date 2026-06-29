"""Conformance gate: FHIR CapabilityStatement + DICOMweb route surface.

Makes the "open standard" claim falsifiable in the PR lint+test gate (no DB):

* the ``GET /api/fhir/metadata`` CapabilityStatement round-trips through the
  real ``fhir.resources`` R4B model, and advertises exactly the resource types
  the exporter emits (single source — they cannot drift);
* every DICOMweb transaction this repo's conformance statement
  (docs/conformance.md) claims is actually a registered route — so the
  statement can never over-claim an endpoint that does not exist.
"""

from __future__ import annotations

import json

from fhir.resources.R4B.capabilitystatement import CapabilityStatement

from bvphoenix.api.fhir import fhir_capabilities
from bvphoenix.main import app
from bvphoenix.services import fhir_export as fx

# The DICOMweb transactions docs/conformance.md advertises. Kept here as the
# machine-checkable twin of that table: add/remove an endpoint and this list
# (and the doc) must move together, or the gate fails.
_EXPECTED_DICOMWEB_ROUTES = {
    "/api/dicom/studies",
    "/api/dicom/studies/{study_uid}/series",
    "/api/dicom/studies/{study_uid}/series/{series_uid}/instances",
    "/api/dicom/studies/{study_uid}/instances",
    "/api/dicom/series",
    "/api/dicom/instances",
    "/api/dicom/studies/{study_uid}",
    "/api/dicom/studies/{study_uid}/series/{series_uid}",
    "/api/dicom/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}",
    "/api/dicom/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/{frame_list}",
    "/api/dicom/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/bulkdata/{tag}",
    "/api/dicom/studies/{study_uid}/metadata",
    "/api/dicom/studies/{study_uid}/series/{series_uid}/metadata",
    "/api/dicom/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata",
}


def _build() -> dict:
    return fx.capability_statement(
        software_version="9.9.9",
        fhir_base_url="https://h/api/fhir",
        dicomweb_base_url="https://h/api/dicom",
        date_iso="2026-06-29",
    )


def test_capability_statement_validates_against_r4b() -> None:
    CapabilityStatement.model_validate(_build())


def test_capability_statement_advertises_exactly_exported_types() -> None:
    cs = _build()
    declared = {r["type"] for r in cs["rest"][0]["resource"]}
    assert declared == set(fx.EXPORTED_RESOURCE_TYPES)
    assert cs["fhirVersion"] == "4.0.1"
    assert "application/fhir+json" in cs["format"]
    # No over-claimed REST interactions (we are not a FHIR REST server).
    assert all("interaction" not in r for r in cs["rest"][0]["resource"])


async def test_metadata_endpoint_serves_valid_capability_statement() -> None:
    resp = await fhir_capabilities()
    assert resp.media_type == "application/fhir+json"
    CapabilityStatement.model_validate(json.loads(resp.body))


def test_fhir_metadata_route_registered() -> None:
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/fhir/metadata" in paths


def test_advertised_dicomweb_transactions_are_real_routes() -> None:
    paths = {getattr(r, "path", "") for r in app.routes}
    missing = _EXPECTED_DICOMWEB_ROUTES - paths
    assert not missing, f"conformance claims unregistered DICOMweb routes: {missing}"
