"""MCP scope catalog — single source of truth for granular permissions.

Each MCP tool declares one scope. The HTTP transport refuses the call
when the principal's bearer scope set does not contain it. Scopes are
issued per-assistant in phoenix's *Settings → AI assistants* page; an
operator can grant a narrow read-only assistant just the ``*:read``
family while a full clinical writer-assistant carries the broader
write set.

Naming convention: ``<resource>:<verb>`` where verb is one of
``read`` / ``write`` / a domain-specific action (``download``,
``endorse``, ``sign``, ``identify``). The catalog is intentionally
flat — no scope hierarchy, no implicit inclusion (``*:write`` does
NOT imply ``*:read``).

Sensitivity flags surface in the UI to warn operators when granting
a scope that exfiltrates PHI or crosses patient boundaries.

Hard gates: ``synthesis:sign`` is structurally HUMAN-ONLY at the
backend layer (the ``/sign`` endpoint refuses any request carrying
``request.state.agent_token_id``). The scope exists in the catalog
for completeness — granting it to an assistant has no effect because
the backend rejects the agent's request anyway. The UI should mark
the scope as ungrantable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScopeDef:
    id: str
    description: str
    sensitive: bool = False
    human_only: bool = False


SCOPE_CATALOG: tuple[ScopeDef, ...] = (
    # --- Patient identity + demographics --------------------------------------
    ScopeDef("patients:read", "Read patient demographics, contacts, identifiers"),
    ScopeDef("patients:write", "Create / update patient demographics + contacts"),
    ScopeDef(
        "patients:identify",
        "Add / remove external_identifiers entries",
        sensitive=True,
    ),
    # --- Clinical events ------------------------------------------------------
    ScopeDef("events:read", "Read clinical_events, list per patient"),
    ScopeDef(
        "events:write",
        "Create non-imaging clinical events (visit, procedure, admission, "
        "lab batch). Also covers status transitions (confirm/reschedule/"
        "complete/cancel/mark-missed) since each transition is an audit "
        "anchor on the event row itself.",
    ),
    # --- Calendar (planning + ICS subscription) -------------------------------
    ScopeDef(
        "calendar:read",
        "Read the patient calendar feed (planned + completed events) and "
        "export ICS for personal calendar subscription",
    ),
    ScopeDef(
        "calendar:write",
        "Import ICS files into a patient calendar (dry-run + commit)",
    ),
    ScopeDef(
        "calendar:sync",
        "Bind external calendar providers (Google, Apple, CalDAV) to a "
        "patient or user and run bidirectional sync. Reserved for step 4 "
        "of the calendar roadmap.",
        sensitive=True,
    ),
    ScopeDef(
        "calendar:subscribe",
        "Mint or revoke a PUBLIC, revocable iCal subscription URL for a "
        "patient (an HMAC-signed feed an external calendar app can poll "
        "with no login). Sensitive: the URL exposes the patient calendar "
        "to anyone who holds it, exactly like a share-link.",
        sensitive=True,
    ),
    # --- Documents (Manifestation) --------------------------------------------
    ScopeDef("documents:read", "List + read document metadata + OCR text"),
    ScopeDef("documents:write", "Update document metadata + content"),
    ScopeDef("documents:ingest", "Upload new documents into a patient fascicolo"),
    ScopeDef(
        "documents:download",
        "Proxy-download the original blob (PDF, DICOM ISO, scan, photo)",
        sensitive=True,
    ),
    ScopeDef("documents:delete", "Soft-delete + restore documents"),
    ScopeDef("documents:merge", "Merge / split document aliases"),
    # --- Patient inbound inbox (fbbf5270) --------------------------------------
    ScopeDef(
        "inbox:read",
        "List + read the patient inbox review queue (staged items, manifests, auto-check verdicts)",
    ),
    ScopeDef(
        "inbox:manage",
        "Mint / relabel / revoke inbound-email capability addresses and "
        "configure trusted senders. Sensitive: an address is a bearer "
        "capability to deliver into the patient's review queue, and a "
        "trusted sender bypasses human review on a clean pass.",
        sensitive=True,
    ),
    ScopeDef(
        "inbox:review",
        "Accept / reject staged inbox items. Accepting writes into the "
        "fascicolo (documents + studies); the review profile is "
        "agent_capable by design, with agent provenance stamped on every "
        "transition.",
        sensitive=True,
    ),
    # --- Public contributions (OpenData publish quarantine) --------------------
    ScopeDef(
        "contributions:read",
        "List + read the public-contribution review queue (submission status + "
        "auto-check verdicts: header de-id, burned-in-pixel risk, malware, CSAM).",
    ),
    ScopeDef(
        "contributions:review",
        "Reject public-contribution submissions to the OpenData library. "
        "Accepting (publishing PHI-bearing imaging to the public web) is "
        "human-only by design and refused for agent actors server-side — there "
        "is no agent-accept path.",
        sensitive=True,
        human_only=True,
    ),
    # --- Report contents (Expression) -----------------------------------------
    ScopeDef("reports:read", "Read report_contents, list per event, citations"),
    ScopeDef(
        "reports:write",
        "Extract a report_content from a document, update content + structured fields",
    ),
    ScopeDef(
        "reports:endorse",
        "Mark an extracted_auto report_content as endorsed (clinician validation)",
        sensitive=True,
    ),
    # --- Canonical synthesis ("Referto BitVision") ----------------------------
    ScopeDef(
        "synthesis:write",
        "Draft / final / reject / supersede a canonical_synthesis report_content",
    ),
    ScopeDef(
        "synthesis:sign",
        "Apply the legally-binding signature to a canonical_synthesis. "
        "STRUCTURALLY HUMAN-ONLY: the backend refuses agent tokens regardless "
        "of scope grant.",
        sensitive=True,
        human_only=True,
    ),
    # --- Provenance lineage ---------------------------------------------------
    ScopeDef(
        "provenance:read",
        "Read append-only lineage of an artefact (audit / explanation)",
    ),
    # --- Cross-patient lookup -------------------------------------------------
    ScopeDef(
        "lookup:external",
        "Lookup patients carrying a given external_identifier (cross-patient, "
        "non-deterministic, returns 0..N candidates).",
        sensitive=True,
    ),
    # --- Sharing -------------------------------------------------------------
    # Mintaging a share-link delegates clinical access to the outside (email,
    # public token, optional autogen password). The backend already gates the
    # mint with enforce_agent_patient_scope + the owner check, but the blast
    # radius of a leaked agent token holding sharing:write is high enough to
    # warrant ``sensitive=True``: the operator must opt in explicitly when
    # consenting the assistant, same posture as ``qna:ask`` and
    # ``lookup:external``.
    ScopeDef(
        "sharing:write",
        "Create / list / update / revoke share-links on studies and folders. "
        "Mintaging a share exposes patient data to the outside.",
        sensitive=True,
    ),
    # --- Fascicolo / study / folder export + tokenised download ---------------
    # Enqueues a Job that bundles a patient's records (studies + DICOM +
    # reports + documents) into a downloadable ZIP and mints the
    # single-use download token that streams it off-platform. Sensitive:
    # the artifact is the patient's full PHI, including raw pixel data,
    # and the token is a bearer capability to fetch it. The backend
    # re-gates every enqueue on owner/admin + the agent's patient scope,
    # and download:dicom is still required for the pixel branch.
    ScopeDef(
        "fascicolo:export",
        "Export a patient / study / folder Health Record to a ZIP and mint "
        "the one-time download token. Egresses full PHI including DICOM.",
        sensitive=True,
    ),
    # The PHR-Bundle export is account-wide, not per-patient: it bundles
    # the WHOLE structured record the platform holds about the token's
    # owner (consents, studies metadata, reports, markers, every managed
    # patient + their documents, audit log) into the portable, versioned
    # open container documented in docs/phr-bundle.md. Distinct blast
    # radius from ``fascicolo:export`` (one patient, DICOM included), so
    # it gets its own grantable scope. No DICOM pixels are egressed.
    ScopeDef(
        "health_record:export",
        "Export the owner's full account as a portable PHR-Bundle (the open, "
        "versioned GDPR Art. 20 container). Egresses all structured PHI text; "
        "no DICOM pixels.",
        sensitive=True,
    ),
    # --- Annotations + markers ------------------------------------------------
    ScopeDef("annotations:read", "Read in-viewer markers / annotations"),
    ScopeDef("annotations:write", "Create / update / delete in-viewer markers + annotations"),
    # --- Findings (structured, coded clinical reperti) ------------------------
    ScopeDef("findings:read", "Read + search structured findings + their vocabulary"),
    ScopeDef("findings:write", "Create / update / delete / restore structured findings"),
    ScopeDef(
        "datasets:read",
        "Read training dataset info: build a cohort labels manifest; list the "
        "datasets your studies are in",
    ),
    ScopeDef("datasets:export", "Enqueue a training-cohort byte bundle (ZIP) export job"),
    # --- Pathology / WSI ------------------------------------------------------
    # Whole-slide imaging is a distinct artefact class from DICOM imaging,
    # so it gets its own read scope: an operator can grant a pathology
    # assistant slide access without also exposing the radiology series.
    # Slide ANNOTATIONS (polygons / counts) reuse the shared annotations:*
    # scopes (the Marker layer is one surface across radiology + pathology).
    ScopeDef(
        "pathology:read",
        "Read pathology whole-slide images: metadata, thumbnail, macro, "
        "and region crops (so a vision LLM can look at the tissue)",
    ),
    # --- Imaging --------------------------------------------------------------
    ScopeDef("imaging:read", "Read DICOM series metadata, slices, thumbnails"),
    ScopeDef(
        "imaging:compute",
        "Trigger expensive imaging compute (segmentations, registrations, "
        "embeddings, descriptive analyses)",
    ),
    # --- Tags + metadata ------------------------------------------------------
    ScopeDef("tags:read", "Read tags + tag aliases"),
    ScopeDef("tags:write", "Add / replace / remove tags on study / series / patient"),
    # Descriptive-metadata edits on the imaging hierarchy. Distinct from
    # ``tags:write`` (a tag is a searchable label) and from
    # ``imaging:compute`` (expensive derived-data production): these edit
    # the human-readable ``*_description`` / ``body_part`` display fields.
    # The DICOM-authoritative columns (UIDs, acquired modality) stay
    # read-only at the endpoint. A dedicated scope lets an operator grant
    # a metadata-tidying assistant exactly this, without tag or compute
    # privileges.
    ScopeDef(
        "studies:write_metadata",
        "Edit a study's safe descriptive fields (study_description). "
        "DICOM-authoritative fields stay read-only.",
    ),
    ScopeDef(
        "series:write_metadata",
        "Edit a series' safe descriptive fields (series_description, "
        "body_part_examined, corrected modality). DICOM-authoritative "
        "fields stay read-only.",
    ),
    # --- Folders (Google Drive-style hierarchical grouping) -------------------
    # Folders hold heterogeneous items (study / series / report / annotation /
    # document / consultation / sub-folder). They are the primary navigation
    # surface inside a fascicolo and the agent must be able to read + reshape
    # the tree to reorganise the patient's records. Folder *sharing* (which
    # mints cross-patient grants) stays human-only and lives off the
    # ``synthesis:sign``-style ``request.state.is_agent`` refusal at the
    # backend share endpoint.
    ScopeDef("folders:read", "List folders + read folder contents (items, subfolders)"),
    ScopeDef(
        "folders:write",
        "Create / rename / move / delete folders, add / remove items (reshape the fascicolo tree)",
    ),
    # --- Search ---------------------------------------------------------------
    ScopeDef("search:read", "Full-text + semantic search across visible patients"),
    # --- Q&A orchestrator -----------------------------------------------------
    # ``qna:ask`` is opt-in even when the assistant already holds
    # documents:read + patients:read because invoking the orchestrator
    # spends platform-paid LLM tokens. The operator should grant it
    # explicitly so a leaked agent token cannot drain the wallet.
    ScopeDef(
        "qna:ask",
        "Run the Q&A orchestrator (server-side LLM tool-use loop) for a patient. "
        "Spends platform-paid tokens billed against the user wallet.",
        sensitive=True,
    ),
    # --- Care phases (semantic timeline groupings) ----------------------------
    # Phases are persisted, patient-scoped semantic episodes (diagnosis,
    # surgery, follow-up, surveillance, ...). Read covers timeline /
    # phase / material / revisions; ``propose`` is dry-run-only LLM
    # classification (no DB write); ``write`` covers everything that
    # mutates the persisted phase set or its event assignments.
    ScopeDef("phases:read", "Read care timeline + phases + material + revisions"),
    ScopeDef(
        "phases:propose",
        "Run the LLM classifier to propose a phase partition (dry-run; no DB writes)",
    ),
    ScopeDef(
        "phases:write",
        "Apply / create / update / assign / unassign / reorder / restore care phases",
    ),
    # --- Notifications (outbound reminders, v3.5) ----------------------------
    ScopeDef(
        "notifications:read",
        "Read outbound notification dispatches: scheduled / sent / cancelled / failed.",
    ),
    ScopeDef(
        "notifications:write",
        "Configure per-contact channels + consent, cancel queued dispatches, "
        "send test notifications. Outbound side-effects: emails / webhooks "
        "to third parties; treat as sensitive even though we only target "
        "patient_contacts the operator already owns.",
        sensitive=True,
    ),
    # --- Patient tasks (operational checklist, v3.4) -------------------------
    # Tasks are private operational to-dos attached to a fascicolo
    # (impegnativa, pharmacy run, transport). Distinct from
    # clinical_events because they don't land in the clinical record /
    # FSE export. Two flat scopes mirroring the events:* pattern; no
    # narrower ``tasks:complete`` (the system has no scope hierarchy
    # and a "just spunta done" agent gets ``tasks:write`` whose blast
    # radius is bounded to operational state, not to clinical content).
    ScopeDef("tasks:read", "Read patient tasks (operational checklist) + transitions audit"),
    ScopeDef(
        "tasks:write",
        "Create / update / delete patient tasks + all FSM transitions "
        "(start / snooze / wake / complete / drop / reopen)",
    ),
    # --- Admin maintenance (platform-wide, owner must be admin) ---------------
    # The backend gates these endpoints with
    # ``require_admin_or_scoped_agent``: the assistant's OWNER must be a
    # platform admin AND the operator must have granted this scope. Bounded
    # to non-destructive embedding maintenance (coverage reads + enqueueing
    # idempotent (re)embed jobs); destructive admin surfaces stay
    # structurally human-only behind ``require_admin``. Sensitive because it
    # is platform-wide rather than patient-scoped.
    ScopeDef(
        "admin:embeddings",
        "Read embedding coverage (aggregate counts per model/kind) and enqueue "
        "missing/failed (re)embedding jobs, including per-model text-chunk "
        "re-embeds. Owner must be a platform admin.",
        sensitive=True,
    ),
)


SCOPE_BY_ID: dict[str, ScopeDef] = {s.id: s for s in SCOPE_CATALOG}


# Single-source mapping tool_name → scope_id. Each new MCP tool MUST
# register here; the dispatcher refuses to invoke a tool that has no
# entry (fail-closed).
TOOL_SCOPE: dict[str, str] = {
    # --- discovery / read ---
    "search_patients": "patients:read",
    "search_studies": "imaging:read",
    "search_hybrid": "search:read",
    "semantic_search": "search:read",
    "search_by_tags": "tags:read",
    "list_tags": "tags:read",
    # --- navigation / read ---
    "get_patient": "patients:read",
    "get_fascicolo_index": "patients:read",
    "get_patient_timeline": "patients:read",
    "list_patient_documents": "documents:read",
    "get_study": "imaging:read",
    "get_deidentification_provenance": "imaging:read",
    "get_series": "imaging:read",
    "get_series_dicom_meta": "imaging:read",
    "get_series_thumbnail": "imaging:read",
    "get_series_slice": "imaging:read",
    "get_study_thumbnails": "imaging:read",
    "describe_series": "imaging:read",
    # Pathology / WSI reads
    "list_pathology_slides": "pathology:read",
    "get_pathology_slide": "pathology:read",
    "get_slide_thumbnail": "pathology:read",
    "get_slide_macro": "pathology:read",
    "get_slide_region": "pathology:read",
    "list_reports": "reports:read",
    "get_document_text": "documents:read",
    "get_document_references": "documents:read",
    "find_documents_by_content_hash": "documents:read",
    "download_document_binary": "documents:download",
    "get_fascicolo_bundle": "patients:read",
    "get_lab_timeseries": "patients:read",
    "get_annotations": "annotations:read",
    "get_segmentations": "imaging:read",
    "get_registration": "imaging:read",
    "get_suv": "imaging:read",
    "crop_series_roi": "imaging:read",
    # --- writes ---
    "update_patient": "patients:write",
    "add_patient_contact": "patients:write",
    "remove_patient_contact": "patients:write",
    "decode_codice_fiscale": "patients:read",  # parser, no DB write
    "update_document": "documents:write",
    "bulk_update_documents": "documents:write",
    "delete_document": "documents:delete",
    "restore_document": "documents:delete",
    "merge_documents": "documents:merge",
    "link_document_to_study": "documents:write",  # legacy; v3 successor TBD
    "unlink_document_from_study": "documents:write",
    "write_annotation": "annotations:write",
    "update_annotation": "annotations:write",
    "delete_annotation": "annotations:write",
    "restore_annotation": "annotations:write",
    "get_annotation_revisions": "annotations:read",
    "get_finding_vocab": "findings:read",
    "search_findings": "findings:read",
    "get_finding": "findings:read",
    "get_finding_revisions": "findings:read",
    "create_finding": "findings:write",
    "update_finding": "findings:write",
    "delete_finding": "findings:write",
    "restore_finding": "findings:write",
    "add_finding_geometry": "findings:write",
    "promote_finding_measurement": "findings:write",
    # Lesion tracks — longitudinal follow-up over findings; reads/writes
    # ride the findings scopes (a track is a view over the diagnosis layer).
    "list_lesion_tracks": "findings:read",
    "get_lesion_track": "findings:read",
    "get_lesion_trajectory": "findings:read",
    "get_lesion_track_revisions": "findings:read",
    "create_lesion_track": "findings:write",
    "update_lesion_track": "findings:write",
    "delete_lesion_track": "findings:write",
    "restore_lesion_track": "findings:write",
    "add_finding_to_track": "findings:write",
    "remove_finding_from_track": "findings:write",
    # Propagation triggers an expensive worker but its consequential effect
    # is a medical Finding write, so it is gated on findings:write.
    "propagate_lesion": "findings:write",
    # Response assessments (RECIST roll-up over the findings layer).
    "list_response_assessments": "findings:read",
    "get_response_assessment": "findings:read",
    "get_response_assessment_revisions": "findings:read",
    "compute_response_assessment": "findings:write",
    "recompute_response_assessment": "findings:write",
    "update_response_assessment": "findings:write",
    "delete_response_assessment": "findings:write",
    "restore_response_assessment": "findings:write",
    "export_training_manifest": "datasets:read",
    "export_training_cohort_bundle": "datasets:export",
    "list_my_datasets": "datasets:read",
    "write_clinical_note": "annotations:write",
    "update_clinical_note": "annotations:write",
    "delete_clinical_note": "annotations:write",
    "embed_series": "imaging:compute",
    "register_series": "imaging:compute",
    # Segmentation writes — model-driven mask production. Auto + interactive
    # land under ``imaging:compute`` (they trigger expensive worker passes
    # and the platform pays the inference cost). External mask upload is
    # data-only and rides ``annotations:write`` to match the backend
    # ``WRITE_ANNOTATIONS`` permission used by the upload endpoint.
    "auto_segment_series": "imaging:compute",
    "predict_segmentation_interactive": "imaging:compute",
    "upload_segmentation": "annotations:write",
    "delete_segmentation": "annotations:write",
    "measure_distance": "imaging:read",
    "measure_volume": "imaging:read",
    "find_hot_spots": "imaging:read",
    "compute_roi_stats": "imaging:read",
    # Multiphase contrast-CT acquisition phases. Reading the manifest is a
    # metadata read; running the classifier is a "descriptive analysis"
    # that persists derived labels, so it rides imaging:compute (NOT the
    # care-timeline phases:* scope).
    "list_study_phases": "imaging:read",
    "detect_study_phases": "imaging:compute",
    "set_series_acquisition_phase": "imaging:compute",
    "compute_phase_washout": "imaging:read",
    "compute_washout_map": "imaging:read",
    # Persisted wash-out measurements are a structured imaging finding, so
    # they ride the findings:* scopes (like lesion_tracks / response_assessments).
    "create_phase_enhancement_set": "findings:write",
    "list_phase_enhancement_sets": "findings:read",
    "get_phase_enhancement_set": "findings:read",
    "delete_phase_enhancement_set": "findings:write",
    "restore_phase_enhancement_set": "findings:write",
    "add_tag_to_study": "tags:write",
    "remove_tag_from_study": "tags:write",
    "replace_study_tags": "tags:write",
    "update_series_metadata": "series:write_metadata",
    "update_study_metadata": "studies:write_metadata",
    "summarize": "search:read",
    "extract_document_entities": "reports:write",
    "similar_to": "search:read",
    # --- v3 tool surface (phase 3d) ----------------------------------
    # Discovery + Navigation:
    "find_clinical_events": "events:read",
    "get_event": "events:read",
    "create_clinical_event": "events:write",
    "update_clinical_event": "events:write",
    "delete_clinical_event": "events:write",
    # ClinicalEvent binary attachments — sub-resource of the event, so
    # writes ride ``events:write`` (the agent gets the whole event
    # lifecycle under one scope). Reads ride ``events:read`` and the
    # binary download path is auth-checked + bucket-isolated so the
    # storage_key never leaks to the caller.
    "upload_clinical_event_attachment": "events:write",
    "list_clinical_event_attachments": "events:read",
    "download_clinical_event_attachment": "events:read",
    "delete_clinical_event_attachment": "events:write",
    "promote_clinical_event_attachment": "events:write",
    # Event ↔ curated drive Document links. Linking/unlinking a document
    # to the event is part of the event's surface, so it rides
    # ``events:write``; the list is ``events:read``. The document the
    # link points at is still gated by the patient read/write the
    # backend enforces on top.
    "link_event_document": "events:write",
    "list_event_documents": "events:read",
    "unlink_event_document": "events:write",
    # --- Sharing (sensitive scope; see SCOPE_CATALOG sharing:write) ----
    "create_study_share_link": "sharing:write",
    "create_folder_share_link": "sharing:write",
    "list_share_links": "sharing:write",
    "update_share_link": "sharing:write",
    "revoke_share_link": "sharing:write",
    # --- Fascicolo export + tokenised download --------------------------
    # The four enqueue tools + the download-token mint egress full PHI,
    # so they ride the sensitive ``fascicolo:export`` scope. ``get_job``
    # is a read-only status poll (ownership re-gated server-side, 404 on
    # foreign jobs) and rides the lowest-privilege ``patients:read`` so
    # any session that kicked off an export can watch it finish.
    "export_fascicolo": "fascicolo:export",
    "export_study": "fascicolo:export",
    "export_folder": "fascicolo:export",
    "bulk_download": "fascicolo:export",
    "issue_download_token": "fascicolo:export",
    "get_job": "patients:read",
    # Account-wide portable PHR-Bundle (GDPR Art. 20). Its own scope so
    # an operator can grant "give me my data back" without also handing
    # over per-patient DICOM egress.
    "export_health_record_bundle": "health_record:export",
    # --- Calendar transitions (FSM-checked sub-resources) ----------------
    "confirm_event": "events:write",
    "reschedule_event": "events:write",
    "complete_event": "events:write",
    "cancel_event": "events:write",
    "mark_event_missed": "events:write",
    # --- Calendar discovery + feed ---------------------------------------
    "find_upcoming_events": "events:read",
    "find_overdue_events": "events:read",
    "find_events_by_date_range": "events:read",
    "export_calendar_ics": "calendar:read",
    "export_event_ics": "calendar:read",
    "list_calendar_subscriptions": "calendar:read",
    "create_calendar_subscription": "calendar:subscribe",
    "revoke_calendar_subscription": "calendar:subscribe",
    "propose_event_link": "documents:read",
    "confirm_event_link": "reports:write",
    "find_reports": "reports:read",
    "get_report_content": "reports:read",
    "get_provenance_chain": "provenance:read",
    "lookup_external_identifier": "lookup:external",
    # Ingestion:
    "extract_report_content": "reports:write",
    "link_external_identifier": "patients:identify",
    # Synthesis:
    "create_canonical_referto": "synthesis:write",
    "cite_source": "reports:write",
    "endorse_report_content": "reports:endorse",
    "reject_report_content": "synthesis:write",
    "supersede_report_content": "synthesis:write",
    "update_report_content": "reports:write",
    # Document operations:
    "ingest_document": "documents:ingest",
    # Resumable upload sessions (DESIGN.md §11.6) — all under documents:ingest;
    # get_upload_session is read-only but there is no separate upload-read scope.
    "create_upload_session": "documents:ingest",
    "upload_session_chunk": "documents:ingest",
    "get_upload_session": "documents:ingest",
    "commit_upload_session": "documents:ingest",
    "abort_upload_session": "documents:ingest",
    "merge_aliases": "documents:merge",
    "split_alias": "documents:merge",
    "download_source_document": "documents:download",
    # Folders (Google Drive-style hierarchical grouping):
    "list_folders": "folders:read",
    "get_folder": "folders:read",
    "create_folder": "folders:write",
    "update_folder": "folders:write",
    "delete_folder": "folders:write",
    "add_item_to_folder": "folders:write",
    "remove_item_from_folder": "folders:write",
    # Care phases (semantic timeline groupings):
    "get_care_timeline": "phases:read",
    "render_care_timeline_svg": "phases:read",
    "get_care_phase": "phases:read",
    "list_care_phase_material": "phases:read",
    "list_care_phase_revisions": "phases:read",
    "propose_care_phases": "phases:propose",
    "apply_phase_proposal": "phases:write",
    "create_care_phase": "phases:write",
    "update_care_phase": "phases:write",
    "assign_event_to_phase": "phases:write",
    "unassign_event_from_phase": "phases:write",
    "reorder_care_phases": "phases:write",
    "restore_care_phase_revision": "phases:write",
    "list_care_phases": "phases:read",
    "get_care_timeline_health": "phases:read",
    "delete_care_phase": "phases:write",
    "export_care_timeline_ics": "phases:read",
    "export_care_timeline_pdf": "phases:read",
    # Caller introspection: scope-free (any authenticated session can
    # ask "what scopes do I hold?"). Mapped to ``patients:read`` as
    # the lowest-privilege scope we already require for any session.
    "get_my_scopes": "patients:read",
    # Inline documentation: returns purely static Markdown guides
    # (mention DSL, agent-writes contract, scopes overview). No PHI,
    # no runtime state. Mapped to ``patients:read`` for the same
    # reason as ``get_my_scopes``: every authenticated session holds
    # it, and the tool must be discoverable / invokable regardless
    # of which write scopes the assistant carries.
    "help": "patients:read",
    # --- Q&A orchestrator (M6 of the patient Q&A plan) -------------
    # The high-level tool runs the server-side agent loop and is
    # billed against the user wallet; the read-only chunk search is
    # mapped to documents:read since it returns excerpts of patient
    # content the assistant already holds the right to read.
    "ask_about_patient": "qna:ask",
    "search_text_chunks": "documents:read",
    # Notifications (v3.5)
    "list_notification_dispatches": "notifications:read",
    "cancel_pending_dispatch": "notifications:write",
    "configure_contact_channel": "notifications:write",
    "revoke_consent": "notifications:write",
    "send_test_notification": "notifications:write",
    "start_telegram_link": "notifications:write",
    "check_telegram_link": "notifications:read",
    "unlink_telegram": "notifications:write",
    # Patient tasks (v3.4 — operational checklist alongside clinical events):
    "list_patient_tasks": "tasks:read",
    "get_patient_task": "tasks:read",
    "find_overdue_tasks": "tasks:read",
    "find_tasks_due_today": "tasks:read",
    "create_patient_task": "tasks:write",
    "update_patient_task": "tasks:write",
    "delete_patient_task": "tasks:write",
    "restore_patient_task": "tasks:write",
    "start_task": "tasks:write",
    "snooze_task": "tasks:write",
    "wake_task": "tasks:write",
    "complete_task": "tasks:write",
    "drop_task": "tasks:write",
    "reopen_task": "tasks:write",
    "assign_task_to_contact": "tasks:write",
    # Calendar export per single task — read-only, so the more lenient
    # tasks:read scope (NOT calendar:read) so an assistant with only
    # the task-checklist scope can still hand the user a downloadable
    # invite. Mirrors export_event_ics but rides ``tasks:read`` rather
    # than ``calendar:read`` because tasks are not on the calendar feed.
    "export_task_ics": "tasks:read",
    # --- Embeddings admin (MCP-GUI parity for /admin/embeddings) -------
    # All five ride the single admin:embeddings scope: the surface is
    # one capability (embedding maintenance) and the backend re-gates
    # every call on the owner being a platform admin.
    "get_embedding_coverage": "admin:embeddings",
    "get_text_embedding_coverage": "admin:embeddings",
    "retry_failed_embeddings": "admin:embeddings",
    "embed_missing_targets": "admin:embeddings",
    "reembed_text_chunks": "admin:embeddings",
    # --- Patient inbound inbox (fbbf5270 §12) ------------------------
    "list_inbox_items": "inbox:read",
    "get_inbox_item": "inbox:read",
    "list_patient_inbox_addresses": "inbox:manage",
    "create_inbox_address": "inbox:manage",
    "set_inbox_address_label": "inbox:manage",
    "revoke_inbox_address": "inbox:manage",
    "configure_trusted_senders": "inbox:manage",
    "accept_inbox_item": "inbox:review",
    "reject_inbox_item": "inbox:review",
    # --- Public contributions (OpenData publish quarantine) ----------
    "list_contribution_queue": "contributions:read",
    "get_contribution": "contributions:read",
    "reject_contribution": "contributions:review",
}


def scope_for_tool(tool_name: str) -> str | None:
    """Return the scope id required to invoke the tool, or None if the
    tool is not in the catalog (which the dispatcher treats as a
    fail-closed condition: unknown tool = unauthorised)."""
    return TOOL_SCOPE.get(tool_name)


def scope_is_sensitive(scope_id: str) -> bool:
    s = SCOPE_BY_ID.get(scope_id)
    return bool(s and s.sensitive)


def scope_is_human_only(scope_id: str) -> bool:
    s = SCOPE_BY_ID.get(scope_id)
    return bool(s and s.human_only)


__all__ = [
    "SCOPE_BY_ID",
    "SCOPE_CATALOG",
    "TOOL_SCOPE",
    "ScopeDef",
    "scope_for_tool",
    "scope_is_human_only",
    "scope_is_sensitive",
]
