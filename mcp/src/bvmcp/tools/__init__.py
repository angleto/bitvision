"""Tool implementations exposed via MCP. One module per tool family.

Families:
- studies: get_study, get_series, describe_series, embed_series
- search: search_studies, similar_to
- search_advanced: semantic_search, search_hybrid
- tags: search_by_tags, list_tags (deterministic tag-based retrieval)
- annotations: get_annotations, list_reports
- clinical_notes: write_clinical_note, update_clinical_note, delete_clinical_note
- patients: get_patient, get_fascicolo_index, get_patient_timeline, list_patient_documents
- images: get_series_thumbnail, get_study_thumbnails (base64 JPEG → LLM vision)
- bundle: get_fascicolo_bundle (markdown + JSON aggregate)
- summaries: summarize (polymorphic series/study/patient summary)
- clinical_events: ClinicalEvent CRUD + propose/confirm document links
- documents: document ingest / merge / split / source download
- report_contents: ReportContent + canonical synthesis lifecycle
- provenance: provenance chain readers
- external_identifiers: cross-patient lookup + linker
"""

from bvmcp.tools import (
    annotations,
    bundle,
    clinical_events,
    clinical_notes,
    documents,
    external_identifiers,
    images,
    patients,
    provenance,
    report_contents,
    search,
    search_advanced,
    studies,
    summaries,
    tags,
)

__all__ = [
    "annotations",
    "bundle",
    "clinical_events",
    "clinical_notes",
    "documents",
    "external_identifiers",
    "images",
    "patients",
    "provenance",
    "report_contents",
    "search",
    "search_advanced",
    "studies",
    "summaries",
    "tags",
]
