"""Every terminology system the platform emits must be on a frozen list.

``DESIGN.md`` §2 carves out one exception to "no ontologies": standard
codes may cross the export boundary — FHIR and DICOM SR — and nowhere
else, drawn from three systems only (SNOMED CT, LOINC, UCUM) plus the
DICOM-native ones the SR/ImagingStudy shapes require, and our own
identifier namespace.

The rule is worth exactly as much as its enforcement. This module is
the enforcement: it walks what the exporters actually produce and fails
on any ``system`` URI or coding-scheme designator nobody signed off.
Widening the list is a deliberate act with a diff attached, which is the
point — adding ICD / ATC / RxNorm should be a decision, not a drive-by.

Deliberately free of the ``fhir.resources`` test-only dependency, so the
guard runs even where the R4B conformance round-trip cannot.
"""

from __future__ import annotations

from typing import Any

from bvphoenix.services import dicom_sr_export as sr
from bvphoenix.services import fhir_export as fx

# ---------------------------------------------------------------- FHIR

#: Absolute ``system`` URIs the FHIR export may emit.
ALLOWED_FHIR_SYSTEMS: frozenset[str] = frozenset(
    {
        # Standard external code systems (the §2 three, minus LOINC which
        # has no emitter yet — add it here when one lands).
        "http://snomed.info/sct",
        "http://loinc.org",
        "http://unitsofmeasure.org",
        # DICOM-native, required by the ImagingStudy / SR shapes.
        fx.DCM_SYSTEM,
        fx.DICOM_UID_SYSTEM,
        # HL7 terminology needed to make a resource well-formed.
        fx.ENDPOINT_CONNECTION_TYPE_SYSTEM,
        # Our own identifier namespace — not an ontology.
        fx.IDENTIFIER_SYSTEM_BASE,
    }
)


def _systems(node: Any) -> set[str]:
    """Every ``system`` value anywhere in a FHIR resource tree."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "system" and isinstance(value, str):
                found.add(value)
            else:
                found |= _systems(value)
    elif isinstance(node, list):
        for item in node:
            found |= _systems(item)
    return found


def _is_allowed(system: str) -> bool:
    # Identifier systems are namespaced per type
    # (``…/identifier/{type}``); the base is what we vet.
    return system in ALLOWED_FHIR_SYSTEMS or system.startswith(f"{fx.IDENTIFIER_SYSTEM_BASE}/")


def _sample_resources() -> list[dict[str, Any]]:
    """One of every resource the exporter can build, populated enough
    that the optional coded branches are actually taken."""
    return [
        fx.patient_resource(
            patient_id="p1",
            display_name="Test Patient",
            birth_date="1970-01-01",
            sex="F",
            identifiers=[{"type": "codice_fiscale", "value": "XXX"}, {"value": "bare"}],
        ),
        fx.endpoint_resource(endpoint_id="e1", address="https://example.test/dicom"),
        fx.imaging_study_resource(
            study_id="s1",
            study_instance_uid="1.2.3",
            modalities=["CT", "MR"],
            started="2026-01-01",
            description="Chest",
            patient_ref="urn:uuid:p1",
            endpoint_ref="urn:uuid:e1",
        ),
        fx.diagnostic_report_resource(
            report_id="r1",
            authority="original",
            status="final",
            author_kind="human",
            title="Referto",
            patient_ref="urn:uuid:p1",
        ),
        fx.document_reference_resource(
            doc_id="d1",
            doc_kind="referto",
            title="Referto.pdf",
            content_type="application/pdf",
            patient_ref="urn:uuid:p1",
        ),
        fx.capability_statement(
            software_version="0.0.0-test",
            fhir_base_url="https://example.test/api/fhir",
            dicomweb_base_url="https://example.test/api/dicom",
            date_iso="2026-01-01T00:00:00Z",
        ),
    ]


def test_fhir_builders_emit_only_allowlisted_systems() -> None:
    for resource in _sample_resources():
        for system in _systems(resource):
            assert _is_allowed(system), (
                f"{resource.get('resourceType')} emits unvetted code system "
                f"{system!r}. DESIGN.md §2 binds terminology to a frozen list; "
                "widen ALLOWED_FHIR_SYSTEMS deliberately, with a source and a "
                "verification date, or emit code.text only."
            )


def test_collection_bundle_emits_only_allowlisted_systems() -> None:
    # CapabilityStatement is served at /metadata, never bundled — and
    # ``to_collection_bundle`` requires an ``id`` on every member.
    members = [r for r in _sample_resources() if r.get("resourceType") != "CapabilityStatement"]
    bundle = fx.to_collection_bundle(members, timestamp="2026-01-01T00:00:00Z")
    unvetted = {s for s in _systems(bundle) if not _is_allowed(s)}
    assert not unvetted, f"bundle emits unvetted code systems: {sorted(unvetted)}"


def test_identifier_systems_stay_under_our_namespace() -> None:
    """Our own identifiers must not squat on somebody else's URI."""
    res = fx.patient_resource(
        patient_id="p1", identifiers=[{"type": "codice_fiscale", "value": "XXX"}]
    )
    for ident in res.get("identifier") or []:
        assert ident["system"].startswith(fx.IDENTIFIER_SYSTEM_BASE)


# ----------------------------------------------------------- DICOM SR

#: Coding-scheme designators the SR export may emit. DICOM uses short
#: designators, not URIs: ``SCT`` is SNOMED CT, ``DCM`` the DICOM
#: controlled terminology, ``UCUM`` the unit system.
ALLOWED_SR_SCHEMES: frozenset[str] = frozenset({"SCT", "DCM", "UCUM"})


def test_sr_export_emits_only_allowlisted_schemes() -> None:
    tables: list[dict[str, dict[str, str]]] = [sr._TOOL_TO_CONCEPT, sr._UNIT_TO_UCUM]
    for table in tables:
        for key, coding in table.items():
            scheme = coding.get("scheme", "")
            assert scheme in ALLOWED_SR_SCHEMES, (
                f"SR coding {key!r} uses unvetted scheme {scheme!r}; "
                f"allowed: {sorted(ALLOWED_SR_SCHEMES)}"
            )


def test_sr_units_are_ucum() -> None:
    """Units are the one place a wrong code silently changes a number."""
    for unit, coding in sr._UNIT_TO_UCUM.items():
        assert coding["scheme"] == "UCUM", f"unit {unit!r} is not UCUM-coded"
