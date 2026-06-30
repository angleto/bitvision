"""Single source of truth for the MCP tool registry.

Both transports (``server.py`` for stdio, ``server_http.py`` for the
HTTP transport that Claude.ai connectors use) import the same
``TOOL_MODULES`` tuple and derive their ``ALL_TOOLS`` /
``_HANDLERS`` from it. Adding a tool family means a one-line edit
here; both transports pick it up by construction.

Pre-2026-05-03 the two transports kept independent ``_TOOL_MODULES``
tuples that drifted: ``care_phases`` was registered on stdio but
NOT on HTTP, so the Claude.ai connector listed 13 phase tools (as
seen by ``stdio_server.ALL_TOOLS``) but the agent's ``tool_search``
returned zero hits because the HTTP transport never advertised
them. Same drift would have hit any future tool added with a single
edit.

This module exists to keep that class of bug structurally
impossible. New tool family → ``_tool_registry.TOOL_MODULES``,
done. The transports re-derive everything else.
"""

from __future__ import annotations

from bvmcp.tools import annotations as annotations_tools
from bvmcp.tools import bundle as bundle_tools
from bvmcp.tools import calendar_subscriptions as calendar_subscriptions_tools
from bvmcp.tools import care_phases as care_phases_tools
from bvmcp.tools import clinical_event_attachments as clinical_event_attachments_tools
from bvmcp.tools import clinical_events as clinical_events_tools
from bvmcp.tools import clinical_notes as clinical_notes_tools
from bvmcp.tools import consent as consent_tools
from bvmcp.tools import contrast_phases as contrast_phases_tools
from bvmcp.tools import contributions as contributions_tools
from bvmcp.tools import datasets as datasets_tools
from bvmcp.tools import document_reads as document_reads_tools
from bvmcp.tools import document_writes as document_writes_tools
from bvmcp.tools import documents as documents_tools
from bvmcp.tools import documents_upload as documents_upload_tools
from bvmcp.tools import embeddings_admin as embeddings_admin_tools
from bvmcp.tools import entities as entities_tools
from bvmcp.tools import exports as exports_tools
from bvmcp.tools import external_identifiers as external_identifiers_tools
from bvmcp.tools import findings as findings_tools
from bvmcp.tools import folders as folders_tools
from bvmcp.tools import help as help_tools
from bvmcp.tools import images as images_tools
from bvmcp.tools import imaging as imaging_tools
from bvmcp.tools import inbox as inbox_tools
from bvmcp.tools import labs as labs_tools
from bvmcp.tools import lesion_tracks as lesion_tracks_tools
from bvmcp.tools import metadata_writes as metadata_writes_tools
from bvmcp.tools import notifications as notifications_tools
from bvmcp.tools import pathology as pathology_tools
from bvmcp.tools import patient_tasks as patient_tasks_tools
from bvmcp.tools import patient_writes as patient_writes_tools
from bvmcp.tools import patients as patients_tools
from bvmcp.tools import provenance as provenance_tools
from bvmcp.tools import qna as qna_tools
from bvmcp.tools import report_contents as report_contents_tools
from bvmcp.tools import response_assessments as response_assessments_tools
from bvmcp.tools import search as search_tools
from bvmcp.tools import search_advanced as search_advanced_tools
from bvmcp.tools import segmentations as segmentations_tools
from bvmcp.tools import sharing as sharing_tools
from bvmcp.tools import studies as studies_tools
from bvmcp.tools import summaries as summaries_tools
from bvmcp.tools import tags as tags_tools
from bvmcp.tools import training as training_tools

TOOL_MODULES = (
    help_tools,
    studies_tools,
    search_tools,
    search_advanced_tools,
    tags_tools,
    annotations_tools,
    clinical_notes_tools,
    patients_tools,
    patient_writes_tools,
    images_tools,
    bundle_tools,
    summaries_tools,
    document_writes_tools,
    document_reads_tools,
    entities_tools,
    folders_tools,
    labs_tools,
    imaging_tools,
    pathology_tools,
    contrast_phases_tools,
    findings_tools,
    lesion_tracks_tools,
    response_assessments_tools,
    segmentations_tools,
    sharing_tools,
    calendar_subscriptions_tools,
    metadata_writes_tools,
    clinical_events_tools,
    clinical_event_attachments_tools,
    patient_tasks_tools,
    notifications_tools,
    documents_tools,
    documents_upload_tools,
    exports_tools,
    consent_tools,
    external_identifiers_tools,
    provenance_tools,
    report_contents_tools,
    care_phases_tools,
    qna_tools,
    training_tools,
    embeddings_admin_tools,
    inbox_tools,
    contributions_tools,
    datasets_tools,
)


__all__ = ["TOOL_MODULES"]
