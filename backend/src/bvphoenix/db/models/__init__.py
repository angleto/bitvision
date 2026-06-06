"""SQLAlchemy ORM models for bitvision phoenix.

Importing this package is enough to register every model on `Base.metadata`,
which is what Alembic's env.py consumes. Keep new model modules imported here.
"""

from bvphoenix.db.models.agent_tokens import (
    AgentAssistant,
    AgentAssistantPatient,
    AgentToken,
)
from bvphoenix.db.models.annotations import Tag, TagAlias
from bvphoenix.db.models.app_settings import AppSetting
from bvphoenix.db.models.audit import AuditLog
from bvphoenix.db.models.audit_session_view import AuditSessionView
from bvphoenix.db.models.calendar_subscriptions import CalendarSubscription
from bvphoenix.db.models.care_phases import (
    CARE_PHASE_AUTHOR_KINDS,
    CARE_PHASE_DEFAULT_COLORS,
    CARE_PHASE_KINDS,
    CARE_PHASE_REVISION_CHANGE_KINDS,
    CarePhase,
    CarePhaseProposal,
    CarePhaseRevision,
)
from bvphoenix.db.models.clinical_event_attachments import (
    ClinicalEventAttachment,
)
from bvphoenix.db.models.clinical_event_transitions import (
    TRANSITION_ACTIONS,
    ClinicalEventTransition,
)
from bvphoenix.db.models.clinical_events import (
    CLINICAL_EVENT_KINDS,
    CLINICAL_EVENT_STATUS_KINDS,
    CLINICAL_EVENT_STATUSES,
    ClinicalEvent,
)
from bvphoenix.db.models.clinical_notes import ClinicalNote
from bvphoenix.db.models.contributor_payouts import ContributorPayout
from bvphoenix.db.models.credit_ledger import CreditLedger
from bvphoenix.db.models.dicom import Derivative, ImagingStudy, Instance, Series
from bvphoenix.db.models.document_catalog import (
    DocumentAuthority,
    DocumentKind,
    DocumentProvenance,
)
from bvphoenix.db.models.document_entities import DocumentEntities
from bvphoenix.db.models.document_ocr import DocumentOCR
from bvphoenix.db.models.duc import DUCMember, DUCRequest, DUCVote
from bvphoenix.db.models.embedding_errors import EmbeddingError
from bvphoenix.db.models.embedding_models import EmbeddingModel
from bvphoenix.db.models.embeddings import Embedding
from bvphoenix.db.models.findings import (
    AnatomySite,
    Finding,
    FindingGeometry,
    FindingRevision,
    FindingType,
    MorphologyTerm,
)
from bvphoenix.db.models.folders import Folder, FolderItem
from bvphoenix.db.models.gdpr import Consent, DataErasureRequest
from bvphoenix.db.models.grants import Grant
from bvphoenix.db.models.idempotency import IdempotencyRecord
from bvphoenix.db.models.jobs import Job
from bvphoenix.db.models.llm_rate_cards import (
    PROVIDERS as LLM_PROVIDERS,
)
from bvphoenix.db.models.llm_rate_cards import (
    TIER_HINTS as LLM_TIER_HINTS,
)
from bvphoenix.db.models.llm_rate_cards import (
    LLMRateCard,
)
from bvphoenix.db.models.markers import Marker, MarkerRevision
from bvphoenix.db.models.notifications import (
    NOTIFICATION_CHANNELS,
    NOTIFICATION_KINDS,
    NOTIFICATION_STATUSES,
    NOTIFICATION_TARGET_KINDS,
    NotificationDispatch,
)
from bvphoenix.db.models.oauth_codes import OAuthCode
from bvphoenix.db.models.password_reset import PasswordResetToken
from bvphoenix.db.models.pathology import PathologySlide
from bvphoenix.db.models.patient_contacts import (
    EMAIL_DELIVERY_STATES,
    PATIENT_CONTACT_CHANNELS,
    PatientContact,
)
from bvphoenix.db.models.patient_tasks import (
    PATIENT_TASK_AUTHOR_KINDS,
    PATIENT_TASK_CATEGORIES,
    PATIENT_TASK_PRIORITIES,
    PATIENT_TASK_STATUSES,
    PATIENT_TASK_TRANSITION_ACTIONS,
    PatientTask,
    PatientTaskTransition,
)
from bvphoenix.db.models.patients import Document, DocumentFile, DocumentStudyLink, Patient
from bvphoenix.db.models.principals import (
    Group,
    Membership,
    Organization,
    Subject,
    User,
)
from bvphoenix.db.models.provenance_events import (
    PROVENANCE_ACTIVITIES,
    PROVENANCE_AGENT_KINDS,
    PROVENANCE_TARGET_KINDS,
    ProvenanceEvent,
)
from bvphoenix.db.models.registrations import (
    REGISTRATION_KINDS,
    REGISTRATION_STATUSES,
    Registration,
)
from bvphoenix.db.models.reindex_jobs import ReindexJob
from bvphoenix.db.models.report_contents import (
    CITATION_TARGET_KINDS,
    CONTENT_DOCUMENT_LINK_ROLES,
    REPORT_CONTENT_AUTHOR_KINDS,
    REPORT_CONTENT_AUTHORITIES,
    REPORT_CONTENT_STATUSES,
    ContentDocumentLink,
    ReportContent,
    ReportContentCitation,
)
from bvphoenix.db.models.revoked_tokens import RevokedToken
from bvphoenix.db.models.segmentations import SEGMENTATION_PRODUCERS, Segmentation
from bvphoenix.db.models.sharing import ShareLink
from bvphoenix.db.models.summaries import Summary
from bvphoenix.db.models.telegram_link_codes import TelegramLinkCode
from bvphoenix.db.models.text_chunks import (
    CHUNK_AUTHOR_KINDS,
    CHUNK_SOURCE_KINDS,
    CHUNKER_VERSIONS,
    DEFAULT_CHUNKER_VERSION,
    TextChunk,
)
from bvphoenix.db.models.text_embeddings import TextEmbedding
from bvphoenix.db.models.text_embeddings_bge_m3 import (
    TextEmbeddingBgeM3,
    TextEmbeddingBgeM3Colbert,
    TextEmbeddingBgeM3Sparse,
)
from bvphoenix.db.models.training_consents import TrainingConsent
from bvphoenix.db.models.training_licenses import (
    DatasetStudy,
    LicensedDataset,
    TrainingLicense,
)
from bvphoenix.db.models.upload_sessions import UploadSession, UploadSessionFile
from bvphoenix.db.models.user_api_keys import UserAPIKey
from bvphoenix.db.models.verification import EmailVerificationToken
from bvphoenix.db.models.versioning import (
    BinaryBlob,
    Commit,
    EntityObject,
    ManifestEntry,
    MergeConflict,
    Proposal,
    Ref,
    RefLog,
)
from bvphoenix.db.models.viewport import ViewportState
from bvphoenix.db.models.wallet_sponsorships import (
    AUDIT_ACTIONS,
    PERIODS,
    SCOPE_KINDS,
    SCOPE_SPECIFICITY,
    WalletSponsorship,
    WalletSponsorshipAudit,
)

__all__ = [
    "AUDIT_ACTIONS",
    "CARE_PHASE_AUTHOR_KINDS",
    "CARE_PHASE_DEFAULT_COLORS",
    "CARE_PHASE_KINDS",
    "CARE_PHASE_REVISION_CHANGE_KINDS",
    "CHUNKER_VERSIONS",
    "CHUNK_AUTHOR_KINDS",
    "CHUNK_SOURCE_KINDS",
    "CITATION_TARGET_KINDS",
    "CLINICAL_EVENT_KINDS",
    "CLINICAL_EVENT_STATUSES",
    "CLINICAL_EVENT_STATUS_KINDS",
    "CONTENT_DOCUMENT_LINK_ROLES",
    "DEFAULT_CHUNKER_VERSION",
    "EMAIL_DELIVERY_STATES",
    "LLM_PROVIDERS",
    "LLM_TIER_HINTS",
    "NOTIFICATION_CHANNELS",
    "NOTIFICATION_KINDS",
    "NOTIFICATION_STATUSES",
    "NOTIFICATION_TARGET_KINDS",
    "PATIENT_CONTACT_CHANNELS",
    "PATIENT_TASK_AUTHOR_KINDS",
    "PATIENT_TASK_CATEGORIES",
    "PATIENT_TASK_PRIORITIES",
    "PATIENT_TASK_STATUSES",
    "PATIENT_TASK_TRANSITION_ACTIONS",
    "PERIODS",
    "PROVENANCE_ACTIVITIES",
    "PROVENANCE_AGENT_KINDS",
    "PROVENANCE_TARGET_KINDS",
    "REPORT_CONTENT_AUTHORITIES",
    "REPORT_CONTENT_AUTHOR_KINDS",
    "REPORT_CONTENT_STATUSES",
    "SCOPE_KINDS",
    "SCOPE_SPECIFICITY",
    "TRANSITION_ACTIONS",
    "AgentAssistant",
    "AgentAssistantPatient",
    "AgentToken",
    "AnatomySite",
    "AppSetting",
    "AuditLog",
    "AuditSessionView",
    "BinaryBlob",
    "CalendarSubscription",
    "CarePhase",
    "CarePhaseProposal",
    "CarePhaseRevision",
    "ClinicalEvent",
    "ClinicalEventAttachment",
    "ClinicalEventTransition",
    "ClinicalNote",
    "Commit",
    "Consent",
    "ContentDocumentLink",
    "ContributorPayout",
    "CreditLedger",
    "DUCMember",
    "DUCRequest",
    "DUCVote",
    "DataErasureRequest",
    "DatasetStudy",
    "Derivative",
    "Document",
    "DocumentAuthority",
    "DocumentEntities",
    "DocumentFile",
    "DocumentKind",
    "DocumentOCR",
    "DocumentProvenance",
    "DocumentStudyLink",
    "EmailVerificationToken",
    "Embedding",
    "EmbeddingError",
    "EmbeddingModel",
    "EntityObject",
    "Finding",
    "FindingGeometry",
    "FindingRevision",
    "FindingType",
    "Folder",
    "FolderItem",
    "Grant",
    "Group",
    "IdempotencyRecord",
    "ImagingStudy",
    "Instance",
    "Job",
    "LLMRateCard",
    "LicensedDataset",
    "ManifestEntry",
    "Marker",
    "MarkerRevision",
    "Membership",
    "MergeConflict",
    "MorphologyTerm",
    "NotificationDispatch",
    "OAuthCode",
    "Organization",
    "PasswordResetToken",
    "PathologySlide",
    "Patient",
    "PatientContact",
    "Proposal",
    "ProvenanceEvent",
    "Ref",
    "RefLog",
    "Registration",
    "ReindexJob",
    "ReportContent",
    "ReportContentCitation",
    "RevokedToken",
    "Segmentation",
    "Series",
    "ShareLink",
    "Subject",
    "Summary",
    "Tag",
    "TagAlias",
    "TelegramLinkCode",
    "TextChunk",
    "TextEmbedding",
    "TextEmbeddingBgeM3",
    "TextEmbeddingBgeM3Colbert",
    "TextEmbeddingBgeM3Sparse",
    "TrainingConsent",
    "TrainingLicense",
    "UploadSession",
    "UploadSessionFile",
    "User",
    "UserAPIKey",
    "ViewportState",
    "WalletSponsorship",
    "WalletSponsorshipAudit",
]
