"""Pure unit tests for the rule-based extractor (Sprint 4, ADR 0008)."""

from __future__ import annotations

from bvphoenix.services.clinical_entities import (
    EXTRACTOR_VERSION,
    canonical_payload,
    extract_entities,
)


def test_extract_lab_value() -> None:
    text = "Creatinina 1.2 mg/dL ad oggi"
    out = extract_entities(text)
    labs = out.entities_proposed["lab_values"]
    assert any(
        lab["analyte"].strip().lower() == "creatinina" and lab["value"] == 1.2 for lab in labs
    )
    assert all(lab["unit"] == "mg/dL" for lab in labs)


def test_extract_blood_pressure_and_hr() -> None:
    text = "PA 130/80 mmHg, FC 72 bpm"
    out = extract_entities(text)
    kinds = {m["kind"] for m in out.entities_proposed["measurements"]}
    assert "blood_pressure" in kinds
    assert "heart_rate" in kinds


def test_extract_dates_iso_normalises() -> None:
    text = "Visita del 12/01/2024 e del 5 marzo 2024"
    out = extract_entities(text)
    isos = sorted(d["iso"] for d in out.entities_proposed["dates"])
    assert isos == ["2024-01-12", "2024-03-05"]


def test_extract_procedures_normalised() -> None:
    text = "Prescritta TC torace e RMN encefalo"
    out = extract_entities(text)
    modalities = {p["modality"] for p in out.entities_proposed["procedures_keywords"]}
    assert "TC" in modalities
    assert "RM" in modalities


def test_canonical_payload_is_deterministic() -> None:
    text = "Creatinina 1.2 mg/dL"
    a = canonical_payload(extract_entities(text))
    b = canonical_payload(extract_entities(text))
    assert a == b


def test_extractor_version_constant_in_payload() -> None:
    out = extract_entities("nothing here")
    assert out.extractor_version == EXTRACTOR_VERSION


def test_unknown_text_returns_empty_namespaces() -> None:
    out = extract_entities("Lorem ipsum dolor sit amet, no clinical content.")
    assert out.entities_proposed["lab_values"] == []
    assert out.entities_proposed["measurements"] == []
    assert out.entities_proposed["dates"] == []
    assert out.entities_proposed["procedures_keywords"] == []
