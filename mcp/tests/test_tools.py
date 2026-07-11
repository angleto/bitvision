"""Unit tests for all 13 MCP tools plus cross-cutting server behavior.

Each tool test intercepts outgoing backend calls via ``httpx.MockTransport``
and asserts:

* request path / method match the documented backend contract
* the bearer token from ``BVP_MCP_USER_TOKEN`` is forwarded
* query params or JSON body carry the caller's inputs
* the tool returns the backend payload correctly serialized for MCP clients
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from bvmcp.server import ALL_TOOLS, call_tool, list_tools
from bvmcp.tools import annotations as annotations_tools
from bvmcp.tools import bundle as bundle_tools
from bvmcp.tools import images as images_tools
from bvmcp.tools import patients as patients_tools
from bvmcp.tools import search as search_tools
from bvmcp.tools import studies as studies_tools

from .conftest import TEST_TOKEN, mock_backend


def _json_response(payload: dict | list) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _assert_auth(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
    assert request.headers["accept"] == "application/json"


# --------------------------------------------------------------------------- #
# search tools
# --------------------------------------------------------------------------- #


async def test_search_studies_forwards_filters_and_returns_results() -> None:
    payload = {"items": [{"id": "s1", "description": "chest CT"}], "total": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/search"
        assert request.url.params["q"] == "pneumonia"
        assert request.url.params["modality"] == "CT"
        assert request.url.params["body_part"] == "chest"
        assert request.url.params["date_from"] == "2024-01-01"
        assert request.url.params["date_to"] == "2024-12-31"
        assert request.url.params["limit"] == "50"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await search_tools.handle(
            "search_studies",
            {
                "q": "pneumonia",
                "modality": "CT",
                "body_part": "chest",
                "date_from": "2024-01-01",
                "date_to": "2024-12-31",
                "limit": 50,
            },
        )

    assert json.loads(result) == payload


async def test_search_studies_uses_default_limit_when_unset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "20"
        return _json_response({"items": [], "total": 0})

    with mock_backend(handler):
        await search_tools.handle("search_studies", {})


async def test_similar_to_hits_uuid_path_with_params() -> None:
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    payload = [{"id": "s2", "score": 0.91}, {"id": "s3", "score": 0.83}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/similar-to/{uuid}"
        assert request.url.params["k"] == "5"
        assert request.url.params["modality"] == "MR"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await search_tools.handle(
            "similar_to",
            {"target_id": uuid, "k": 5, "modality": "MR"},
        )

    assert json.loads(result) == payload


# --------------------------------------------------------------------------- #
# studies tools
# --------------------------------------------------------------------------- #


async def test_get_study_fetches_by_uuid() -> None:
    uuid = "study-uuid"
    payload = {"id": uuid, "description": "CT chest", "series": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/studies/{uuid}"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await studies_tools.handle("get_study", {"study_id": uuid})

    assert json.loads(result) == payload


async def test_get_series_fetches_by_uuid() -> None:
    uuid = "series-uuid"
    payload = {"id": uuid, "modality": "CT", "instance_count": 120}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/series/{uuid}"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await studies_tools.handle("get_series", {"series_id": uuid})

    assert json.loads(result) == payload


async def test_describe_series_posts_hint_in_body() -> None:
    uuid = "series-uuid"
    payload = {"annotation_id": "ann-1", "text": "descriptive text"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/series/{uuid}/llm/describe"
        body = json.loads(request.content)
        assert body == {"hint": "focus on cardiac"}
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await studies_tools.handle(
            "describe_series",
            {"series_id": uuid, "hint": "focus on cardiac"},
        )

    assert json.loads(result) == payload


async def test_embed_series_posts_to_embed_endpoint() -> None:
    uuid = "series-uuid"
    payload = {"job_id": "job-123", "status": "queued"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/series/{uuid}/embed"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await studies_tools.handle("embed_series", {"series_id": uuid})

    assert json.loads(result) == payload


# --------------------------------------------------------------------------- #
# annotations tools
# --------------------------------------------------------------------------- #


async def test_get_annotations_reads_markers_endpoint() -> None:
    payload = [
        {
            "id": "m1",
            "patient_id": "pat-uuid",
            "target_kind": "series",
            "target_id": "series-uuid",
            "kind": "measurement.distance",
            "author_kind": "human",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/patients/pat-uuid/markers"
        assert request.url.params["target_kind"] == "series"
        assert request.url.params["target_id"] == "series-uuid"
        assert request.url.params["kind"] == "measurement.distance"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await annotations_tools.handle(
            "get_annotations",
            {
                "patient_id": "pat-uuid",
                "target_kind": "series",
                "target_id": "series-uuid",
                "kind": "measurement.distance",
            },
        )

    parsed = json.loads(result)
    assert parsed["markers"] == payload
    assert parsed["marker_count"] == 1
    assert "notes" not in parsed


async def test_get_annotations_omits_kind_when_not_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/patients/pat-uuid/markers"
        assert "kind" not in request.url.params
        return _json_response([])

    with mock_backend(handler):
        await annotations_tools.handle(
            "get_annotations",
            {
                "patient_id": "pat-uuid",
                "target_kind": "study",
                "target_id": "study-uuid",
            },
        )


async def test_get_annotations_includes_notes_when_requested() -> None:
    marker_payload = [{"id": "m1", "kind": "bbox.lesion", "author_kind": "agent"}]
    note_payload = [{"id": "n1", "text": "follow-up needed", "author_kind": "agent"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/patients/pat-uuid/markers":
            return _json_response(marker_payload)
        if request.url.path == "/api/patients/pat-uuid/notes":
            assert request.url.params["author_kind"] == "agent"
            return _json_response(note_payload)
        raise AssertionError(f"unexpected path: {request.url.path}")

    with mock_backend(handler):
        result = await annotations_tools.handle(
            "get_annotations",
            {
                "patient_id": "pat-uuid",
                "target_kind": "study",
                "target_id": "study-uuid",
                "include_notes": True,
                "author_kind": "agent",
            },
        )

    parsed = json.loads(result)
    assert parsed["markers"] == marker_payload
    assert parsed["notes"] == note_payload


async def test_list_reports_fetches_study_reports() -> None:
    uuid = "study-uuid"
    payload = [{"id": "r1", "version": 1, "text": "final report"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/studies/{uuid}/reports"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await annotations_tools.handle("list_reports", {"study_id": uuid})

    assert json.loads(result) == payload


# --------------------------------------------------------------------------- #
# patient tools
# --------------------------------------------------------------------------- #


async def test_get_patient_fetches_profile() -> None:
    uuid = "patient-uuid"
    payload = {"id": uuid, "name": "Mario Rossi", "sex": "M"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{uuid}"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await patients_tools.handle("get_patient", {"patient_id": uuid})

    assert json.loads(result) == payload


async def test_get_fascicolo_index_fetches_structured_index() -> None:
    uuid = "patient-uuid"
    payload = {"studies": 4, "reports": 2, "documents": 1, "annotations": 7}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{uuid}/index"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await patients_tools.handle("get_fascicolo_index", {"patient_id": uuid})

    assert json.loads(result) == payload


async def test_get_patient_timeline_forwards_section_and_limit() -> None:
    uuid = "patient-uuid"
    payload = [{"date": "2024-05-01", "kind": "study", "id": "s1"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{uuid}/timeline"
        assert request.url.params["section"] == "studies"
        assert request.url.params["limit"] == "25"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await patients_tools.handle(
            "get_patient_timeline",
            {"patient_id": uuid, "section": "studies", "limit": 25},
        )

    assert json.loads(result) == payload


async def test_get_patient_timeline_uses_default_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "50"
        assert "section" not in request.url.params
        return _json_response([])

    with mock_backend(handler):
        await patients_tools.handle("get_patient_timeline", {"patient_id": "patient-uuid"})


async def test_list_patient_documents_forwards_type_filter() -> None:
    uuid = "patient-uuid"
    payload = [{"id": "d1", "type": "discharge_letter"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{uuid}/documents"
        assert request.url.params["type"] == "discharge_letter"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await patients_tools.handle(
            "list_patient_documents",
            {"patient_id": uuid, "type": "discharge_letter"},
        )

    assert json.loads(result) == payload


# --------------------------------------------------------------------------- #
# bundle tool — aggregates five endpoints into markdown + JSON appendix
# --------------------------------------------------------------------------- #


async def test_get_fascicolo_bundle_aggregates_all_sections() -> None:
    uuid = "patient-uuid"
    study_id = "study-1"

    patient = {
        "id": uuid,
        "display_name": "Mario Rossi",
        "birth_date": "1970-01-01",
        "sex": "M",
        "tax_id": "RSSMRA70A01H501Z",
        "phone": None,
        "email": None,
        "address": None,
        "blood_type": "A+",
        "allergies": "Penicillin",
        "notes": "Hypertension",
        "external_id": None,
        "created_at": "2024-01-01T00:00:00",
    }
    index = {
        "patient": patient,
        "sections": [
            {
                "key": "studies",
                "label": "Studi",
                "count": 1,
                "last_date": "2024-05-01",
                "breakdown": {"CT": 1},
            },
        ],
        "total_items": 1,
    }
    timeline = [
        {
            "type": "study",
            "date": "2024-05-01",
            "data": {
                "id": study_id,
                "study_description": "Chest CT",
                "modalities": ["CT"],
                "study_date": "2024-05-01",
            },
        },
        {
            "type": "document",
            "date": "2024-04-01",
            "data": {"id": "d1", "document_type": "prescription", "title": "Prescription"},
        },
    ]
    documents = [
        {
            "id": "d1",
            "patient_id": uuid,
            "document_type": "prescription",
            "title": "Prescription",
            "text": "500mg daily",
            "document_date": "2024-04-01",
            "created_at": "2024-04-01T10:00:00",
        }
    ]
    reports = [
        {
            "id": "r1",
            "study_id": study_id,
            "version": 1,
            "text": "Impression: clear",
            "created_at": "2024-05-02T09:00:00",
        }
    ]
    annotations = [
        {
            "id": "a1",
            "target_kind": "study",
            "target_id": study_id,
            "source": "llm",
            "kind": "description",
            "created_at": "2024-05-03T11:00:00",
        }
    ]

    paths_hit: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths_hit.append(request.url.path)
        if request.url.path == f"/api/patients/{uuid}":
            return _json_response(patient)
        if request.url.path == f"/api/patients/{uuid}/index":
            return _json_response(index)
        if request.url.path == f"/api/patients/{uuid}/timeline":
            assert request.url.params["limit"] == "50"
            return _json_response(timeline)
        if request.url.path == f"/api/patients/{uuid}/documents":
            return _json_response(documents)
        if request.url.path == f"/api/studies/{study_id}/reports":
            return _json_response(reports)
        if request.url.path == "/api/annotations":
            assert request.url.params["target_kind"] == "study"
            assert request.url.params["target_id"] == study_id
            return _json_response(annotations)
        return httpx.Response(404, json={"detail": "unexpected"})

    with mock_backend(handler):
        result = await call_tool("get_fascicolo_bundle", {"patient_id": uuid})

    # Two TextContent items: markdown + JSON appendix
    assert len(result) == 2
    markdown, appendix = result[0].text, result[1].text

    assert "# Fascicolo Paziente" in markdown
    assert "Mario Rossi" in markdown
    assert "Chest CT" in markdown
    assert "Prescription" in markdown
    assert "Impression: clear" in markdown

    data = json.loads(appendix)
    assert data["patient_id"] == uuid
    assert data["demographics"]["display_name"] == "Mario Rossi"
    assert data["studies"][0]["id"] == study_id
    assert data["reports_by_study"][study_id][0]["id"] == "r1"
    assert data["annotations_by_study"][study_id][0]["id"] == "a1"
    assert data["documents"][0]["id"] == "d1"

    # Every core endpoint was called
    assert f"/api/patients/{uuid}" in paths_hit
    assert f"/api/patients/{uuid}/index" in paths_hit
    assert f"/api/patients/{uuid}/timeline" in paths_hit
    assert f"/api/patients/{uuid}/documents" in paths_hit
    assert f"/api/studies/{study_id}/reports" in paths_hit
    assert "/api/annotations" in paths_hit


async def test_get_fascicolo_bundle_respects_include_filter() -> None:
    """When only demographics is requested, skip the fan-out."""
    uuid = "patient-uuid"
    patient = {"id": uuid, "display_name": "Jane Doe", "birth_date": None, "sex": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/patients/{uuid}":
            return _json_response(patient)
        # Index / timeline / documents are still fetched to build the overview+appendix
        return _json_response([] if request.url.path.endswith(("/timeline", "/documents")) else {})

    with mock_backend(handler) as recorder:
        result = await bundle_tools.handle(
            "get_fascicolo_bundle",
            {"patient_id": uuid, "include": ["demographics"], "lang": "en"},
        )

    assert len(result) == 2
    assert "# Patient Record" in result[0].text
    assert "Jane Doe" in result[0].text
    # No per-study fan-out happened (no /reports or /annotations paths)
    for req in recorder.requests:
        assert "/reports" not in req.url.path
        assert req.url.path != "/api/annotations"


async def test_get_fascicolo_bundle_tolerates_backend_errors() -> None:
    """If a single endpoint 5xx's, the bundle still renders from what's available."""
    uuid = "patient-uuid"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/patients/{uuid}":
            return _json_response({"id": uuid, "display_name": "X", "birth_date": None})
        # Everything else blows up
        return httpx.Response(503, json={"detail": "boom"})

    with mock_backend(handler):
        result = await call_tool("get_fascicolo_bundle", {"patient_id": uuid})

    # Still returns a markdown + appendix (not an "Error: …" single item)
    assert len(result) == 2
    assert not result[0].text.startswith("Error:")
    assert "# Fascicolo Paziente" in result[0].text


# --------------------------------------------------------------------------- #
# cross-cutting: list_tools, dispatcher, error handling
# --------------------------------------------------------------------------- #


EXPECTED_TOOL_NAMES = {
    # --- Help / self-service guides ---
    "help",
    # --- Sprint 1 / 2 — discovery + study/series reads ---
    "get_study",
    "get_deidentification_provenance",
    "get_series",
    "describe_series",
    "embed_series",
    "search_studies",
    "similar_to",
    "get_annotations",
    "list_reports",
    "get_patient",
    "get_fascicolo_index",
    "get_patient_timeline",
    "list_patient_documents",
    "get_series_thumbnail",
    "get_study_thumbnails",
    "list_pathology_slides",
    "get_pathology_slide",
    "get_slide_thumbnail",
    "get_slide_macro",
    "get_slide_region",
    "get_fascicolo_bundle",
    "semantic_search",
    "search_hybrid",
    "summarize",
    "search_by_tags",
    "list_tags",
    # --- Sprint 2 — document write tools (ADR 0001/0003/0004) ---
    "update_document",
    "bulk_update_documents",
    "link_document_to_study",
    "unlink_document_from_study",
    # --- Sprint 3 — soft-delete / restore / merge / OCR / signed URL ---
    "delete_document",
    "restore_document",
    "merge_documents",
    "download_document_binary",
    "get_document_text",
    "get_document_references",
    # --- Resumable upload sessions (chunked document ingest) ---
    "create_upload_session",
    "get_upload_session",
    "upload_session_chunk",
    "commit_upload_session",
    "abort_upload_session",
    # --- Sprint 4 — entities + lab time-series ---
    "extract_document_entities",
    "get_lab_timeseries",
    # --- Sprint 5 — imaging reads + annotation writes (ADR 0011) ---
    "get_series_dicom_meta",
    "get_series_slice",
    "write_annotation",
    "update_annotation",
    "delete_annotation",
    "restore_annotation",
    "get_annotation_revisions",
    # --- Findings — structured, coded clinical reperti (P3) ---
    "get_finding_vocab",
    "search_findings",
    "find_similar_findings",
    "get_finding",
    "get_finding_revisions",
    "create_finding",
    "update_finding",
    "delete_finding",
    "restore_finding",
    "add_finding_geometry",
    "promote_finding_measurement",
    "create_findings_from_hot_spots",
    # --- Lesion tracks — longitudinal lesion follow-up ---
    "list_lesion_tracks",
    "get_lesion_track",
    "get_lesion_trajectory",
    "get_lesion_track_revisions",
    "create_lesion_track",
    "update_lesion_track",
    "delete_lesion_track",
    "restore_lesion_track",
    "add_finding_to_track",
    "remove_finding_from_track",
    "propagate_lesion",
    # --- Response assessments (RECIST roll-up) ---
    "list_response_assessments",
    "get_response_assessment",
    "get_response_assessment_revisions",
    "compute_response_assessment",
    "recompute_response_assessment",
    "update_response_assessment",
    "delete_response_assessment",
    "restore_response_assessment",
    # --- Training cohort export (P5) ---
    "export_training_manifest",
    "export_training_cohort_bundle",
    "list_my_datasets",
    # --- Sprint 3.5 — agent-driven tag + metadata writes ---
    "add_tag_to_study",
    "remove_tag_from_study",
    "replace_study_tags",
    "update_study_metadata",
    "update_series_metadata",
    # --- Sprint 5b/6 — imaging maturity (ROI crop + measurements + SUV) ---
    "crop_series_roi",
    "measure_distance",
    "measure_volume",
    "get_suv",
    # --- Sprint 6 — segmentations registry + cross-modal registration ---
    "get_segmentations",
    "register_series",
    "get_registration",
    # --- Multiphase contrast-CT acquisition phases ---
    "list_study_phases",
    "detect_study_phases",
    "set_series_acquisition_phase",
    "compute_phase_washout",
    "compute_washout_map",
    "create_phase_enhancement_set",
    "list_phase_enhancement_sets",
    "get_phase_enhancement_set",
    "delete_phase_enhancement_set",
    "restore_phase_enhancement_set",
    # --- Patient anagrafica writes (ADR 0019 / patient:write scope) ---
    "update_patient",
    "decode_codice_fiscale",
    "add_patient_contact",
    "remove_patient_contact",
    "search_patients",
    # --- v3 phase 3d — clinical events + reports + provenance + identifiers ---
    "find_clinical_events",
    "get_event",
    "create_clinical_event",
    "update_clinical_event",
    "delete_clinical_event",
    "propose_event_link",
    "confirm_event_link",
    "find_reports",
    "get_report_content",
    "get_provenance_chain",
    "lookup_external_identifier",
    "extract_report_content",
    "link_external_identifier",
    "create_canonical_referto",
    "cite_source",
    "endorse_report_content",
    "reject_report_content",
    "supersede_report_content",
    # --- v3 — document operations (ingest / merge / split / source download) ---
    "ingest_document",
    "merge_aliases",
    "split_alias",
    "download_source_document",
    # --- Folder navigation + tree reshape (folders:read / folders:write) ---
    # The user requires the LLM to be able to navigate the fascicolo
    # tree and reorganise it via MCP. Keep these registered or cross-
    # patient triage by the agent breaks.
    "list_folders",
    "get_folder",
    "create_folder",
    "update_folder",
    "delete_folder",
    "add_item_to_folder",
    "remove_item_from_folder",
    # --- Care timeline & clinical phases (semantic groupings) ---
    "get_care_timeline",
    "render_care_timeline_svg",
    "get_care_phase",
    "list_care_phase_material",
    "list_care_phase_revisions",
    "propose_care_phases",
    "apply_phase_proposal",
    "create_care_phase",
    "update_care_phase",
    "assign_event_to_phase",
    "unassign_event_from_phase",
    "reorder_care_phases",
    "restore_care_phase_revision",
    # GUI-superset additions (gap-closing per memory
    # feedback_mcp_must_be_gui_superset).
    "list_care_phases",
    "get_care_timeline_health",
    "delete_care_phase",
    "export_care_timeline_ics",
    "export_care_timeline_pdf",
    "get_my_scopes",
    # --- Clinical notes (annotations:write) ---
    "write_clinical_note",
    "update_clinical_note",
    "delete_clinical_note",
    # --- Q&A orchestrator + chunk search ---
    "ask_about_patient",
    "search_text_chunks",
    # --- ROI compute + hot-spot discovery (imaging:read / imaging:compute) ---
    "compute_roi_stats",
    "find_hot_spots",
    # --- Calendar (FSM transitions + range / overdue / upcoming feeds) ---
    "confirm_event",
    "reschedule_event",
    "complete_event",
    "cancel_event",
    "mark_event_missed",
    "find_upcoming_events",
    "find_overdue_events",
    "find_events_by_date_range",
    "export_calendar_ics",
    # --- ReportContent mutation (sponsored cite path) ---
    "update_report_content",
    # --- Segmentation writes (auto + interactive + upload) — see
    # help(topic='segmentations'). Closes the discovery -> voxel
    # mask -> training-ready label loop from the MCP side; the
    # backend already exposed all three endpoints.
    "auto_segment_series",
    "predict_segmentation_interactive",
    "upload_segmentation",
    "delete_segmentation",
    # --- ClinicalEvent binary attachments (events:write / events:read).
    # Backend lives in api/clinical_event_attachments.py.
    "upload_clinical_event_attachment",
    "list_clinical_event_attachments",
    "download_clinical_event_attachment",
    "delete_clinical_event_attachment",
    "promote_clinical_event_attachment",
    # --- Event ↔ document reconciliation (v4.4.75). Backend lives in
    # api/clinical_event_documents.py.
    "link_event_document",
    "list_event_documents",
    "unlink_event_document",
    "find_documents_by_content_hash",
    # --- Share-link CRUD (sharing:write, sensitive). Backend lives
    # in api/sharing.py. Mintaging exposes patient data to the
    # outside, hence the sensitive scope.
    "create_study_share_link",
    "create_folder_share_link",
    "list_share_links",
    "update_share_link",
    "revoke_share_link",
    # --- Fascicolo / study / folder export + tokenised download
    # (fascicolo:export, sensitive; get_job rides patients:read).
    # Backend lives in api/patient_export.py + api/jobs.py +
    # api/auth.py (download-token). The OTP->curl flow.
    "export_fascicolo",
    "export_study",
    "export_segmentation_dicom_seg",
    "export_folder",
    "bulk_download",
    "export_health_record_bundle",
    "get_consent_ledger",
    "get_job",
    "issue_download_token",
    # --- Public iCal subscription handles (calendar:subscribe /
    # calendar:read, sensitive). Backend lives in api/calendar.py;
    # parity for the /settings/calendar GUI.
    "create_calendar_subscription",
    "list_calendar_subscriptions",
    "revoke_calendar_subscription",
    # --- Per-event ICS export (calendar:read). Backend api/calendar.py.
    "export_event_ics",
    # --- Patient tasks (v3.4 operational checklist, tasks:read /
    # tasks:write). Backend api/patient_tasks.py; parity for the
    # patient-page task checklist GUI.
    "list_patient_tasks",
    "get_patient_task",
    "find_overdue_tasks",
    "find_tasks_due_today",
    "create_patient_task",
    "update_patient_task",
    "delete_patient_task",
    "restore_patient_task",
    "start_task",
    "snooze_task",
    "wake_task",
    "complete_task",
    "drop_task",
    "reopen_task",
    "assign_task_to_contact",
    "export_task_ics",
    # --- Notifications (v3.5 outbound reminders, notifications:read /
    # notifications:write). Backend api/notifications.py +
    # services/notifications/.
    "list_notification_dispatches",
    "cancel_pending_dispatch",
    "configure_contact_channel",
    "revoke_consent",
    "send_test_notification",
    "start_telegram_link",
    "check_telegram_link",
    "unlink_telegram",
    # --- Embeddings admin (MCP-GUI parity for /admin/embeddings;
    # admin:embeddings scope, owner must be a platform admin). Backend
    # api/embeddings_admin.py.
    "get_embedding_coverage",
    "get_text_embedding_coverage",
    "retry_failed_embeddings",
    "embed_missing_targets",
    "reembed_text_chunks",
    # --- Patient inbound inbox (fbbf5270 §12; inbox:read / inbox:manage /
    # inbox:review scopes). Backend api/inbox.py.
    "list_patient_inbox_addresses",
    "create_inbox_address",
    "set_inbox_address_label",
    "revoke_inbox_address",
    "configure_trusted_senders",
    "list_inbox_items",
    "get_inbox_item",
    "accept_inbox_item",
    "reject_inbox_item",
    # --- Public contributions (OpenData publish quarantine; contributions:read
    # / contributions:review scopes). Backend api/contributions.py. Accept is
    # human-only (no agent-accept tool by design).
    "list_contribution_queue",
    "get_contribution",
    "reject_contribution",
    "get_contribution_gt_boxes",
    "save_contribution_gt_boxes",
    "score_contribution_gt",
    "get_deid_recall_runs",
    # --- Public dataset catalog (OpenData commons; catalog:read scope).
    # Backend api/catalog.py. Read-only, aggregate/citation data, no PHI.
    "list_public_datasets",
    "get_public_dataset",
    "get_dataset_citation",
}


async def test_list_tools_returns_all_well_formed_tools() -> None:
    tools = await list_tools()
    assert len(tools) == len(EXPECTED_TOOL_NAMES)
    assert {t.name for t in tools} == EXPECTED_TOOL_NAMES
    for tool in tools:
        assert tool.name, "tool must have a name"
        assert tool.description and len(tool.description) > 10, (
            f"tool {tool.name!r} must have a meaningful description"
        )
        assert isinstance(tool.inputSchema, dict)
        assert tool.inputSchema.get("type") == "object"
        assert "properties" in tool.inputSchema


async def test_all_tools_matches_list_tools() -> None:
    """Guard against ALL_TOOLS drifting from what list_tools returns."""
    tools = await list_tools()
    assert {t.name for t in ALL_TOOLS} == {t.name for t in tools}


async def test_call_tool_unknown_returns_meaningful_error() -> None:
    result = await call_tool("does_not_exist", {})
    assert len(result) == 1
    assert result[0].type == "text"
    assert "unknown tool" in result[0].text.lower()
    assert "does_not_exist" in result[0].text


async def test_call_tool_dispatches_to_correct_handler() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/patients/p1"
        return _json_response({"id": "p1", "name": "Test Patient"})

    with mock_backend(handler):
        result = await call_tool("get_patient", {"patient_id": "p1"})

    assert len(result) == 1
    assert result[0].type == "text"
    assert json.loads(result[0].text) == {"id": "p1", "name": "Test Patient"}


async def test_call_tool_accepts_none_arguments() -> None:
    """Dispatcher must coerce ``None`` arguments to an empty dict."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"items": [], "total": 0})

    with mock_backend(handler):
        result = await call_tool("search_studies", None)

    assert len(result) == 1
    assert result[0].type == "text"


async def test_backend_401_surfaces_as_user_friendly_error() -> None:
    """401 must come back as a structured text error carrying the
    backend's response body so the agent can self-correct (e.g. read
    ``required_scope``). The previous behaviour collapsed the body
    into the one-line repr of ``HTTPStatusError``; the fence in
    ``server*.py`` now routes HTTPStatusError through
    ``format_http_error`` to keep the diagnostic intact."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid token"})

    with mock_backend(handler):
        result = await call_tool("get_study", {"study_id": "x"})

    assert len(result) == 1
    assert result[0].type == "text"
    text = result[0].text
    payload = json.loads(text)
    assert payload["error"] == "backend_error"
    assert payload["http_status"] == 401
    assert payload["detail"] == {"detail": "invalid token"}
    # No python traceback leaking into the response
    assert "Traceback" not in text


async def test_backend_5xx_surfaces_as_retry_or_report_error() -> None:
    """5xx must surface as an MCP-visible structured error, not crash
    the server. Format matches the 4xx path so the agent can branch
    on ``http_status`` without re-parsing free text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "service unavailable"})

    with mock_backend(handler):
        result = await call_tool("search_studies", {"q": "anything"})

    assert len(result) == 1
    assert result[0].type == "text"
    text = result[0].text
    payload = json.loads(text)
    assert payload["error"] == "backend_error"
    assert payload["http_status"] == 503
    assert payload["detail"] == {"detail": "service unavailable"}
    assert "Traceback" not in text


async def test_missing_token_omits_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no user token is configured, no authorization header is sent."""
    from bvmcp.config import get_settings

    monkeypatch.setattr(get_settings(), "user_token", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return _json_response({"id": "x"})

    with mock_backend(handler):
        await studies_tools.handle("get_study", {"study_id": "x"})


@pytest.mark.parametrize(
    "name,args,expected_path",
    [
        ("get_study", {"study_id": "u"}, "/api/studies/u"),
        (
            "get_deidentification_provenance",
            {"study_id": "u"},
            "/api/studies/u/deidentification-provenance",
        ),
        ("get_series", {"series_id": "u"}, "/api/series/u"),
        ("describe_series", {"series_id": "u"}, "/api/series/u/llm/describe"),
        ("embed_series", {"series_id": "u"}, "/api/series/u/embed"),
        ("search_studies", {}, "/api/search"),
        ("similar_to", {"target_id": "u"}, "/api/similar-to/u"),
        (
            "get_annotations",
            {"patient_id": "p", "target_kind": "study", "target_id": "u"},
            "/api/patients/p/markers",
        ),
        ("list_reports", {"study_id": "u"}, "/api/studies/u/reports"),
        ("get_patient", {"patient_id": "u"}, "/api/patients/u"),
        ("get_fascicolo_index", {"patient_id": "u"}, "/api/patients/u/index"),
        ("get_patient_timeline", {"patient_id": "u"}, "/api/patients/u/timeline"),
        ("list_patient_documents", {"patient_id": "u"}, "/api/patients/u/documents"),
    ],
)
async def test_every_json_tool_hits_expected_backend_path(
    name: str, args: dict, expected_path: str
) -> None:
    """Smoke-check the URL contract for each JSON-returning tool in one go."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path
        _assert_auth(request)
        return _json_response({})

    with mock_backend(handler) as recorder:
        result = await call_tool(name, args)

    assert len(recorder.requests) == 1
    assert result[0].type == "text"
    # Output must not be an error
    assert not result[0].text.startswith("Error:")


# --------------------------------------------------------------------------- #
# images tools
# --------------------------------------------------------------------------- #


def _jpeg_response(jpeg_bytes: bytes) -> httpx.Response:
    return httpx.Response(200, content=jpeg_bytes, headers={"content-type": "image/jpeg"})


async def test_get_series_thumbnail_returns_image_and_text_content() -> None:
    uuid = "series-uuid"
    # Minimal fake JPEG payload — the tool should base64-encode as-is.
    jpeg_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/series/{uuid}/thumbnail"
        assert request.url.params["index"] == "3"
        assert request.url.params["wc_delta"] == "10.0"
        assert request.url.params["ww_delta"] == "-5.0"
        assert request.url.params["max_side"] == "256"
        # Bearer token still forwarded; accept header omitted for binary fetch.
        assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
        assert (
            "accept" not in request.headers or request.headers.get("accept") != "application/json"
        )
        return _jpeg_response(jpeg_bytes)

    with mock_backend(handler):
        result = await images_tools.handle(
            "get_series_thumbnail",
            {"series_id": uuid, "slice_index": 3, "wc": 10.0, "ww": -5.0, "max_side": 256},
        )

    assert len(result) == 2
    image_block, text_block = result
    assert image_block.type == "image"
    assert image_block.mimeType == "image/jpeg"
    assert base64.b64decode(image_block.data) == jpeg_bytes
    assert text_block.type == "text"
    assert uuid in text_block.text
    assert "index 3" in text_block.text


async def test_get_series_thumbnail_defaults_middle_slice_and_zero_offsets() -> None:
    uuid = "series-uuid"

    def handler(request: httpx.Request) -> httpx.Response:
        # ``index`` omitted lets the backend pick the middle slice.
        assert "index" not in request.url.params
        assert request.url.params["wc_delta"] == "0.0"
        assert request.url.params["ww_delta"] == "0.0"
        assert request.url.params["max_side"] == "512"
        return _jpeg_response(b"x")

    with mock_backend(handler):
        result = await images_tools.handle("get_series_thumbnail", {"series_id": uuid})

    assert result[0].type == "image"
    assert "middle" in result[1].text


async def test_get_series_thumbnail_via_call_tool_forwards_image_content() -> None:
    """End-to-end through the server dispatcher: ImageContent survives."""
    jpeg_bytes = b"\xff\xd8\xff\xe0more-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/series/u/thumbnail"
        return _jpeg_response(jpeg_bytes)

    with mock_backend(handler):
        result = await call_tool("get_series_thumbnail", {"series_id": "u"})

    assert len(result) == 2
    assert result[0].type == "image"
    assert result[1].type == "text"


async def test_get_series_thumbnail_surfaces_404_as_user_friendly_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "series not found"})

    with mock_backend(handler):
        result = await call_tool("get_series_thumbnail", {"series_id": "missing"})

    assert len(result) == 1
    assert result[0].type == "text"
    payload = json.loads(result[0].text)
    assert payload["error"] == "backend_error"
    assert payload["http_status"] == 404
    assert payload["detail"] == {"detail": "series not found"}
    assert "Traceback" not in result[0].text
