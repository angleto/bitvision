"""Pure-unit tests for the training-cohort labels manifest (P5).

No DB. Guards the safety-critical surface: a training cohort spans
patients by construction, so the manifest builder MUST strip every
patient / study / finding / author identifier and re-key to synthetic
ids. A leak here is a breach, so it is tested directly.
"""

from __future__ import annotations

import json
import uuid

from bvphoenix.services.training_cohort import FindingExportRow, build_labels_manifest


def _row(study_id: uuid.UUID, finding_id: uuid.UUID, author: str = "agent") -> FindingExportRow:
    return FindingExportRow(
        finding_id=finding_id,
        study_id=study_id,
        author_kind=author,
        type_key="nodule",
        type_category="lesion",
        type_code=None,
        anatomy_key="lung_upper_lobe",
        anatomy_code=None,
        laterality="right",
        morphology=["spiculated"],
        measurements={"longest_diameter_mm": 14.0, "suv_max": 6.2},
        bbox_lps=None,
        status="confirmed",
        confidence=0.9,
        geometry=[{"role": "mask", "kind": "segmentation", "mask_label": "nodule_1"}],
    )


def test_manifest_deidentifies_and_rekeys() -> None:
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    f1, f2, f3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [_row(s1, f1), _row(s1, f2), _row(s2, f3)]
    m = build_labels_manifest(
        rows,
        dataset_id="ds-x",
        generated_at="2026-06-06T00:00:00Z",
        kanon={"ct/lung": 7},
    )

    blob = json.dumps(m)
    # No raw identifier leaks (patient/study/finding/author UUIDs).
    for ident in (str(s1), str(s2), str(f1), str(f2), str(f3)):
        assert ident not in blob, f"identifier leaked into manifest: {ident}"

    # Synthetic re-keying: 2 studies, 3 findings; same study -> same id.
    assert m["study_count"] == 2
    assert m["finding_count"] == 3
    study_ids = [it["study_id"] for it in m["items"]]
    assert study_ids[0] == study_ids[1] != study_ids[2]
    assert m["items"][0]["finding_id"] == "finding-0001"
    assert m["items"][0]["study_id"] == "study-0001"

    # Coded fields + provenance class retained.
    it = m["items"][0]
    assert it["type"] == "nodule"
    assert it["category"] == "lesion"
    assert it["anatomy"] == "lung_upper_lobe"
    assert it["laterality"] == "right"
    assert it["morphology"] == ["spiculated"]
    assert it["measurements"]["longest_diameter_mm"] == 14.0
    assert it["author_kind"] == "agent"
    assert it["geometry"][0]["kind"] == "segmentation"
    assert m["k_anonymity"] == {"ct/lung": 7}
    assert m["schema"] == "bvphoenix.training-labels/v1"


def test_empty_manifest_is_well_formed() -> None:
    m = build_labels_manifest([], dataset_id="ds-0", generated_at="t")
    assert m["finding_count"] == 0
    assert m["study_count"] == 0
    assert m["items"] == []
