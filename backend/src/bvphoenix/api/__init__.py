"""HTTP API surface. Each module exposes an APIRouter; main.py wires them."""

from fastapi import APIRouter

from bvphoenix.api import a2a as a2a_routes
from bvphoenix.api import admin_llm_prompts as admin_llm_prompts_routes
from bvphoenix.api import admin_llm_rates as admin_llm_rates_routes
from bvphoenix.api import admin_users as admin_users_routes
from bvphoenix.api import ai_assistants as ai_assistants_routes
from bvphoenix.api import ai_tier as ai_tier_routes
from bvphoenix.api import app_settings as app_settings_routes
from bvphoenix.api import audit as audit_routes
from bvphoenix.api import auth as auth_routes
from bvphoenix.api import bulk as bulk_routes
from bvphoenix.api import bulk_upload as bulk_upload_routes
from bvphoenix.api import calendar as calendar_routes
from bvphoenix.api import care_phases as care_phases_routes
from bvphoenix.api import (
    clinical_event_attachments as clinical_event_attachments_routes,
)
from bvphoenix.api import clinical_events as clinical_events_routes
from bvphoenix.api import clinical_notes as clinical_notes_routes
from bvphoenix.api import consultations_compat as consultations_compat_routes
from bvphoenix.api import contributions as contributions_routes
from bvphoenix.api import credits as credits_routes
from bvphoenix.api import dicom_upload as dicom_upload_routes
from bvphoenix.api import dicomweb as dicomweb_routes
from bvphoenix.api import display_metadata as display_metadata_routes
from bvphoenix.api import docs as docs_routes
from bvphoenix.api import document_catalog as document_catalog_routes
from bvphoenix.api import documents as documents_routes
from bvphoenix.api import duc as duc_routes
from bvphoenix.api import embeddings_admin as embeddings_admin_routes
from bvphoenix.api import external_identifiers as external_identifiers_routes
from bvphoenix.api import fhir as fhir_routes
from bvphoenix.api import findings as findings_routes
from bvphoenix.api import folders as folders_routes
from bvphoenix.api import gdpr as gdpr_routes
from bvphoenix.api import governance as governance_routes
from bvphoenix.api import history as history_routes
from bvphoenix.api import inbox as inbox_routes
from bvphoenix.api import internal_auth as internal_auth_routes
from bvphoenix.api import internal_inbound_email as internal_inbound_email_routes
from bvphoenix.api import jobs as jobs_routes
from bvphoenix.api import lesion_tracks as lesion_tracks_routes
from bvphoenix.api import llm_stream as llm_stream_routes
from bvphoenix.api import markers as markers_routes
from bvphoenix.api import me as me_routes
from bvphoenix.api import me_ai_models as me_ai_models_routes
from bvphoenix.api import measurements as measurements_routes
from bvphoenix.api import mfa as mfa_routes
from bvphoenix.api import notifications as notifications_routes
from bvphoenix.api import pathology as pathology_routes
from bvphoenix.api import patient_export as patient_export_routes
from bvphoenix.api import patient_tasks as patient_tasks_routes
from bvphoenix.api import patient_tree as patient_tree_routes
from bvphoenix.api import patients as patients_routes
from bvphoenix.api import payouts as payouts_routes
from bvphoenix.api import pet_mip as pet_mip_routes
from bvphoenix.api import pet_voi as pet_voi_routes
from bvphoenix.api import proposals as proposals_routes
from bvphoenix.api import provenance as provenance_routes
from bvphoenix.api import qna as qna_routes
from bvphoenix.api import report_contents as report_contents_routes
from bvphoenix.api import response_assessments as response_assessments_routes
from bvphoenix.api import search as search_routes
from bvphoenix.api import search_chunks as search_chunks_routes
from bvphoenix.api import search_hybrid as search_hybrid_routes
from bvphoenix.api import search_semantic as search_semantic_routes
from bvphoenix.api import segmentations as segmentations_routes
from bvphoenix.api import sharing as sharing_routes
from bvphoenix.api import sponsorships as sponsorships_routes
from bvphoenix.api import storage as storage_routes
from bvphoenix.api import studies as studies_routes
from bvphoenix.api import summaries as summaries_routes
from bvphoenix.api import tags as tags_routes
from bvphoenix.api import training_exports as training_exports_routes
from bvphoenix.api import transparency as transparency_routes
from bvphoenix.api import upload_sessions as upload_sessions_routes
from bvphoenix.api import user_api_keys as user_api_keys_routes
from bvphoenix.api import version as version_routes
from bvphoenix.api import viewport_state as viewport_state_routes
from bvphoenix.auth import oidc as oidc_routes

api_router = APIRouter(prefix="/api")
api_router.include_router(version_routes.router)
api_router.include_router(auth_routes.router)
api_router.include_router(mfa_routes.router)
api_router.include_router(oidc_routes.router)
api_router.include_router(studies_routes.router)
api_router.include_router(search_routes.router)
api_router.include_router(search_chunks_routes.router)
api_router.include_router(search_hybrid_routes.router)
api_router.include_router(search_semantic_routes.router)
api_router.include_router(tags_routes.router)
api_router.include_router(tags_routes.router_writes)
api_router.include_router(measurements_routes.router)
api_router.include_router(llm_stream_routes.router)
api_router.include_router(sharing_routes.router)
api_router.include_router(folders_routes.router)
api_router.include_router(clinical_events_routes.router)
api_router.include_router(clinical_event_attachments_routes.router)
api_router.include_router(patient_tasks_routes.router)
api_router.include_router(notifications_routes.router)
api_router.include_router(care_phases_routes.router)
api_router.include_router(calendar_routes.router)
api_router.include_router(me_routes.router)
api_router.include_router(me_ai_models_routes.router)
api_router.include_router(consultations_compat_routes.router)
api_router.include_router(report_contents_routes.router)
api_router.include_router(external_identifiers_routes.router)
api_router.include_router(provenance_routes.router)
api_router.include_router(qna_routes.router)
api_router.include_router(ai_tier_routes.router)
api_router.include_router(storage_routes.router)
api_router.include_router(sponsorships_routes.router)
api_router.include_router(documents_routes.router)
api_router.include_router(document_catalog_routes.router)
api_router.include_router(patients_routes.router)
api_router.include_router(proposals_routes.router)
api_router.include_router(history_routes.router)
api_router.include_router(jobs_routes.router)
api_router.include_router(patient_tree_routes.router)
api_router.include_router(patient_export_routes.router)
api_router.include_router(pathology_routes.router)
api_router.include_router(gdpr_routes.router)
api_router.include_router(a2a_routes.router)
api_router.include_router(ai_assistants_routes.router)
api_router.include_router(inbox_routes.router)
api_router.include_router(contributions_routes.router)
api_router.include_router(internal_auth_routes.router)
api_router.include_router(internal_inbound_email_routes.router)
api_router.include_router(dicom_upload_routes.router)
api_router.include_router(dicomweb_routes.router)
api_router.include_router(fhir_routes.router)
api_router.include_router(display_metadata_routes.router)
api_router.include_router(segmentations_routes.router)
api_router.include_router(viewport_state_routes.router)
api_router.include_router(audit_routes.router)
api_router.include_router(clinical_notes_routes.router)
api_router.include_router(pet_voi_routes.router)
api_router.include_router(pet_mip_routes.router)
api_router.include_router(embeddings_admin_routes.router)
api_router.include_router(summaries_routes.router)
api_router.include_router(bulk_upload_routes.router)
api_router.include_router(upload_sessions_routes.router)
api_router.include_router(bulk_routes.router)
api_router.include_router(transparency_routes.router)
api_router.include_router(governance_routes.router)
api_router.include_router(user_api_keys_routes.router)
api_router.include_router(credits_routes.router)
api_router.include_router(duc_routes.router)
api_router.include_router(payouts_routes.router)
api_router.include_router(markers_routes.router)
api_router.include_router(findings_routes.router)
api_router.include_router(lesion_tracks_routes.router)
api_router.include_router(response_assessments_routes.router)
api_router.include_router(training_exports_routes.router)
api_router.include_router(app_settings_routes.router)
api_router.include_router(admin_users_routes.router)
api_router.include_router(admin_llm_rates_routes.router)
api_router.include_router(admin_llm_prompts_routes.router)
# Authenticated OpenAPI docs (Swagger / ReDoc / schema). Mounted under
# the ``/api`` prefix so the ingress routes them to the backend; the
# FastAPI defaults at ``/docs`` / ``/openapi.json`` are disabled in
# ``main.py`` (they fell outside ``/api`` and 404'd in production).
api_router.include_router(docs_routes.router)

__all__ = ["api_router"]
