"""Tests for the P3 Finding MCP tools (the structured diagnostic surface).

Registry/enum consistency + the create / update / delete / restore /
search / geometry tools forward to the right ``/api/findings`` endpoints
with the right verbs, params, headers and bodies. All requests are
intercepted with ``httpx.MockTransport`` (no backend).
"""

from __future__ import annotations

import json

import httpx

from bvmcp.scopes import TOOL_SCOPE
from bvmcp.tools import findings as findings_tools
from bvmcp.tools import training as training_tools

from .conftest import TEST_TOKEN, mock_backend


def _json_response(payload: dict | list) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _assert_auth(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"


def test_findings_dispatch_tools_and_scopes_consistent() -> None:
    tool_names = {t.name for t in findings_tools.TOOLS}
    for name in findings_tools._DISPATCH:
        assert name in tool_names, f"{name} dispatched but not declared"
        assert name in TOOL_SCOPE, f"{name} dispatched but unscoped"
    for t in findings_tools.TOOLS:
        assert t.name in findings_tools._DISPATCH
    # The full diagnostic loop is exposed.
    assert {
        "get_finding_vocab",
        "search_findings",
        "get_finding",
        "get_finding_revisions",
        "create_finding",
        "update_finding",
        "delete_finding",
        "restore_finding",
        "add_finding_geometry",
    } <= tool_names
    # read vs write scoping
    assert TOOL_SCOPE["search_findings"] == "findings:read"
    assert TOOL_SCOPE["create_finding"] == "findings:write"
    assert TOOL_SCOPE["delete_finding"] == "findings:write"


def test_find_similar_findings_registered_and_read_scoped() -> None:
    assert "find_similar_findings" in {t.name for t in findings_tools.TOOLS}
    assert "find_similar_findings" in findings_tools._DISPATCH
    assert TOOL_SCOPE["find_similar_findings"] == "findings:read"
    tool = next(t for t in findings_tools.TOOLS if t.name == "find_similar_findings")
    assert tool.annotations.readOnlyHint is True


async def test_find_similar_findings_forwards_params() -> None:
    fid = "f-9"
    payload = [{"finding": {"id": "f-10"}, "score": 0.93, "matched_series_id": "s-2"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/findings/{fid}/similar"
        assert request.url.params["k"] == "5"
        assert request.url.params["same_type"] == "true"
        assert request.url.params["modality"] == "CT"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await findings_tools.handle(
            "find_similar_findings",
            {"finding_id": fid, "k": 5, "same_type": True, "modality": "CT"},
        )
    assert json.loads(result) == payload


async def test_get_finding_vocab() -> None:
    payload = {"finding_types": [], "anatomy_sites": [], "morphology_terms": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/findings/vocab"
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await findings_tools.handle("get_finding_vocab", {})
    assert json.loads(result) == payload


async def test_create_finding_forwards_body_dry_run_idempotency() -> None:
    pid = "p-1"
    payload = {"id": "f-1", "type": "nodule", "etag": "e1"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/patients/{pid}/findings"
        assert request.url.params["dry_run"] == "true"
        assert request.headers["idempotency-key"] == "idem-1"
        body = json.loads(request.content)
        assert body["type"] == "nodule"
        assert body["study_id"] == "s-1"
        assert body["longest_diameter_mm"] == 14.0
        assert body["morphology"] == ["spiculated"]
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await findings_tools.handle(
            "create_finding",
            {
                "patient_id": pid,
                "study_id": "s-1",
                "type": "nodule",
                "morphology": ["spiculated"],
                "longest_diameter_mm": 14.0,
                "dry_run": True,
                "idempotency_key": "idem-1",
            },
        )
    assert json.loads(result) == payload


async def test_update_finding_partial_body_and_if_match() -> None:
    fid = "f-2"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == f"/api/findings/{fid}"
        assert request.headers["if-match"] == '"old-etag"'
        body = json.loads(request.content)
        # only provided keys are sent (PATCH semantics)
        assert body == {"status": "confirmed", "suv_max": 6.2}
        _assert_auth(request)
        return _json_response({"id": fid, "status": "confirmed"})

    with mock_backend(handler):
        result = await findings_tools.handle(
            "update_finding",
            {"finding_id": fid, "status": "confirmed", "suv_max": 6.2, "if_match": "old-etag"},
        )
    assert json.loads(result)["status"] == "confirmed"


async def test_delete_finding_soft_with_reason_and_if_match() -> None:
    fid = "f-3"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/api/findings/{fid}"
        assert request.url.params["reason"] == "duplicate"
        assert request.headers["if-match"] == '"cur"'
        _assert_auth(request)
        return httpx.Response(204)

    with mock_backend(handler):
        result = await findings_tools.handle(
            "delete_finding", {"finding_id": fid, "reason": "duplicate", "if_match": "cur"}
        )
    body = json.loads(result)
    assert body["recoverable"] is True
    assert body["http_status"] == 204


async def test_restore_finding_posts_to_restore() -> None:
    fid = "f-4"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/findings/{fid}/restore"
        _assert_auth(request)
        return _json_response({"id": fid, "deleted_at": None})

    with mock_backend(handler):
        result = await findings_tools.handle("restore_finding", {"finding_id": fid})
    assert json.loads(result)["deleted_at"] is None


async def test_search_findings_forwards_structured_filters() -> None:
    pid = "p-9"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/patients/{pid}/findings"
        p = request.url.params
        assert p["type"] == "nodule"
        assert p["anatomy"] == "lung_upper_lobe"
        assert p["min_diameter_mm"] == "10"
        assert p["morphology"] == "spiculated"
        _assert_auth(request)
        return _json_response([])

    with mock_backend(handler):
        result = await findings_tools.handle(
            "search_findings",
            {
                "patient_id": pid,
                "type": "nodule",
                "anatomy": "lung_upper_lobe",
                "morphology": ["spiculated"],
                "min_diameter_mm": 10,
            },
        )
    assert json.loads(result) == []


async def test_search_findings_corpus_mode_omits_patient() -> None:
    """No patient_id -> corpus-wide visibility-scoped search at
    /api/findings/search, forwarding scope + structured filters."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/findings/search"
        p = request.url.params
        assert p["type"] == "mass"
        assert p["scope"] == "mine"
        assert p["min_volume_ml"] == "5"
        _assert_auth(request)
        return _json_response([])

    with mock_backend(handler):
        result = await findings_tools.handle(
            "search_findings",
            {"type": "mass", "scope": "mine", "min_volume_ml": 5},
        )
    assert json.loads(result) == []


async def test_add_finding_geometry_posts_link() -> None:
    fid = "f-5"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/findings/{fid}/geometry"
        body = json.loads(request.content)
        assert body == {"role": "bbox", "marker_id": "m-1"}
        _assert_auth(request)
        return _json_response({"id": fid, "geometry": [{"id": "g-1", "role": "bbox"}]})

    with mock_backend(handler):
        result = await findings_tools.handle(
            "add_finding_geometry",
            {"finding_id": fid, "role": "bbox", "marker_id": "m-1"},
        )
    assert json.loads(result)["geometry"][0]["role"] == "bbox"


async def test_promote_finding_measurement_posts_voi_with_headers() -> None:
    fid = "f-9"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/findings/{fid}/promote-measurement"
        body = json.loads(request.content)
        assert body == {
            "series_id": "s-1",
            "source": "voi_spherical",
            "center_mm": {"x": 1.0, "y": 2.0, "z": 3.0},
            "radius_mm": 10.0,
        }
        # idempotency + optimistic-concurrency headers are forwarded.
        assert request.headers["idempotency-key"] == "idem-1"
        assert request.headers["if-match"] == '"etag-7"'
        _assert_auth(request)
        return _json_response({"id": fid, "suv_max": 5.2, "volume_ml": 12.0, "status": "candidate"})

    with mock_backend(handler):
        result = await findings_tools.handle(
            "promote_finding_measurement",
            {
                "finding_id": fid,
                "series_id": "s-1",
                "source": "voi_spherical",
                "center_mm": {"x": 1.0, "y": 2.0, "z": 3.0},
                "radius_mm": 10.0,
                "idempotency_key": "idem-1",
                "if_match": '"etag-7"',
            },
        )
    out = json.loads(result)
    assert out["suv_max"] == 5.2
    assert out["status"] == "candidate"


async def test_export_training_manifest_posts_query() -> None:
    """P5: the training-manifest tool POSTs the structured query and
    returns the de-identified manifest the backend builds."""
    payload = {"dataset_id": "ds-1", "finding_count": 0, "items": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/training-exports/manifest"
        body = json.loads(request.content)
        assert body["type"] == "nodule"
        assert body["k_min"] == 5
        _assert_auth(request)
        return _json_response(payload)

    with mock_backend(handler):
        result = await training_tools.handle(
            "export_training_manifest",
            {"type": "nodule", "k_min": 5},
        )
    assert json.loads(result) == payload


async def test_export_training_cohort_bundle_enqueues_job() -> None:
    """P5-rest: the bundle tool POSTs the query to the async endpoint and
    returns the Job descriptor to poll."""
    job = {"id": "job-1", "kind": "training_cohort_export", "status": "queued"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/training-exports"
        body = json.loads(request.content)
        assert body["type"] == "mass"
        assert body["min_diameter_mm"] == 20
        _assert_auth(request)
        return _json_response(job)

    with mock_backend(handler):
        result = await training_tools.handle(
            "export_training_cohort_bundle",
            {"type": "mass", "min_diameter_mm": 20},
        )
    assert json.loads(result)["status"] == "queued"
