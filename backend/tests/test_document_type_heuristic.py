"""Tests for the filename / text-preview document-type heuristic."""

from __future__ import annotations

import pytest

from bvphoenix.services.document_type_heuristic import guess_document_type


@pytest.mark.parametrize(
    "filename,expected",
    [
        # --- explicit spec fixtures --------------------------------------
        ("lab-results-2024.pdf", "lab_result"),
        ("discharge_letter_mario_rossi.pdf", "discharge_summary"),
        ("ricetta-bianca-dott-bianchi.pdf", "prescription"),
        ("consenso-informato.pdf", "consent"),
        # 0045 reclassifies radiology reports out of ``er_report`` into
        # the LOINC-aligned ``imaging_report`` bucket. The ER report
        # type stays for triage / pronto-soccorso letters only.
        ("referto-tac-torace-2024-03.pdf", "radiology_report"),
        ("visita-oncologica-2025.pdf", "specialist_visit_note"),
        ("controllo-periodico-3-mesi.pdf", "progress_note"),
        ("anamnesi-patologica-prossima.pdf", "history_physical"),
        ("foto-documento.jpg", "personal_note"),
        ("note.txt", "progress_note"),
        ("random-file.txt", "progress_note"),
        ("screenshot.png", "personal_note"),
    ],
)
def test_spec_fixtures(filename: str, expected: str) -> None:
    assert guess_document_type(filename) == expected


def test_consent_english() -> None:
    assert guess_document_type("Patient_Consent_Form.pdf") == "consent"


def test_privacy_maps_to_consent() -> None:
    assert guess_document_type("privacy-policy.pdf") == "consent"


def test_prescription_english() -> None:
    assert guess_document_type("prescription_oct_2024.pdf") == "prescription"


def test_terapia_maps_to_prescription() -> None:
    assert guess_document_type("terapia-domiciliare.pdf") == "prescription"


def test_referral_italian() -> None:
    assert guess_document_type("richiesta-visita-cardiologica.pdf") == "referral"


def test_lab_result_italian() -> None:
    assert guess_document_type("analisi-del-sangue.pdf") == "lab_result"


def test_lab_result_blood_english() -> None:
    assert guess_document_type("blood_test_results.pdf") == "lab_result"


def test_er_report_pronto_soccorso() -> None:
    assert guess_document_type("pronto-soccorso-2024.pdf") == "emergency_report"


def test_emergency_english() -> None:
    assert guess_document_type("emergency_room_report.pdf") == "emergency_report"


def test_radiology_report() -> None:
    # Radiology reports map to LOINC 18748-4 / ``imaging_report`` after
    # migration 0045; the previous ``er_report`` mapping was a stop-gap
    # until a dedicated imaging type existed.
    assert guess_document_type("referto-rx-torace.pdf") == "radiology_report"
    assert guess_document_type("MRI_brain_report.pdf") == "radiology_report"
    assert guess_document_type("ecografia_addome_2024.pdf") == "radiology_report"


def test_specialist_report() -> None:
    # LOINC 11488-4 — consultation note from any specialist visit.
    assert guess_document_type("visita-cardiologica-2025-03.pdf") == "specialist_visit_note"
    assert guess_document_type("oncology_consultation_note.pdf") == "specialist_visit_note"


def test_progress_note() -> None:
    # LOINC 11506-3 — follow-up / decorso clinico.
    assert guess_document_type("controllo-post-operatorio.pdf") == "progress_note"
    assert guess_document_type("follow_up_3m.pdf") == "progress_note"


def test_history_and_physical() -> None:
    # LOINC 34117-2 — anamnesi + esame obiettivo.
    assert guess_document_type("anamnesi-patologica.pdf") == "history_physical"
    assert guess_document_type("history_and_physical_2024.pdf") == "history_physical"


def test_image_without_hint_is_personal_notebook() -> None:
    assert guess_document_type("IMG_20240512_142233.jpeg") == "personal_note"
    assert guess_document_type("scan.tiff") == "personal_note"
    assert guess_document_type("photo.HEIC") == "personal_note"


def test_unknown_extension_is_other() -> None:
    assert guess_document_type("archive.zip") == "unclassified"
    assert guess_document_type("data.bin") == "unclassified"


def test_no_extension_is_other() -> None:
    assert guess_document_type("mystery") == "unclassified"


def test_text_preview_triggers_consent() -> None:
    preview = (
        "Ospedale San Raffaele\n"
        "CONSENSO INFORMATO al trattamento dei dati personali\n"
        "Il sottoscritto dichiara di aver ricevuto..."
    )
    assert guess_document_type("scan001.pdf", preview) == "consent"


def test_text_preview_triggers_discharge() -> None:
    preview = (
        "Lettera di dimissione ospedaliera\nPaziente: Mario Rossi\nData di dimissione: 12/03/2024"
    )
    assert guess_document_type("doc.pdf", preview) == "discharge_summary"


def test_text_preview_triggers_prescription() -> None:
    preview = (
        "Prescrizione medica\n"
        "Dott. Giuseppe Bianchi\n"
        "Farmaco: amoxicillina 500mg, 2 volte al giorno."
    )
    assert guess_document_type("doc.pdf", preview) == "prescription"


def test_text_preview_triggers_lab_result() -> None:
    preview = (
        "Referto di laboratorio analisi cliniche\nEmocromo completo\nGlobuli rossi: 4.5 x10^12/L"
    )
    assert guess_document_type("doc.pdf", preview) == "lab_result"


def test_text_preview_triggers_er_report() -> None:
    preview = "Pronto Soccorso - Ospedale\nCodice triage: giallo\nArrivo: 14:22"
    assert guess_document_type("doc.pdf", preview) == "emergency_report"


def test_filename_wins_over_text_preview() -> None:
    # Filename is specific ("consenso"), preview mentions prescription —
    # filename rules run first and should pin the result to consent.
    preview = "Prescrizione medica del paziente..."
    assert guess_document_type("consenso-informato.pdf", preview) == "consent"


def test_text_preview_ignored_when_filename_is_clear() -> None:
    assert guess_document_type("lab-results.pdf", "Random unrelated prose.") == "lab_result"


def test_blank_text_preview_falls_back() -> None:
    assert guess_document_type("mystery.pdf", "") == "progress_note"
    assert guess_document_type("mystery.pdf", None) == "progress_note"


def test_text_preview_on_image_without_filename_hit() -> None:
    # Even though the file is a JPG, a strong preview signal should win.
    preview = "CONSENSO INFORMATO al trattamento sanitario."
    assert guess_document_type("scan.jpg", preview) == "consent"


def test_path_prefix_does_not_break_matching() -> None:
    assert guess_document_type("/tmp/uploads/Ricetta.pdf") == "prescription"
    assert guess_document_type("C:\\Users\\me\\Desktop\\Consenso.pdf") == "consent"


def test_case_insensitive() -> None:
    assert guess_document_type("DISCHARGE_LETTER.PDF") == "discharge_summary"
    assert guess_document_type("LAB-RESULTS.pdf") == "lab_result"


def test_empty_filename_is_other() -> None:
    assert guess_document_type("") == "unclassified"
