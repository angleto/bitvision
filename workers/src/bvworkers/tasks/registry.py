"""Central list of task functions exposed to Arq.

Add new tasks here as they are implemented. Keeping the list in one place
makes it trivial to audit what a worker can do.
"""

from bvworkers.tasks.autotag_target import autotag_target
from bvworkers.tasks.bulk_document_update import bulk_document_update
from bvworkers.tasks.care_phase_propose import propose_care_phases
from bvworkers.tasks.chunk_and_embed import (
    chunk_and_embed_clinical_note,
    chunk_and_embed_document,
    chunk_and_embed_report_content,
    chunk_and_embed_summary,
)
from bvworkers.tasks.cleanup_jobs import cleanup_expired_jobs
from bvworkers.tasks.cleanup_upload_sessions import cleanup_upload_sessions
from bvworkers.tasks.deidentify_reindex import deidentify_reindex_study
from bvworkers.tasks.dispatch_notification import (
    dispatch_notification,
    notification_safety_net,
)
from bvworkers.tasks.embed_bge_m3 import embed_bge_m3_all, embed_bge_m3_dense
from bvworkers.tasks.embed_series import embed_series
from bvworkers.tasks.embed_text import embed_text_target
from bvworkers.tasks.embed_text_multilingual import embed_text_ml
from bvworkers.tasks.entity_extraction import extract_document_entities
from bvworkers.tasks.export_gdpr import export_gdpr_zip
from bvworkers.tasks.export_patient import export_patient_zip
from bvworkers.tasks.export_study import export_study_zip
from bvworkers.tasks.generate_summary import generate_summary
from bvworkers.tasks.inbox import (
    inbox_maintenance,
    process_inbound_email,
    promote_inbox_item,
)
from bvworkers.tasks.ingest_bulk import ingest_bulk_files
from bvworkers.tasks.ocr import run_document_ocr
from bvworkers.tasks.pack_entity_objects import pack_entity_objects_task
from bvworkers.tasks.pack_volume import pack_volume
from bvworkers.tasks.ping import ping
from bvworkers.tasks.prefetch_series import prefetch_series
from bvworkers.tasks.propagate_lesion import propagate_lesion
from bvworkers.tasks.purge_documents import purge_expired_documents
from bvworkers.tasks.registration import register_series
from bvworkers.tasks.reindex_batch import reindex_batch
from bvworkers.tasks.review_checks import run_review_checks
from bvworkers.tasks.segment_auto import segment_auto
from bvworkers.tasks.segment_interactive import medsam_predict_2d
from bvworkers.tasks.tile_wsi import tile_wsi
from bvworkers.tasks.training_cohort_export import training_cohort_export_zip

FUNCTIONS = [
    ping,
    pack_volume,
    bulk_document_update,
    propose_care_phases,
    purge_expired_documents,
    run_document_ocr,
    extract_document_entities,
    pack_entity_objects_task,
    embed_series,
    embed_text_target,
    embed_text_ml,
    embed_bge_m3_dense,
    embed_bge_m3_all,
    chunk_and_embed_document,
    chunk_and_embed_clinical_note,
    chunk_and_embed_summary,
    chunk_and_embed_report_content,
    export_gdpr_zip,
    export_patient_zip,
    export_study_zip,
    prefetch_series,
    reindex_batch,
    generate_summary,
    autotag_target,
    cleanup_expired_jobs,
    cleanup_upload_sessions,
    deidentify_reindex_study,
    segment_auto,
    medsam_predict_2d,
    tile_wsi,
    training_cohort_export_zip,
    ingest_bulk_files,
    register_series,
    propagate_lesion,
    dispatch_notification,
    notification_safety_net,
    run_review_checks,
    process_inbound_email,
    promote_inbox_item,
    inbox_maintenance,
]
