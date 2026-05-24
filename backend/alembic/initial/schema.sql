--
-- PostgreSQL database dump
--


-- Dumped from database version 16.13 (Debian 16.13-1.pgdg12+1)
-- Dumped by pg_dump version 16.13 (Debian 16.13-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: citext; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: contribution_tier; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.contribution_tier AS ENUM (
    't1',
    't2',
    't3',
    't4'
);


--
-- Name: subject_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.subject_kind AS ENUM (
    'user',
    'organization',
    'group',
    'public',
    'agent'
);


--
-- Name: app_current_subject(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.app_current_subject() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
    SELECT CASE
        WHEN current_setting('app.current_subject_id', true) IS NULL THEN NULL
        WHEN current_setting('app.current_subject_id', true) IN ('', 'anonymous', 'service') THEN NULL
        ELSE current_setting('app.current_subject_id', true)::uuid
    END
$$;


--
-- Name: app_is_service(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.app_is_service() RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
    SELECT coalesce(current_setting('app.current_subject_id', true), '') = 'service'
$$;


--
-- Name: enforce_document_in_folder(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_document_in_folder() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            doc_id uuid;
        BEGIN
            IF TG_TABLE_NAME = 'folder_items' THEN
                doc_id := OLD.resource_id;
            ELSE
                doc_id := NEW.id;
            END IF;
            IF EXISTS (
                SELECT 1
                FROM documents d
                WHERE d.id = doc_id
                  AND d.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM folder_items fi
                      WHERE fi.resource_kind = 'document'
                        AND fi.resource_id = d.id
                  )
            ) THEN
                RAISE EXCEPTION 'document_orphan_forbidden: document % has zero folder containment', doc_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: fn_ce_derive_event_date(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn_ce_derive_event_date() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            tz text := COALESCE(NEW.timezone, 'UTC');
        BEGIN
            -- Only derive when the API hasn't supplied an explicit
            -- event_date (back-compat path: legacy callers set it
            -- directly without touching the timestamps).
            IF NEW.event_status IN ('planned','confirmed','rescheduled')
               AND NEW.planned_start_at IS NOT NULL THEN
                NEW.event_date := ((NEW.planned_start_at AT TIME ZONE tz))::date;
            ELSIF NEW.event_status IN ('completed','missed')
                  AND NEW.actual_start_at IS NOT NULL THEN
                NEW.event_date := ((NEW.actual_start_at AT TIME ZONE tz))::date;
            END IF;
            RETURN NEW;
        END $$;


--
-- Name: principal_set(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.principal_set(subject_uuid uuid) RETURNS TABLE(subject_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
    WITH RECURSIVE inherited(subject_id) AS (
        SELECT subject_uuid
        WHERE subject_uuid IS NOT NULL
        UNION
        SELECT m.parent_subject_id
        FROM memberships m
        JOIN inherited i ON m.subject_id = i.subject_id
    )
    SELECT subject_id FROM inherited
    UNION
    SELECT '00000000-0000-0000-0000-000000000001'::uuid
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_assistant_patients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_assistant_patients (
    assistant_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    granted_by_subject_id uuid,
    granted_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_assistants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_assistants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    owner_subject_id uuid NOT NULL,
    label character varying(255) NOT NULL,
    provider character varying(64),
    model_id character varying(128),
    notes text,
    permissions jsonb DEFAULT '[]'::jsonb NOT NULL,
    deidentify_on_use boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    client_id character varying(64) NOT NULL,
    client_secret_hash character varying(64),
    client_secret_prefix character varying(16)
);


--
-- Name: agent_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    token_hash character varying(128) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    assistant_id uuid NOT NULL,
    token_tail character varying(16) NOT NULL
);


--
-- Name: app_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_settings (
    key character varying(128) NOT NULL,
    value jsonb NOT NULL,
    scope character varying(16) DEFAULT 'admin'::character varying NOT NULL,
    description text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by_subject_id uuid,
    CONSTRAINT ck_app_settings_scope CHECK (((scope)::text = ANY ((ARRAY['public'::character varying, 'admin'::character varying])::text[])))
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    actor_subject_id uuid,
    action character varying(64) NOT NULL,
    resource_kind character varying(32),
    resource_id uuid,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    ip_address inet,
    user_agent text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    agent_token_id uuid,
    model_version character varying(128),
    conversation_id character varying(128)
);


--
-- Name: audit_session_view; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_session_view (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    actor_subject_id uuid,
    patient_id uuid,
    agent_token_id uuid,
    conversation_id character varying(128),
    first_event_at timestamp with time zone DEFAULT now() NOT NULL,
    last_event_at timestamp with time zone DEFAULT now() NOT NULL,
    read_count integer DEFAULT 1 NOT NULL,
    resource_kinds_touched jsonb DEFAULT '[]'::jsonb NOT NULL,
    ip_address inet,
    user_agent text
);


--
-- Name: binary_blobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.binary_blobs (
    content_hash bytea NOT NULL,
    s3_bucket character varying(128) NOT NULL,
    s3_key text NOT NULL,
    size_bytes bigint NOT NULL,
    content_type character varying(128),
    is_tombstoned boolean DEFAULT false NOT NULL,
    tombstoned_at timestamp with time zone,
    refcount integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_binary_blobs_hash_len CHECK ((octet_length(content_hash) = 32)),
    CONSTRAINT ck_binary_blobs_refcount_nonneg CHECK ((refcount >= 0))
);


--
-- Name: care_phase; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.care_phase (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    slug character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    name_i18n jsonb DEFAULT '{}'::jsonb NOT NULL,
    kind character varying(32) NOT NULL,
    color_hex character varying(7) NOT NULL,
    start_date date,
    end_date date,
    ordinal integer NOT NULL,
    narrative_md text,
    author_kind character varying(16) NOT NULL,
    proposed_by_agent_id uuid,
    confirmed_by_user_id uuid,
    confirmed_at timestamp with time zone,
    etag uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_care_phase_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying])::text[]))),
    CONSTRAINT ck_care_phase_color_hex CHECK (((color_hex)::text ~ '^#[0-9A-Fa-f]{6}$'::text)),
    CONSTRAINT ck_care_phase_kind CHECK (((kind)::text = ANY ((ARRAY['imaging'::character varying, 'surgery'::character varying, 'followup'::character varying, 'surveillance'::character varying, 'visit'::character varying, 'reassessment'::character varying, 'planned'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT ck_care_phase_ordinal_nonneg CHECK ((ordinal >= 0))
);


--
-- Name: care_phase_proposal; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.care_phase_proposal (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    job_id uuid,
    payload jsonb NOT NULL,
    model_id character varying(128) NOT NULL,
    input_hash character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_at timestamp with time zone,
    applied_by_user_id uuid
);


--
-- Name: care_phase_revision; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.care_phase_revision (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    phase_id uuid NOT NULL,
    revision_no integer NOT NULL,
    snapshot jsonb NOT NULL,
    change_kind character varying(32) NOT NULL,
    author_kind character varying(16) NOT NULL,
    actor_id uuid,
    diff_summary text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_care_phase_revision_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying])::text[]))),
    CONSTRAINT ck_care_phase_revision_kind CHECK (((change_kind)::text = ANY ((ARRAY['create'::character varying, 'update'::character varying, 'assign'::character varying, 'unassign'::character varying, 'apply_proposal'::character varying, 'restore'::character varying, 'delete'::character varying])::text[]))),
    CONSTRAINT ck_care_phase_revision_no_pos CHECK ((revision_no >= 1))
);


--
-- Name: clinical_event_attachments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clinical_event_attachments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    filename character varying(255) NOT NULL,
    mime character varying(128) NOT NULL,
    size_bytes bigint NOT NULL,
    storage_key character varying(512) NOT NULL,
    uploaded_by_subject_id uuid,
    uploaded_by_kind character varying(16) DEFAULT 'human'::character varying NOT NULL,
    promoted_to_document_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT ck_ce_attachments_size_nonneg CHECK ((size_bytes >= 0)),
    CONSTRAINT ck_ce_attachments_uploader_kind CHECK (((uploaded_by_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[])))
);


--
-- Name: clinical_event_transitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clinical_event_transitions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id uuid NOT NULL,
    action character varying(32) NOT NULL,
    idempotency_key character varying(255) NOT NULL,
    snapshot_before jsonb NOT NULL,
    snapshot_after jsonb NOT NULL,
    actor_subject_id uuid,
    author_kind character varying(16) NOT NULL,
    reason character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ce_transitions_action CHECK (((action)::text = ANY ((ARRAY['confirm'::character varying, 'reschedule'::character varying, 'complete'::character varying, 'cancel'::character varying, 'mark_missed'::character varying])::text[]))),
    CONSTRAINT ck_ce_transitions_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[])))
);


--
-- Name: clinical_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clinical_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    event_date date,
    title character varying(255) NOT NULL,
    body_part character varying(64),
    code_loinc character varying(32),
    code_snomed character varying(32),
    narrative text,
    etag uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    phase_id uuid,
    phase_assigned_by character varying(16),
    phase_assigned_at timestamp with time zone,
    phase_assignment_confidence double precision,
    source character varying(64),
    event_status character varying(16) DEFAULT 'completed'::character varying NOT NULL,
    planned_start_at timestamp with time zone,
    planned_end_at timestamp with time zone,
    actual_start_at timestamp with time zone,
    actual_end_at timestamp with time zone,
    timezone character varying(64),
    location_struct jsonb,
    recurrence_rule character varying(512),
    recurrence_exdates jsonb,
    parent_event_id uuid,
    reminder_offsets_minutes jsonb,
    external_calendar_link_id uuid,
    external_event_id character varying(255),
    external_event_etag character varying(255),
    status_changed_at timestamp with time zone,
    status_changed_by_kind character varying(16),
    status_change_reason character varying(255),
    meeting_url character varying(512),
    links jsonb,
    CONSTRAINT ck_clinical_events_kind CHECK (((kind)::text = ANY ((ARRAY['imaging_study'::character varying, 'surgical_procedure'::character varying, 'outpatient_visit'::character varying, 'inpatient_admission'::character varying, 'lab_batch'::character varying, 'consultation_event'::character varying, 'pathology_review'::character varying, 'mdt_meeting'::character varying, 'cardio_diagnostic'::character varying, 'endoscopy'::character varying, 'radiology_appointment'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT ck_clinical_events_phase_assigned_by CHECK (((phase_assigned_by IS NULL) OR ((phase_assigned_by)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying])::text[])))),
    CONSTRAINT ck_clinical_events_phase_confidence_range CHECK (((phase_assignment_confidence IS NULL) OR ((phase_assignment_confidence >= (0)::double precision) AND (phase_assignment_confidence <= (1)::double precision)))),
    CONSTRAINT ck_clinical_events_status CHECK (((event_status)::text = ANY ((ARRAY['planned'::character varying, 'confirmed'::character varying, 'completed'::character varying, 'cancelled'::character varying, 'missed'::character varying, 'rescheduled'::character varying])::text[]))),
    CONSTRAINT ck_clinical_events_status_by_kind CHECK (((status_changed_by_kind IS NULL) OR ((status_changed_by_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[])))),
    CONSTRAINT ck_clinical_events_time_required_by_status CHECK ((((event_status)::text <> ALL ((ARRAY['planned'::character varying, 'confirmed'::character varying])::text[])) OR (planned_start_at IS NOT NULL)))
);


--
-- Name: clinical_notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clinical_notes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    target_kind character varying(32) NOT NULL,
    target_id uuid NOT NULL,
    author_subject_id uuid NOT NULL,
    body text NOT NULL,
    pinned boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    author_kind character varying(8) DEFAULT 'human'::character varying NOT NULL,
    model_id character varying(128),
    provider character varying(64),
    agent_token_id uuid,
    anchor jsonb,
    CONSTRAINT ck_clinical_notes_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying])::text[]))),
    CONSTRAINT ck_clinical_notes_body_nonempty CHECK ((length(btrim(body)) > 0)),
    CONSTRAINT ck_clinical_notes_target_kind CHECK (((target_kind)::text = ANY ((ARRAY['study'::character varying, 'series'::character varying, 'document'::character varying, 'consultation'::character varying, 'patient'::character varying])::text[])))
);


--
-- Name: commits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.commits (
    commit_hash bytea NOT NULL,
    patient_id uuid NOT NULL,
    tree_hash bytea NOT NULL,
    parent_hashes bytea[] DEFAULT '{}'::bytea[] NOT NULL,
    author_subject_id uuid,
    author_kind character varying(8) DEFAULT 'human'::character varying NOT NULL,
    model_id character varying(128),
    provider character varying(64),
    agent_token_id uuid,
    branch_at_creation character varying(128),
    message text NOT NULL,
    db_txid bigint DEFAULT txid_current() NOT NULL,
    app_version character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    share_link_id uuid,
    agent_assistant_id uuid,
    CONSTRAINT ck_commits_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying, 'link'::character varying])::text[]))),
    CONSTRAINT ck_commits_hash_len CHECK ((octet_length(commit_hash) = 32)),
    CONSTRAINT ck_commits_parent_arity CHECK (((cardinality(parent_hashes) >= 0) AND (cardinality(parent_hashes) <= 2))),
    CONSTRAINT ck_commits_tree_hash_len CHECK ((octet_length(tree_hash) = 32))
);


--
-- Name: consents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.consents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_subject_id uuid NOT NULL,
    kind text NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: content_document_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.content_document_links (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    report_content_id uuid NOT NULL,
    document_id uuid NOT NULL,
    role character varying(20) NOT NULL,
    excerpt text,
    created_by_subject_id uuid,
    agent_token_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_content_document_links_role CHECK (((role)::text = ANY ((ARRAY['extracted_from'::character varying, 'cites'::character varying, 'mentions'::character varying])::text[])))
);


--
-- Name: contributor_payouts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contributor_payouts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    license_id uuid NOT NULL,
    user_subject_id uuid NOT NULL,
    amount_cents bigint NOT NULL,
    bytes_contributed bigint NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    paid_at timestamp with time zone,
    payout_reference character varying(255),
    notes jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_contributor_payouts_amount_nonneg CHECK ((amount_cents >= 0)),
    CONSTRAINT ck_contributor_payouts_bytes_nonneg CHECK ((bytes_contributed >= 0)),
    CONSTRAINT ck_contributor_payouts_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'paid'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: credit_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_ledger (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_subject_id uuid NOT NULL,
    kind character varying(16) NOT NULL,
    amount_cents bigint NOT NULL,
    balance_after_cents bigint NOT NULL,
    reference_kind character varying(32),
    reference_id uuid,
    idempotency_key text NOT NULL,
    notes jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    caller_subject_id uuid NOT NULL,
    sponsorship_id uuid,
    CONSTRAINT ck_credit_ledger_kind CHECK (((kind)::text = ANY ((ARRAY['topup'::character varying, 'debit'::character varying, 'refund'::character varying])::text[])))
);


--
-- Name: data_erasure_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.data_erasure_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_subject_id uuid NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    reason text,
    scope text DEFAULT 'self'::text NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    notes jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_erasure_scope CHECK ((scope = ANY (ARRAY['self'::text, 'studies'::text, 'annotations'::text, 'consents_only'::text]))),
    CONSTRAINT ck_erasure_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying, 'completed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: dataset_studies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dataset_studies (
    dataset_id uuid NOT NULL,
    study_id uuid NOT NULL,
    contributor_subject_id uuid,
    anonymized_s3_bucket character varying(128) NOT NULL,
    anonymized_s3_key character varying(1024) NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    size_bytes bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: derivatives; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.derivatives (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    series_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    format character varying(32) NOT NULL,
    s3_bucket character varying(128) NOT NULL,
    s3_key character varying(1024) NOT NULL,
    size_bytes bigint,
    generator_version character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_derivatives_kind_nonempty CHECK (((kind)::text <> ''::text))
);


--
-- Name: document_authorities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_authorities (
    id character varying(64) NOT NULL,
    display_name jsonb DEFAULT '{}'::jsonb NOT NULL,
    description text,
    trust_score integer DEFAULT 50 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 100 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_entities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_entities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    extractor_version character varying(48) NOT NULL,
    entities_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_files (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    sequence integer DEFAULT 0 NOT NULL,
    file_s3_key text NOT NULL,
    file_content_type character varying(128),
    original_filename character varying(255),
    size_bytes bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_kinds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_kinds (
    id character varying(64) NOT NULL,
    display_name jsonb DEFAULT '{}'::jsonb NOT NULL,
    description text,
    loinc_code character varying(32),
    fhir_resource character varying(64),
    is_active boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 100 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_ocr; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_ocr (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    file_id uuid,
    content_sha256 character varying(64) NOT NULL,
    ocr_engine character varying(32) NOT NULL,
    ocr_engine_version character varying(32) NOT NULL,
    text text DEFAULT ''::text NOT NULL,
    page_count integer,
    bbox_words jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_provenances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_provenances (
    id character varying(64) NOT NULL,
    display_name jsonb DEFAULT '{}'::jsonb NOT NULL,
    description text,
    is_digital boolean DEFAULT true NOT NULL,
    is_imaging boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 100 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_study_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_study_links (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    study_id uuid NOT NULL,
    link_kind character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by_subject_id uuid,
    CONSTRAINT ck_document_study_links_kind CHECK (((link_kind)::text = ANY ((ARRAY['primary_report'::character varying, 'addendum'::character varying, 'second_opinion'::character varying, 'extracted_from'::character varying, 'cites'::character varying, 'mentions'::character varying])::text[])))
);


--
-- Name: documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    uploaded_by_subject_id uuid,
    title character varying(255) NOT NULL,
    text text,
    file_s3_key text,
    file_content_type character varying(128),
    document_date date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    content_sha256 character varying(64),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    purge_after timestamp with time zone,
    delete_reason character varying(255),
    kind_id character varying(64) DEFAULT 'unclassified'::character varying NOT NULL,
    provenance_id character varying(64) DEFAULT 'manual_entry'::character varying NOT NULL,
    authority_id character varying(64) DEFAULT 'original'::character varying NOT NULL,
    original_blob_hash character varying(64),
    etag uuid DEFAULT gen_random_uuid() NOT NULL
);


--
-- Name: duc_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.duc_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_subject_id uuid NOT NULL,
    role character varying(32) DEFAULT 'member'::character varying NOT NULL,
    active_since timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT ck_duc_members_role CHECK (((role)::text = ANY ((ARRAY['chair'::character varying, 'member'::character varying, 'external_advisor'::character varying])::text[])))
);


--
-- Name: duc_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.duc_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    license_id uuid NOT NULL,
    submitted_by_subject_id uuid,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    summary text NOT NULL,
    rationale jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_duc_requests_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying, 'expired'::character varying, 'withdrawn'::character varying])::text[])))
);


--
-- Name: duc_votes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.duc_votes (
    request_id uuid NOT NULL,
    member_id uuid NOT NULL,
    decision character varying(16) NOT NULL,
    rationale text,
    voted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_duc_votes_decision CHECK (((decision)::text = ANY ((ARRAY['approve'::character varying, 'reject'::character varying, 'abstain'::character varying])::text[])))
);


--
-- Name: email_verification_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_verification_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_subject_id uuid NOT NULL,
    token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: embedding_errors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.embedding_errors (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    target_kind text NOT NULL,
    target_id uuid NOT NULL,
    model_id text NOT NULL,
    error_message text NOT NULL,
    error_class text,
    failed_at timestamp with time zone DEFAULT now() NOT NULL,
    retry_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_embedding_errors_target_kind CHECK ((target_kind = ANY (ARRAY['study'::text, 'series'::text, 'instance'::text])))
);


--
-- Name: embedding_models; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.embedding_models (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    dim integer NOT NULL,
    provider text NOT NULL,
    weights_uri text,
    is_active boolean DEFAULT true NOT NULL,
    is_default_for_kind boolean DEFAULT false NOT NULL,
    deprecated_at timestamp with time zone,
    model_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_embedding_models_dim_positive CHECK ((dim > 0)),
    CONSTRAINT ck_embedding_models_kind CHECK ((kind = ANY (ARRAY['image'::text, 'text'::text, 'multimodal'::text])))
);


--
-- Name: embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.embeddings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    target_kind character varying(16) NOT NULL,
    target_id uuid NOT NULL,
    model_id character varying(128) NOT NULL,
    vector public.vector(512) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_embeddings_target_kind CHECK (((target_kind)::text = ANY ((ARRAY['study'::character varying, 'series'::character varying, 'instance'::character varying, 'report'::character varying, 'annotation'::character varying, 'consultation'::character varying, 'document'::character varying])::text[])))
);


--
-- Name: entity_objects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_objects (
    object_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    schema_version integer NOT NULL,
    payload jsonb,
    payload_size integer NOT NULL,
    storage_kind character varying(8) DEFAULT 'full'::character varying NOT NULL,
    delta_parent_hash bytea,
    delta_bytes bytea,
    is_tombstoned boolean DEFAULT false NOT NULL,
    tombstoned_at timestamp with time zone,
    tombstoned_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    s3_bucket character varying(128),
    s3_key text,
    s3_etag character varying(64),
    CONSTRAINT ck_entity_objects_hash_len CHECK ((octet_length(object_hash) = 32)),
    CONSTRAINT ck_entity_objects_kind CHECK (((entity_kind)::text = ANY ((ARRAY['patient'::character varying, 'study'::character varying, 'series'::character varying, 'report'::character varying, 'annotation'::character varying, 'tag'::character varying, 'clinical_note'::character varying, 'patient_document'::character varying, 'consultation'::character varying, 'summary'::character varying, 'measurement'::character varying, 'segmentation'::character varying, '_tree_'::character varying])::text[]))),
    CONSTRAINT ck_entity_objects_storage_invariant CHECK (((((storage_kind)::text = 'full'::text) AND (payload IS NOT NULL) AND (delta_parent_hash IS NULL) AND (delta_bytes IS NULL) AND (s3_bucket IS NULL) AND (s3_key IS NULL)) OR (((storage_kind)::text = 'delta'::text) AND (payload IS NULL) AND (delta_parent_hash IS NOT NULL) AND (delta_bytes IS NOT NULL) AND (s3_bucket IS NULL) AND (s3_key IS NULL)) OR (((storage_kind)::text = 's3'::text) AND (payload IS NULL) AND (delta_parent_hash IS NULL) AND (delta_bytes IS NULL) AND (s3_bucket IS NOT NULL) AND (s3_key IS NOT NULL)))),
    CONSTRAINT ck_entity_objects_storage_kind CHECK (((storage_kind)::text = ANY ((ARRAY['full'::character varying, 'delta'::character varying, 's3'::character varying])::text[])))
);


--
-- Name: folder_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.folder_items (
    folder_id uuid NOT NULL,
    resource_kind character varying(16) NOT NULL,
    resource_id uuid NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_folder_items_kind CHECK (((resource_kind)::text = ANY ((ARRAY['study'::character varying, 'series'::character varying, 'report'::character varying, 'annotation'::character varying, 'document'::character varying, 'consultation'::character varying, 'subfolder'::character varying, 'patient'::character varying])::text[])))
);


--
-- Name: folders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.folders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(255) NOT NULL,
    owner_subject_id uuid NOT NULL,
    parent_folder_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    patient_id uuid,
    description text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    clinical_date date,
    narrative_md text,
    is_root boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_folders_root_shape CHECK (((is_root = false) OR ((patient_id IS NOT NULL) AND (parent_folder_id IS NULL))))
);


--
-- Name: grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.grants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    resource_kind character varying(32) NOT NULL,
    resource_id uuid NOT NULL,
    grantor_subject_id uuid NOT NULL,
    grantee_subject_id uuid NOT NULL,
    parent_grant_id uuid,
    permissions character varying(64)[] NOT NULL,
    conditions jsonb DEFAULT '{}'::jsonb NOT NULL,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    revoked_at timestamp with time zone,
    revoked_by_subject_id uuid,
    is_commercial boolean DEFAULT false NOT NULL,
    purpose text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deidentify boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_grants_resource_kind CHECK (((resource_kind)::text = ANY ((ARRAY['study'::character varying, 'series'::character varying, 'instance'::character varying, 'annotation'::character varying, 'dataset'::character varying, 'patient'::character varying, 'folder'::character varying])::text[])))
);


--
-- Name: groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.groups (
    subject_id uuid NOT NULL,
    parent_org_subject_id uuid,
    slug character varying(120) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: idempotency_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.idempotency_records (
    idempotency_key character varying(255) NOT NULL,
    request_hash character varying(64) NOT NULL,
    actor_subject_id uuid,
    method character varying(8) NOT NULL,
    path character varying(512) NOT NULL,
    response_status integer NOT NULL,
    response_body jsonb,
    response_headers jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: imaging_studies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.imaging_studies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid,
    study_instance_uid character varying(128) NOT NULL,
    owner_subject_id uuid NOT NULL,
    owner_org_subject_id uuid,
    contribution_tier public.contribution_tier DEFAULT 't1'::public.contribution_tier NOT NULL,
    is_public boolean DEFAULT false NOT NULL,
    is_listed_for_sale boolean DEFAULT false NOT NULL,
    ingestion_complete boolean DEFAULT false NOT NULL,
    study_description text,
    study_date date,
    modalities character varying(16)[] DEFAULT '{}'::character varying[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    clinical_event_id uuid
);


--
-- Name: instances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instances (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    series_id uuid NOT NULL,
    sop_instance_uid character varying(128) NOT NULL,
    sop_class_uid character varying(128),
    instance_number integer,
    s3_bucket character varying(128) NOT NULL,
    s3_key character varying(1024) NOT NULL,
    size_bytes bigint,
    content_sha256 character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind character varying(64) NOT NULL,
    owner_subject_id uuid NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    status character varying(16) DEFAULT 'queued'::character varying NOT NULL,
    progress_total integer,
    progress_done integer DEFAULT 0 NOT NULL,
    stage character varying(64),
    input jsonb DEFAULT '{}'::jsonb NOT NULL,
    result_uri text,
    error jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    expires_at timestamp with time zone NOT NULL,
    arq_job_id character varying(64),
    scope_ids uuid[],
    CONSTRAINT ck_jobs_progress_done_nonneg CHECK ((progress_done >= 0)),
    CONSTRAINT ck_jobs_progress_total_nonneg CHECK (((progress_total IS NULL) OR (progress_total >= 0))),
    CONSTRAINT ck_jobs_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'succeeded'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: licensed_datasets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.licensed_datasets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    license_id uuid NOT NULL,
    manifest_hash character varying(64) NOT NULL,
    study_count integer NOT NULL,
    contributor_count integer NOT NULL,
    k_anon integer NOT NULL,
    manifest_s3_bucket character varying(128) NOT NULL,
    manifest_s3_key character varying(1024) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_licensed_datasets_contributor_count CHECK ((contributor_count >= 0)),
    CONSTRAINT ck_licensed_datasets_count CHECK ((study_count >= 0)),
    CONSTRAINT ck_licensed_datasets_k_anon CHECK ((k_anon >= 1))
);


--
-- Name: llm_rate_cards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_rate_cards (
    model_id text NOT NULL,
    provider text NOT NULL,
    display_name text NOT NULL,
    input_usd_per_mtok numeric(10,4) NOT NULL,
    output_usd_per_mtok numeric(10,4) NOT NULL,
    cache_read_usd_per_mtok numeric(10,4) DEFAULT '0'::numeric NOT NULL,
    cache_creation_usd_per_mtok numeric(10,4) DEFAULT '0'::numeric NOT NULL,
    markup_pct numeric(5,2),
    tier_hint text DEFAULT 'standard'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    is_in_house boolean DEFAULT false NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by_subject_id uuid,
    CONSTRAINT ck_llm_rate_input_nonneg CHECK ((input_usd_per_mtok >= (0)::numeric)),
    CONSTRAINT ck_llm_rate_markup_bounds CHECK (((markup_pct IS NULL) OR ((markup_pct >= (0)::numeric) AND (markup_pct <= (500)::numeric)))),
    CONSTRAINT ck_llm_rate_output_nonneg CHECK ((output_usd_per_mtok >= (0)::numeric)),
    CONSTRAINT ck_llm_rate_tier_hint CHECK ((tier_hint = ANY (ARRAY['free'::text, 'standard'::text, 'premium'::text])))
);


--
-- Name: manifest_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
)
PARTITION BY HASH (commit_hash);


--
-- Name: manifest_entries_p00; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p00 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p01 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p02 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p03 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p04 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p05 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p06 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p07 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p08 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p09 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p10; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p10 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p11; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p11 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p12; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p12 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p13; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p13 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p14; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p14 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: manifest_entries_p15; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_entries_p15 (
    commit_hash bytea NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    object_hash bytea NOT NULL
);


--
-- Name: markers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.markers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    target_kind character varying(16) NOT NULL,
    target_id uuid NOT NULL,
    kind character varying(48) NOT NULL,
    geometry jsonb,
    body text,
    computed jsonb,
    author_subject_id uuid,
    author_kind character varying(16) DEFAULT 'human'::character varying NOT NULL,
    model_id text,
    provider text,
    agent_token_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_markers_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[]))),
    CONSTRAINT ck_markers_kind CHECK (((kind)::text = ANY ((ARRAY['measurement.distance'::character varying, 'measurement.angle'::character varying, 'measurement.area'::character varying, 'measurement.ellipse'::character varying, 'measurement.freehand'::character varying, 'measurement.arrow'::character varying, 'measurement.text'::character varying, 'measurement.probe'::character varying, 'measurement.bbox'::character varying, 'bbox.lesion'::character varying, 'fiducial'::character varying, 'reading-note'::character varying, 'text-overlay'::character varying])::text[]))),
    CONSTRAINT ck_markers_target_kind CHECK (((target_kind)::text = ANY ((ARRAY['study'::character varying, 'series'::character varying, 'instance'::character varying])::text[])))
);


--
-- Name: memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memberships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    subject_id uuid NOT NULL,
    parent_subject_id uuid NOT NULL,
    role character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_memberships_role CHECK (((role)::text = ANY ((ARRAY['admin'::character varying, 'member'::character varying, 'viewer'::character varying, 'nested'::character varying])::text[])))
);


--
-- Name: merge_conflicts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.merge_conflicts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    proposal_id uuid NOT NULL,
    entity_kind character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    base_object_hash bytea,
    source_object_hash bytea,
    target_object_hash bytea,
    conflict_kind character varying(16) NOT NULL,
    resolution character varying(16),
    resolved_object_hash bytea,
    resolved_by_subject_id uuid,
    resolved_at timestamp with time zone,
    CONSTRAINT ck_merge_conflicts_kind CHECK (((conflict_kind)::text = ANY ((ARRAY['add_add'::character varying, 'edit_edit'::character varying, 'edit_delete'::character varying, 'delete_edit'::character varying])::text[]))),
    CONSTRAINT ck_merge_conflicts_resolution CHECK (((resolution IS NULL) OR ((resolution)::text = ANY ((ARRAY['take_source'::character varying, 'take_target'::character varying, 'manual'::character varying, 'auto_merge'::character varying])::text[]))))
);


--
-- Name: notification_dispatches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_dispatches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    contact_id uuid NOT NULL,
    target_kind character varying(32) NOT NULL,
    target_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    channel character varying(32) NOT NULL,
    offset_minutes integer NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    locale character varying(8) DEFAULT 'it'::character varying NOT NULL,
    idempotency_key character varying(64) NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    provider_message_id character varying(255),
    error_code character varying(64),
    author_kind character varying(16) NOT NULL,
    author_subject_id uuid,
    arq_job_id character varying(64),
    template_context jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    CONSTRAINT ck_notification_dispatches_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[]))),
    CONSTRAINT ck_notification_dispatches_channel CHECK (((channel)::text = ANY ((ARRAY['email'::character varying, 'webhook_generic'::character varying, 'webhook_telegram'::character varying, 'webhook_whatsapp'::character varying, 'ics_attachment'::character varying])::text[]))),
    CONSTRAINT ck_notification_dispatches_kind CHECK (((kind)::text = ANY ((ARRAY['event_reminder'::character varying, 'task_reminder'::character varying, 'followup'::character varying])::text[]))),
    CONSTRAINT ck_notification_dispatches_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'sent'::character varying, 'failed'::character varying, 'dead_letter'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT ck_notification_dispatches_target_kind CHECK (((target_kind)::text = ANY ((ARRAY['clinical_event'::character varying, 'patient_task'::character varying])::text[])))
);


--
-- Name: oauth_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_codes (
    code character varying(64) NOT NULL,
    client_id character varying(64) NOT NULL,
    redirect_uri text NOT NULL,
    code_challenge character varying(128) NOT NULL,
    code_challenge_method character varying(16) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: organizations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organizations (
    subject_id uuid NOT NULL,
    slug character varying(120) NOT NULL,
    kind character varying(50),
    homepage_url text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: password_reset_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.password_reset_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_subject_id uuid NOT NULL,
    token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    requested_ip text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: patient_contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patient_contacts (
    id uuid NOT NULL,
    patient_id uuid NOT NULL,
    label character varying(120) NOT NULL,
    relationship character varying(80),
    email character varying(255),
    phone character varying(64),
    notes text,
    is_primary boolean DEFAULT false NOT NULL,
    consent_to_contact boolean DEFAULT false NOT NULL,
    delegation_subject_id uuid,
    delegation_share_link_id uuid,
    delegation_grant_id uuid,
    delegation_level character varying(16),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    preferred_channels jsonb DEFAULT '["email"]'::jsonb NOT NULL,
    preferred_locale character varying(8) DEFAULT 'it'::character varying NOT NULL,
    telegram_chat_id character varying(64),
    whatsapp_phone character varying(32),
    webhook_url character varying(512),
    webhook_secret_encrypted bytea,
    consent_email boolean DEFAULT false NOT NULL,
    consent_telegram boolean DEFAULT false NOT NULL,
    consent_whatsapp boolean DEFAULT false NOT NULL,
    consent_webhook boolean DEFAULT false NOT NULL,
    email_delivery_state character varying(16) DEFAULT 'active'::character varying NOT NULL,
    opt_out_token uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_patient_contacts_email_delivery_state CHECK (((email_delivery_state)::text = ANY ((ARRAY['active'::character varying, 'bounced'::character varying, 'suppressed'::character varying, 'unsubscribed'::character varying])::text[])))
);


--
-- Name: patient_task_transitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patient_task_transitions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    action character varying(32) NOT NULL,
    idempotency_key character varying(255) NOT NULL,
    snapshot_before jsonb NOT NULL,
    snapshot_after jsonb NOT NULL,
    actor_subject_id uuid,
    author_kind character varying(16) NOT NULL,
    reason character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_pt_transitions_action CHECK (((action)::text = ANY ((ARRAY['start'::character varying, 'snooze'::character varying, 'wake'::character varying, 'complete'::character varying, 'drop'::character varying, 'reopen'::character varying, 'reschedule'::character varying])::text[]))),
    CONSTRAINT ck_pt_transitions_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[])))
);


--
-- Name: patient_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patient_tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    title character varying(255) NOT NULL,
    description text,
    category character varying(32) DEFAULT 'other'::character varying NOT NULL,
    priority character varying(16) DEFAULT 'normal'::character varying NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    due_at timestamp with time zone,
    snooze_until timestamp with time zone,
    completed_at timestamp with time zone,
    timezone character varying(64),
    phase_id uuid,
    phase_assigned_by character varying(16),
    phase_assigned_at timestamp with time zone,
    recurrence_rule character varying(512),
    parent_task_id uuid,
    assigned_to_contact_id uuid,
    related_event_id uuid,
    related_document_id uuid,
    labels jsonb,
    links jsonb,
    reminder_offsets_minutes jsonb,
    etag uuid DEFAULT gen_random_uuid() NOT NULL,
    author_kind character varying(16) NOT NULL,
    created_by_subject_id uuid,
    status_changed_at timestamp with time zone,
    status_changed_by_kind character varying(16),
    status_change_reason character varying(255),
    deleted_at timestamp with time zone,
    deleted_by_subject_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_patient_tasks_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[]))),
    CONSTRAINT ck_patient_tasks_category CHECK (((category)::text = ANY ((ARRAY['admin'::character varying, 'pharmacy'::character varying, 'appointment_prep'::character varying, 'transport'::character varying, 'communication'::character varying, 'personal'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT ck_patient_tasks_phase_assigned_by CHECK (((phase_assigned_by IS NULL) OR ((phase_assigned_by)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[])))),
    CONSTRAINT ck_patient_tasks_priority CHECK (((priority)::text = ANY ((ARRAY['low'::character varying, 'normal'::character varying, 'high'::character varying, 'urgent'::character varying])::text[]))),
    CONSTRAINT ck_patient_tasks_snooze_when CHECK ((((status)::text <> 'snoozed'::text) OR (snooze_until IS NOT NULL))),
    CONSTRAINT ck_patient_tasks_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'in_progress'::character varying, 'snoozed'::character varying, 'done'::character varying, 'dropped'::character varying])::text[]))),
    CONSTRAINT ck_patient_tasks_status_by_kind CHECK (((status_changed_by_kind IS NULL) OR ((status_changed_by_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[]))))
);


--
-- Name: patients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patients (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    managed_by_subject_id uuid,
    self_user_subject_id uuid,
    display_name character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    birth_date date,
    sex character varying(1),
    phone character varying(32),
    email character varying(255),
    address text,
    blood_type character varying(8),
    allergies text,
    notes text,
    birth_place_city character varying(128),
    birth_place_province character varying(8),
    asl_code character varying(16),
    asl_name character varying(255),
    external_identifiers jsonb DEFAULT '[]'::jsonb NOT NULL,
    cf_normalized character varying(16) GENERATED ALWAYS AS (upper((jsonb_path_query_first(external_identifiers, '$[*]?(@."type" == "fiscal-code")."value"'::jsonpath) #>> '{}'::text[]))) STORED,
    notes_updated_at timestamp with time zone,
    notes_updated_by_subject_id uuid,
    CONSTRAINT ck_patients_external_identifiers_array CHECK ((jsonb_typeof(external_identifiers) = 'array'::text))
);


--
-- Name: proposals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proposals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    source_ref_name character varying(128) NOT NULL,
    target_ref_name character varying(128) NOT NULL,
    source_head_commit bytea NOT NULL,
    target_head_commit bytea NOT NULL,
    base_commit bytea,
    proposer_subject_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    status character varying(16) DEFAULT 'open'::character varying NOT NULL,
    conflict_count integer DEFAULT 0 NOT NULL,
    merge_commit bytea,
    reviewed_by_subject_id uuid,
    reviewed_at timestamp with time zone,
    review_decision character varying(16),
    review_notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    CONSTRAINT ck_proposals_review_decision CHECK (((review_decision IS NULL) OR ((review_decision)::text = ANY ((ARRAY['approve'::character varying, 'request_changes'::character varying, 'reject'::character varying])::text[])))),
    CONSTRAINT ck_proposals_status CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'approved'::character varying, 'rejected'::character varying, 'merged'::character varying, 'withdrawn'::character varying, 'superseded'::character varying])::text[])))
);


--
-- Name: provenance_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provenance_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    target_kind character varying(32) NOT NULL,
    target_id uuid NOT NULL,
    activity character varying(32) NOT NULL,
    agent_kind character varying(8) NOT NULL,
    agent_subject_id uuid,
    agent_token_id uuid,
    source_kind character varying(32),
    source_id uuid,
    diff jsonb,
    metadata jsonb,
    signature_hash character varying(64),
    prev_signature_hash character varying(64),
    agent_assistant_id uuid,
    CONSTRAINT ck_provenance_events_activity CHECK (((activity)::text = ANY ((ARRAY['create'::character varying, 'classify'::character varying, 'extract'::character varying, 'endorse'::character varying, 'sign'::character varying, 'reject'::character varying, 'supersede'::character varying, 'merge'::character varying, 'split'::character varying, 'cite'::character varying, 'link'::character varying, 'unlink'::character varying, 'redact'::character varying, 'delete'::character varying, 'restore'::character varying, 'identify'::character varying, 'update'::character varying, 'transition.confirm'::character varying, 'transition.reschedule'::character varying, 'transition.complete'::character varying, 'transition.cancel'::character varying, 'transition.mark_missed'::character varying, 'create.rescheduled'::character varying, 'attachment.upload'::character varying, 'attachment.delete'::character varying, 'attachment.promote'::character varying, 'transition.start'::character varying, 'transition.snooze'::character varying, 'transition.wake'::character varying, 'transition.drop'::character varying, 'transition.reopen'::character varying])::text[]))),
    CONSTRAINT ck_provenance_events_agent_identified CHECK ((((agent_kind)::text <> 'agent'::text) OR (agent_token_id IS NOT NULL) OR (agent_assistant_id IS NOT NULL))),
    CONSTRAINT ck_provenance_events_agent_kind CHECK (((agent_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying])::text[]))),
    CONSTRAINT ck_provenance_events_human_subject_present CHECK ((((agent_kind)::text <> 'human'::text) OR (agent_subject_id IS NOT NULL))),
    CONSTRAINT ck_provenance_events_target_kind CHECK (((target_kind)::text = ANY ((ARRAY['patient'::character varying, 'clinical_event'::character varying, 'imaging_study'::character varying, 'series'::character varying, 'report_content'::character varying, 'document'::character varying, 'document_file'::character varying, 'marker'::character varying, 'tag'::character varying, 'external_identifier'::character varying, 'content_document_link'::character varying, 'report_content_citation'::character varying, 'patient_task'::character varying])::text[])))
);


--
-- Name: redaction_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.redaction_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    target_kind character varying(32) NOT NULL,
    target_id uuid NOT NULL,
    field_path character varying(128) NOT NULL,
    original_excerpt_hash bytea NOT NULL,
    redaction_kind character varying(32) NOT NULL,
    model_id character varying(128),
    provider character varying(64),
    prompt_hash bytea,
    applied_by_subject_id uuid,
    reviewer_subject_id uuid,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_at timestamp with time zone,
    CONSTRAINT ck_redaction_events_excerpt_hash_len CHECK ((octet_length(original_excerpt_hash) = 32)),
    CONSTRAINT ck_redaction_events_kind CHECK (((redaction_kind)::text = ANY ((ARRAY['regex_codice_fiscale'::character varying, 'regex_phone'::character varying, 'regex_email'::character varying, 'regex_date_precise'::character varying, 'regex_address'::character varying, 'regex_proper_name'::character varying, 'llm_scrub_via_mcp'::character varying, 'manual'::character varying])::text[]))),
    CONSTRAINT ck_redaction_events_prompt_hash_len CHECK (((prompt_hash IS NULL) OR (octet_length(prompt_hash) = 32)))
);


--
-- Name: ref_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ref_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    ref_name character varying(128) NOT NULL,
    from_commit bytea,
    to_commit bytea NOT NULL,
    op_kind character varying(16) NOT NULL,
    actor_subject_id uuid,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ref_log_op_kind CHECK (((op_kind)::text = ANY ((ARRAY['init'::character varying, 'commit'::character varying, 'merge'::character varying, 'reset'::character varying, 'revert'::character varying, 'rebase'::character varying, 'delete'::character varying])::text[])))
);


--
-- Name: refs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.refs (
    patient_id uuid NOT NULL,
    ref_name character varying(128) NOT NULL,
    commit_hash bytea NOT NULL,
    owner_subject_id uuid,
    visibility character varying(8) DEFAULT 'private'::character varying NOT NULL,
    is_locked boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_refs_visibility CHECK (((visibility)::text = ANY ((ARRAY['private'::character varying, 'shared'::character varying, 'public'::character varying])::text[])))
);


--
-- Name: registrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    fixed_series_id uuid NOT NULL,
    moving_series_id uuid NOT NULL,
    kind character varying(16) NOT NULL,
    status character varying(16) DEFAULT 'queued'::character varying NOT NULL,
    s3_bucket character varying(255),
    s3_key character varying(512),
    result_meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    job_id uuid,
    requested_by_subject_id uuid,
    agent_token_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT ck_registrations_kind CHECK (((kind)::text = ANY ((ARRAY['rigid'::character varying, 'demons'::character varying])::text[]))),
    CONSTRAINT ck_registrations_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'succeeded'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: reindex_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reindex_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    target_kind character varying(16) NOT NULL,
    from_model_id character varying(128),
    to_model_id character varying(128) NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    total_items integer,
    processed_items integer DEFAULT 0 NOT NULL,
    failed_items integer DEFAULT 0 NOT NULL,
    batch_size integer DEFAULT 100 NOT NULL,
    error_summary text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_by_subject_id uuid,
    CONSTRAINT ck_reindex_jobs_batch_size CHECK (((batch_size > 0) AND (batch_size <= 10000))),
    CONSTRAINT ck_reindex_jobs_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'paused'::character varying, 'completed'::character varying, 'failed'::character varying, 'rolled_back'::character varying])::text[]))),
    CONSTRAINT ck_reindex_jobs_target_kind CHECK (((target_kind)::text = ANY ((ARRAY['study'::character varying, 'series'::character varying, 'instance'::character varying])::text[])))
);


--
-- Name: report_content_citations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.report_content_citations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    report_content_id uuid NOT NULL,
    target_kind character varying(24) NOT NULL,
    target_id uuid NOT NULL,
    excerpt text,
    page integer,
    bbox jsonb,
    file_id uuid,
    slice_idx integer,
    annotation_marker_idx integer,
    lab_value_id uuid,
    agent_token_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_report_content_citations_target_kind CHECK (((target_kind)::text = ANY ((ARRAY['clinical_event'::character varying, 'imaging_study'::character varying, 'series'::character varying, 'report_content'::character varying, 'document'::character varying, 'marker'::character varying, 'lab_value'::character varying])::text[])))
);


--
-- Name: report_contents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.report_contents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    clinical_event_id uuid NOT NULL,
    authority_id character varying(64) NOT NULL,
    status character varying(24) NOT NULL,
    language character varying(10) DEFAULT 'it'::character varying NOT NULL,
    title character varying(255),
    narrative_md text,
    structured_fields jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by_subject_id uuid NOT NULL,
    author_kind character varying(8) NOT NULL,
    agent_token_id uuid,
    model_id character varying(128),
    provider character varying(64),
    extracted_at timestamp with time zone,
    parser_version character varying(64),
    endorsed_by_subject_id uuid,
    endorsed_at timestamp with time zone,
    findings_md text,
    recommendations_md text,
    confidence double precision,
    token_usage jsonb,
    deidentified_input boolean,
    consent_snapshot jsonb,
    signed_by_subject_id uuid,
    signed_at timestamp with time zone,
    rejected_reason text,
    superseded_by_id uuid,
    supersede_reason text,
    etag uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    agent_assistant_id uuid,
    CONSTRAINT ck_report_contents_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying])::text[]))),
    CONSTRAINT ck_report_contents_authority_status CHECK (((((authority_id)::text = ANY ((ARRAY['original'::character varying, 'derived'::character varying])::text[])) AND ((status)::text = ANY ((ARRAY['extracted_auto'::character varying, 'endorsed'::character varying, 'stale'::character varying])::text[]))) OR (((authority_id)::text = 'canonical_synthesis'::text) AND ((status)::text = ANY ((ARRAY['draft'::character varying, 'final'::character varying, 'signed'::character varying, 'rejected'::character varying, 'stale'::character varying])::text[]))))),
    CONSTRAINT ck_report_contents_rejected_reason CHECK ((((status)::text <> 'rejected'::text) OR (rejected_reason IS NOT NULL))),
    CONSTRAINT ck_report_contents_signed_complete CHECK ((((status)::text <> 'signed'::text) OR ((signed_by_subject_id IS NOT NULL) AND (signed_at IS NOT NULL)))),
    CONSTRAINT ck_report_contents_status CHECK (((status)::text = ANY ((ARRAY['extracted_auto'::character varying, 'endorsed'::character varying, 'draft'::character varying, 'final'::character varying, 'signed'::character varying, 'rejected'::character varying, 'stale'::character varying])::text[])))
);


--
-- Name: revoked_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.revoked_tokens (
    jti uuid NOT NULL,
    revoked_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_by_subject_id uuid,
    reason text,
    subject_id uuid,
    typ character varying(16)
);


--
-- Name: segmentations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.segmentations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    series_id uuid NOT NULL,
    producer character varying(32) NOT NULL,
    producer_version character varying(48),
    label character varying(128) NOT NULL,
    s3_bucket character varying(255) NOT NULL,
    s3_key character varying(512) NOT NULL,
    size_bytes bigint,
    label_map jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by_subject_id uuid,
    agent_token_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: series; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.series (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    study_id uuid NOT NULL,
    series_instance_uid character varying(128) NOT NULL,
    series_number integer,
    modality character varying(16),
    body_part_examined character varying(64),
    series_description text,
    expected_instance_count integer,
    received_instance_count integer DEFAULT 0 NOT NULL,
    ingestion_complete boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: share_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.share_links (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    grant_id uuid NOT NULL,
    token character varying(64) NOT NULL,
    password_hash text,
    label text,
    max_uses integer,
    use_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    recipient_name character varying(255),
    recipient_email character varying(255),
    recipient_phone character varying(64),
    mode character varying(16) DEFAULT 'claim'::character varying NOT NULL,
    claimed_by_subject_id uuid,
    claimed_at timestamp with time zone,
    received_at timestamp with time zone,
    prepared_job_id uuid,
    download_count integer DEFAULT 0 NOT NULL,
    ai_sponsorship_cap_cents bigint,
    ai_sponsorship_id uuid,
    CONSTRAINT ck_share_links_mode CHECK (((mode)::text = ANY ((ARRAY['claim'::character varying, 'anonymous'::character varying])::text[])))
);


--
-- Name: subjects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subjects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind public.subject_kind NOT NULL,
    display_name character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: summaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.summaries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    target_kind text NOT NULL,
    target_id uuid NOT NULL,
    lang text DEFAULT 'it'::text NOT NULL,
    model_id text NOT NULL,
    provider text NOT NULL,
    summary_md text NOT NULL,
    bullet_points jsonb,
    token_usage jsonb,
    source_version_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_summaries_lang CHECK ((lang = ANY (ARRAY['it'::text, 'en'::text]))),
    CONSTRAINT ck_summaries_target_kind CHECK ((target_kind = ANY (ARRAY['series'::text, 'study'::text, 'patient'::text])))
);


--
-- Name: tag_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tag_aliases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    namespace character varying(64) NOT NULL,
    primary_value character varying(255) NOT NULL,
    alias_value character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tags (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    target_kind character varying(16) NOT NULL,
    target_id uuid NOT NULL,
    namespace character varying(64) NOT NULL,
    value character varying(255) NOT NULL,
    created_by_subject_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    confidence double precision,
    patient_id uuid,
    agent_assistant_id uuid,
    CONSTRAINT ck_tags_source CHECK (((source)::text = ANY ((ARRAY['manual'::character varying, 'agent'::character varying, 'auto'::character varying, 'imported'::character varying])::text[]))),
    CONSTRAINT ck_tags_target_kind CHECK (((target_kind)::text = ANY ((ARRAY['study'::character varying, 'series'::character varying, 'instance'::character varying, 'dataset'::character varying])::text[])))
);


--
-- Name: telegram_link_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.telegram_link_codes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code character varying(32) NOT NULL,
    patient_id uuid NOT NULL,
    contact_id uuid NOT NULL,
    created_by_subject_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    telegram_chat_id character varying(64),
    CONSTRAINT ck_telegram_link_codes_length CHECK (((length((code)::text) >= 8) AND (length((code)::text) <= 32)))
);


--
-- Name: text_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.text_chunks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_kind character varying(32) NOT NULL,
    source_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    author_kind character varying(16) DEFAULT 'unknown'::character varying NOT NULL,
    authority_id character varying(64),
    document_kind_id character varying(64),
    chunker_version character varying(64) NOT NULL,
    page integer,
    char_start integer NOT NULL,
    char_end integer NOT NULL,
    text text NOT NULL,
    text_tsv tsvector GENERATED ALWAYS AS (to_tsvector('italian'::regconfig, text)) STORED NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_text_chunks_author_kind CHECK (((author_kind)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'system'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT ck_text_chunks_source_kind CHECK (((source_kind)::text = ANY ((ARRAY['document'::character varying, 'clinical_note'::character varying, 'summary'::character varying, 'report_content'::character varying])::text[])))
);


--
-- Name: text_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.text_embeddings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    target_kind text NOT NULL,
    target_id uuid NOT NULL,
    model_id text NOT NULL,
    vector public.vector(384) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_text_embeddings_model_id CHECK ((model_id = 'minilm-multi-v1'::text)),
    CONSTRAINT ck_text_embeddings_target_kind CHECK ((target_kind = ANY (ARRAY['series'::text, 'report'::text, 'annotation'::text, 'consultation'::text, 'document'::text, 'patient'::text, 'document_chunk'::text])))
);


--
-- Name: training_consents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.training_consents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_subject_id uuid NOT NULL,
    study_id uuid NOT NULL,
    tier character varying(2) NOT NULL,
    consent_version integer DEFAULT 1 NOT NULL,
    consent_hash character varying(64) NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    revoke_reason text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_training_consents_tier CHECK (((tier)::text = ANY ((ARRAY['t3'::character varying, 't4'::character varying])::text[]))),
    CONSTRAINT ck_training_consents_version_positive CHECK ((consent_version > 0))
);


--
-- Name: training_licenses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.training_licenses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    licensee_name character varying(255) NOT NULL,
    licensee_email character varying(320) NOT NULL,
    price_usd_cents bigint NOT NULL,
    term_months integer DEFAULT 12 NOT NULL,
    status character varying(16) DEFAULT 'draft'::character varying NOT NULL,
    duc_request_id uuid,
    signed_at timestamp with time zone,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone,
    notes jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_training_licenses_price_nonneg CHECK ((price_usd_cents >= 0)),
    CONSTRAINT ck_training_licenses_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'pending_duc'::character varying, 'approved'::character varying, 'signed'::character varying, 'revoked'::character varying])::text[]))),
    CONSTRAINT ck_training_licenses_term_positive CHECK ((term_months > 0))
);


--
-- Name: user_api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_api_keys (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_subject_id uuid NOT NULL,
    provider character varying(32) NOT NULL,
    key_nonce bytea NOT NULL,
    key_ciphertext bytea NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    subject_id uuid NOT NULL,
    email character varying(320) NOT NULL,
    oidc_subject character varying(255),
    is_admin boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    password_hash text,
    email_verified_at timestamp with time zone,
    mfa_secret text,
    mfa_enabled_at timestamp with time zone,
    backup_codes_hash text[],
    storage_quota_bytes bigint,
    max_concurrent_jobs integer,
    is_active boolean DEFAULT true NOT NULL,
    blocked_at timestamp with time zone,
    blocked_reason character varying(255)
);


--
-- Name: viewport_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.viewport_states (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_subject_id uuid NOT NULL,
    series_id uuid NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: wallet_sponsorship_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallet_sponsorship_audit (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sponsorship_id uuid NOT NULL,
    actor_subject_id uuid,
    action text NOT NULL,
    before_cap_cents bigint,
    after_cap_cents bigint,
    notes jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_wallet_sponsorship_audit_action CHECK ((action = ANY (ARRAY['created'::text, 'cap_raised'::text, 'cap_lowered'::text, 'revoked'::text, 'expired'::text])))
);


--
-- Name: wallet_sponsorships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallet_sponsorships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sponsor_subject_id uuid NOT NULL,
    sponsored_subject_id uuid NOT NULL,
    scope_kind text NOT NULL,
    scope_id uuid,
    cap_cents bigint NOT NULL,
    spent_cents bigint DEFAULT '0'::bigint NOT NULL,
    period text,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    revoked_at timestamp with time zone,
    revoked_by_subject_id uuid,
    purpose text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_wallet_sponsorships_cap_positive CHECK ((cap_cents > 0)),
    CONSTRAINT ck_wallet_sponsorships_distinct_subjects CHECK ((sponsor_subject_id <> sponsored_subject_id)),
    CONSTRAINT ck_wallet_sponsorships_period CHECK (((period IS NULL) OR (period = ANY (ARRAY['weekly'::text, 'monthly'::text])))),
    CONSTRAINT ck_wallet_sponsorships_scope_id_match CHECK ((((scope_kind = 'global'::text) AND (scope_id IS NULL)) OR ((scope_kind <> 'global'::text) AND (scope_id IS NOT NULL)))),
    CONSTRAINT ck_wallet_sponsorships_scope_kind CHECK ((scope_kind = ANY (ARRAY['patient'::text, 'consultation'::text, 'organization'::text, 'global'::text]))),
    CONSTRAINT ck_wallet_sponsorships_spent_nonneg CHECK ((spent_cents >= 0))
);


--
-- Name: manifest_entries_p00; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p00 FOR VALUES WITH (modulus 16, remainder 0);


--
-- Name: manifest_entries_p01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p01 FOR VALUES WITH (modulus 16, remainder 1);


--
-- Name: manifest_entries_p02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p02 FOR VALUES WITH (modulus 16, remainder 2);


--
-- Name: manifest_entries_p03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p03 FOR VALUES WITH (modulus 16, remainder 3);


--
-- Name: manifest_entries_p04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p04 FOR VALUES WITH (modulus 16, remainder 4);


--
-- Name: manifest_entries_p05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p05 FOR VALUES WITH (modulus 16, remainder 5);


--
-- Name: manifest_entries_p06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p06 FOR VALUES WITH (modulus 16, remainder 6);


--
-- Name: manifest_entries_p07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p07 FOR VALUES WITH (modulus 16, remainder 7);


--
-- Name: manifest_entries_p08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p08 FOR VALUES WITH (modulus 16, remainder 8);


--
-- Name: manifest_entries_p09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p09 FOR VALUES WITH (modulus 16, remainder 9);


--
-- Name: manifest_entries_p10; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p10 FOR VALUES WITH (modulus 16, remainder 10);


--
-- Name: manifest_entries_p11; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p11 FOR VALUES WITH (modulus 16, remainder 11);


--
-- Name: manifest_entries_p12; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p12 FOR VALUES WITH (modulus 16, remainder 12);


--
-- Name: manifest_entries_p13; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p13 FOR VALUES WITH (modulus 16, remainder 13);


--
-- Name: manifest_entries_p14; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p14 FOR VALUES WITH (modulus 16, remainder 14);


--
-- Name: manifest_entries_p15; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries ATTACH PARTITION public.manifest_entries_p15 FOR VALUES WITH (modulus 16, remainder 15);


--
-- Name: agent_assistant_patients agent_assistant_patients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_assistant_patients
    ADD CONSTRAINT agent_assistant_patients_pkey PRIMARY KEY (assistant_id, patient_id);


--
-- Name: agent_assistants agent_assistants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_assistants
    ADD CONSTRAINT agent_assistants_pkey PRIMARY KEY (id);


--
-- Name: agent_tokens agent_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT agent_tokens_pkey PRIMARY KEY (id);


--
-- Name: agent_tokens agent_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT agent_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: app_settings app_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_pkey PRIMARY KEY (key);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: audit_session_view audit_session_view_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_session_view
    ADD CONSTRAINT audit_session_view_pkey PRIMARY KEY (id);


--
-- Name: binary_blobs binary_blobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.binary_blobs
    ADD CONSTRAINT binary_blobs_pkey PRIMARY KEY (content_hash);


--
-- Name: care_phase care_phase_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase
    ADD CONSTRAINT care_phase_pkey PRIMARY KEY (id);


--
-- Name: care_phase_proposal care_phase_proposal_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase_proposal
    ADD CONSTRAINT care_phase_proposal_pkey PRIMARY KEY (id);


--
-- Name: care_phase_revision care_phase_revision_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase_revision
    ADD CONSTRAINT care_phase_revision_pkey PRIMARY KEY (id);


--
-- Name: clinical_event_attachments clinical_event_attachments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_event_attachments
    ADD CONSTRAINT clinical_event_attachments_pkey PRIMARY KEY (id);


--
-- Name: clinical_event_transitions clinical_event_transitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_event_transitions
    ADD CONSTRAINT clinical_event_transitions_pkey PRIMARY KEY (id);


--
-- Name: clinical_events clinical_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_events
    ADD CONSTRAINT clinical_events_pkey PRIMARY KEY (id);


--
-- Name: clinical_notes clinical_notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_notes
    ADD CONSTRAINT clinical_notes_pkey PRIMARY KEY (id);


--
-- Name: commits commits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commits
    ADD CONSTRAINT commits_pkey PRIMARY KEY (commit_hash);


--
-- Name: consents consents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consents
    ADD CONSTRAINT consents_pkey PRIMARY KEY (id);


--
-- Name: content_document_links content_document_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_document_links
    ADD CONSTRAINT content_document_links_pkey PRIMARY KEY (id);


--
-- Name: contributor_payouts contributor_payouts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contributor_payouts
    ADD CONSTRAINT contributor_payouts_pkey PRIMARY KEY (id);


--
-- Name: credit_ledger credit_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_ledger
    ADD CONSTRAINT credit_ledger_pkey PRIMARY KEY (id);


--
-- Name: data_erasure_requests data_erasure_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_erasure_requests
    ADD CONSTRAINT data_erasure_requests_pkey PRIMARY KEY (id);


--
-- Name: dataset_studies dataset_studies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dataset_studies
    ADD CONSTRAINT dataset_studies_pkey PRIMARY KEY (dataset_id, study_id);


--
-- Name: derivatives derivatives_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.derivatives
    ADD CONSTRAINT derivatives_pkey PRIMARY KEY (id);


--
-- Name: document_authorities document_authorities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_authorities
    ADD CONSTRAINT document_authorities_pkey PRIMARY KEY (id);


--
-- Name: document_entities document_entities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_entities
    ADD CONSTRAINT document_entities_pkey PRIMARY KEY (id);


--
-- Name: document_kinds document_kinds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_kinds
    ADD CONSTRAINT document_kinds_pkey PRIMARY KEY (id);


--
-- Name: document_ocr document_ocr_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_ocr
    ADD CONSTRAINT document_ocr_pkey PRIMARY KEY (id);


--
-- Name: document_provenances document_provenances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_provenances
    ADD CONSTRAINT document_provenances_pkey PRIMARY KEY (id);


--
-- Name: document_study_links document_study_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_study_links
    ADD CONSTRAINT document_study_links_pkey PRIMARY KEY (id);


--
-- Name: duc_members duc_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duc_members
    ADD CONSTRAINT duc_members_pkey PRIMARY KEY (id);


--
-- Name: duc_requests duc_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duc_requests
    ADD CONSTRAINT duc_requests_pkey PRIMARY KEY (id);


--
-- Name: duc_votes duc_votes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duc_votes
    ADD CONSTRAINT duc_votes_pkey PRIMARY KEY (request_id, member_id);


--
-- Name: email_verification_tokens email_verification_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_pkey PRIMARY KEY (id);


--
-- Name: email_verification_tokens email_verification_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: embedding_errors embedding_errors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embedding_errors
    ADD CONSTRAINT embedding_errors_pkey PRIMARY KEY (id);


--
-- Name: embedding_models embedding_models_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embedding_models
    ADD CONSTRAINT embedding_models_pkey PRIMARY KEY (id);


--
-- Name: embeddings embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embeddings
    ADD CONSTRAINT embeddings_pkey PRIMARY KEY (id);


--
-- Name: entity_objects entity_objects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_objects
    ADD CONSTRAINT entity_objects_pkey PRIMARY KEY (object_hash);


--
-- Name: folder_items folder_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folder_items
    ADD CONSTRAINT folder_items_pkey PRIMARY KEY (folder_id, resource_kind, resource_id);


--
-- Name: folders folders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_pkey PRIMARY KEY (id);


--
-- Name: grants grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grants
    ADD CONSTRAINT grants_pkey PRIMARY KEY (id);


--
-- Name: groups groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.groups
    ADD CONSTRAINT groups_pkey PRIMARY KEY (subject_id);


--
-- Name: instances instances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT instances_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: licensed_datasets licensed_datasets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.licensed_datasets
    ADD CONSTRAINT licensed_datasets_pkey PRIMARY KEY (id);


--
-- Name: llm_rate_cards llm_rate_cards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_rate_cards
    ADD CONSTRAINT llm_rate_cards_pkey PRIMARY KEY (model_id);


--
-- Name: manifest_entries pk_manifest_entries; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries
    ADD CONSTRAINT pk_manifest_entries PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p00 manifest_entries_p00_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p00
    ADD CONSTRAINT manifest_entries_p00_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p01 manifest_entries_p01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p01
    ADD CONSTRAINT manifest_entries_p01_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p02 manifest_entries_p02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p02
    ADD CONSTRAINT manifest_entries_p02_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p03 manifest_entries_p03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p03
    ADD CONSTRAINT manifest_entries_p03_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p04 manifest_entries_p04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p04
    ADD CONSTRAINT manifest_entries_p04_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p05 manifest_entries_p05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p05
    ADD CONSTRAINT manifest_entries_p05_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p06 manifest_entries_p06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p06
    ADD CONSTRAINT manifest_entries_p06_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p07 manifest_entries_p07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p07
    ADD CONSTRAINT manifest_entries_p07_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p08 manifest_entries_p08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p08
    ADD CONSTRAINT manifest_entries_p08_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p09 manifest_entries_p09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p09
    ADD CONSTRAINT manifest_entries_p09_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p10 manifest_entries_p10_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p10
    ADD CONSTRAINT manifest_entries_p10_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p11 manifest_entries_p11_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p11
    ADD CONSTRAINT manifest_entries_p11_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p12 manifest_entries_p12_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p12
    ADD CONSTRAINT manifest_entries_p12_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p13 manifest_entries_p13_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p13
    ADD CONSTRAINT manifest_entries_p13_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p14 manifest_entries_p14_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p14
    ADD CONSTRAINT manifest_entries_p14_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: manifest_entries_p15 manifest_entries_p15_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_entries_p15
    ADD CONSTRAINT manifest_entries_p15_pkey PRIMARY KEY (commit_hash, entity_kind, entity_id);


--
-- Name: markers markers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.markers
    ADD CONSTRAINT markers_pkey PRIMARY KEY (id);


--
-- Name: memberships memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_pkey PRIMARY KEY (id);


--
-- Name: merge_conflicts merge_conflicts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT merge_conflicts_pkey PRIMARY KEY (id);


--
-- Name: notification_dispatches notification_dispatches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_dispatches
    ADD CONSTRAINT notification_dispatches_pkey PRIMARY KEY (id);


--
-- Name: oauth_codes oauth_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_codes
    ADD CONSTRAINT oauth_codes_pkey PRIMARY KEY (code);


--
-- Name: organizations organizations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_pkey PRIMARY KEY (subject_id);


--
-- Name: organizations organizations_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_slug_key UNIQUE (slug);


--
-- Name: password_reset_tokens password_reset_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (id);


--
-- Name: password_reset_tokens password_reset_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: patient_contacts patient_contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_contacts
    ADD CONSTRAINT patient_contacts_pkey PRIMARY KEY (id);


--
-- Name: document_files patient_document_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_files
    ADD CONSTRAINT patient_document_files_pkey PRIMARY KEY (id);


--
-- Name: documents patient_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT patient_documents_pkey PRIMARY KEY (id);


--
-- Name: patient_task_transitions patient_task_transitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_task_transitions
    ADD CONSTRAINT patient_task_transitions_pkey PRIMARY KEY (id);


--
-- Name: patient_tasks patient_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_tasks
    ADD CONSTRAINT patient_tasks_pkey PRIMARY KEY (id);


--
-- Name: patients patients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT patients_pkey PRIMARY KEY (id);


--
-- Name: patients patients_self_user_subject_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT patients_self_user_subject_id_key UNIQUE (self_user_subject_id);


--
-- Name: idempotency_records pk_idempotency_records; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT pk_idempotency_records PRIMARY KEY (idempotency_key, request_hash);


--
-- Name: refs pk_refs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT pk_refs PRIMARY KEY (patient_id, ref_name);


--
-- Name: proposals proposals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT proposals_pkey PRIMARY KEY (id);


--
-- Name: provenance_events provenance_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provenance_events
    ADD CONSTRAINT provenance_events_pkey PRIMARY KEY (id);


--
-- Name: redaction_events redaction_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.redaction_events
    ADD CONSTRAINT redaction_events_pkey PRIMARY KEY (id);


--
-- Name: ref_log ref_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_log
    ADD CONSTRAINT ref_log_pkey PRIMARY KEY (id);


--
-- Name: registrations registrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registrations
    ADD CONSTRAINT registrations_pkey PRIMARY KEY (id);


--
-- Name: reindex_jobs reindex_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reindex_jobs
    ADD CONSTRAINT reindex_jobs_pkey PRIMARY KEY (id);


--
-- Name: report_content_citations report_content_citations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_content_citations
    ADD CONSTRAINT report_content_citations_pkey PRIMARY KEY (id);


--
-- Name: report_contents report_contents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_contents
    ADD CONSTRAINT report_contents_pkey PRIMARY KEY (id);


--
-- Name: revoked_tokens revoked_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.revoked_tokens
    ADD CONSTRAINT revoked_tokens_pkey PRIMARY KEY (jti);


--
-- Name: segmentations segmentations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.segmentations
    ADD CONSTRAINT segmentations_pkey PRIMARY KEY (id);


--
-- Name: series series_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series
    ADD CONSTRAINT series_pkey PRIMARY KEY (id);


--
-- Name: share_links share_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.share_links
    ADD CONSTRAINT share_links_pkey PRIMARY KEY (id);


--
-- Name: share_links share_links_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.share_links
    ADD CONSTRAINT share_links_token_key UNIQUE (token);


--
-- Name: imaging_studies studies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.imaging_studies
    ADD CONSTRAINT studies_pkey PRIMARY KEY (id);


--
-- Name: subjects subjects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subjects
    ADD CONSTRAINT subjects_pkey PRIMARY KEY (id);


--
-- Name: summaries summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.summaries
    ADD CONSTRAINT summaries_pkey PRIMARY KEY (id);


--
-- Name: tag_aliases tag_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_aliases
    ADD CONSTRAINT tag_aliases_pkey PRIMARY KEY (id);


--
-- Name: tags tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT tags_pkey PRIMARY KEY (id);


--
-- Name: telegram_link_codes telegram_link_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_link_codes
    ADD CONSTRAINT telegram_link_codes_pkey PRIMARY KEY (id);


--
-- Name: text_chunks text_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.text_chunks
    ADD CONSTRAINT text_chunks_pkey PRIMARY KEY (id);


--
-- Name: text_embeddings text_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.text_embeddings
    ADD CONSTRAINT text_embeddings_pkey PRIMARY KEY (id);


--
-- Name: training_consents training_consents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.training_consents
    ADD CONSTRAINT training_consents_pkey PRIMARY KEY (id);


--
-- Name: training_licenses training_licenses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.training_licenses
    ADD CONSTRAINT training_licenses_pkey PRIMARY KEY (id);


--
-- Name: care_phase uq_care_phase_patient_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase
    ADD CONSTRAINT uq_care_phase_patient_id UNIQUE (patient_id, id);


--
-- Name: care_phase uq_care_phase_patient_slug; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase
    ADD CONSTRAINT uq_care_phase_patient_slug UNIQUE (patient_id, slug);


--
-- Name: care_phase_revision uq_care_phase_revision_phase_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase_revision
    ADD CONSTRAINT uq_care_phase_revision_phase_no UNIQUE (phase_id, revision_no);


--
-- Name: clinical_event_transitions uq_ce_transitions_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_event_transitions
    ADD CONSTRAINT uq_ce_transitions_idempotency UNIQUE (event_id, action, idempotency_key);


--
-- Name: clinical_events uq_clinical_events_patient_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_events
    ADD CONSTRAINT uq_clinical_events_patient_id UNIQUE (patient_id, id);


--
-- Name: content_document_links uq_content_document_links_triple; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_document_links
    ADD CONSTRAINT uq_content_document_links_triple UNIQUE (report_content_id, document_id, role);


--
-- Name: derivatives uq_derivatives_series_kind_format; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.derivatives
    ADD CONSTRAINT uq_derivatives_series_kind_format UNIQUE (series_id, kind, format);


--
-- Name: document_entities uq_document_entities_cache; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_entities
    ADD CONSTRAINT uq_document_entities_cache UNIQUE (document_id, extractor_version, content_sha256);


--
-- Name: document_ocr uq_document_ocr_cache; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_ocr
    ADD CONSTRAINT uq_document_ocr_cache UNIQUE (file_id, content_sha256, ocr_engine_version);


--
-- Name: document_study_links uq_document_study_links_triple; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_study_links
    ADD CONSTRAINT uq_document_study_links_triple UNIQUE (document_id, study_id, link_kind);


--
-- Name: embedding_models uq_embedding_models_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embedding_models
    ADD CONSTRAINT uq_embedding_models_name UNIQUE (name);


--
-- Name: embeddings uq_embeddings_target_model; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embeddings
    ADD CONSTRAINT uq_embeddings_target_model UNIQUE (target_kind, target_id, model_id);


--
-- Name: groups uq_groups_org_slug; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.groups
    ADD CONSTRAINT uq_groups_org_slug UNIQUE (parent_org_subject_id, slug);


--
-- Name: instances uq_instances_series_uid; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT uq_instances_series_uid UNIQUE (series_id, sop_instance_uid);


--
-- Name: memberships uq_memberships_edge; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT uq_memberships_edge UNIQUE (subject_id, parent_subject_id);


--
-- Name: merge_conflicts uq_merge_conflicts_proposal_entity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT uq_merge_conflicts_proposal_entity UNIQUE (proposal_id, entity_kind, entity_id);


--
-- Name: notification_dispatches uq_notification_dispatches_idem; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_dispatches
    ADD CONSTRAINT uq_notification_dispatches_idem UNIQUE (idempotency_key);


--
-- Name: patient_contacts uq_patient_contacts_patient_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_contacts
    ADD CONSTRAINT uq_patient_contacts_patient_id UNIQUE (patient_id, id);


--
-- Name: patient_tasks uq_patient_tasks_patient_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_tasks
    ADD CONSTRAINT uq_patient_tasks_patient_id UNIQUE (patient_id, id);


--
-- Name: patient_task_transitions uq_pt_transitions_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_task_transitions
    ADD CONSTRAINT uq_pt_transitions_idempotency UNIQUE (task_id, action, idempotency_key);


--
-- Name: segmentations uq_segmentations_series_producer_label; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.segmentations
    ADD CONSTRAINT uq_segmentations_series_producer_label UNIQUE (series_id, producer, label);


--
-- Name: series uq_series_study_uid; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series
    ADD CONSTRAINT uq_series_study_uid UNIQUE (study_id, series_instance_uid);


--
-- Name: imaging_studies uq_studies_owner_uid; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.imaging_studies
    ADD CONSTRAINT uq_studies_owner_uid UNIQUE (owner_subject_id, study_instance_uid);


--
-- Name: summaries uq_summaries_target_lang_model; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.summaries
    ADD CONSTRAINT uq_summaries_target_lang_model UNIQUE (target_kind, target_id, lang, model_id);


--
-- Name: tag_aliases uq_tag_aliases_namespace_alias; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_aliases
    ADD CONSTRAINT uq_tag_aliases_namespace_alias UNIQUE (namespace, alias_value);


--
-- Name: tags uq_tags_target_namespace_value; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT uq_tags_target_namespace_value UNIQUE (target_kind, target_id, namespace, value);


--
-- Name: text_chunks uq_text_chunks_source_version_start; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.text_chunks
    ADD CONSTRAINT uq_text_chunks_source_version_start UNIQUE (source_kind, source_id, chunker_version, char_start);


--
-- Name: text_embeddings uq_text_embeddings_target_model; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.text_embeddings
    ADD CONSTRAINT uq_text_embeddings_target_model UNIQUE (target_kind, target_id, model_id);


--
-- Name: viewport_states uq_viewport_states_user_series; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.viewport_states
    ADD CONSTRAINT uq_viewport_states_user_series UNIQUE (user_subject_id, series_id);


--
-- Name: user_api_keys user_api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_api_keys
    ADD CONSTRAINT user_api_keys_pkey PRIMARY KEY (id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_oidc_subject_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_oidc_subject_key UNIQUE (oidc_subject);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (subject_id);


--
-- Name: viewport_states viewport_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.viewport_states
    ADD CONSTRAINT viewport_states_pkey PRIMARY KEY (id);


--
-- Name: wallet_sponsorship_audit wallet_sponsorship_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_sponsorship_audit
    ADD CONSTRAINT wallet_sponsorship_audit_pkey PRIMARY KEY (id);


--
-- Name: wallet_sponsorships wallet_sponsorships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_sponsorships
    ADD CONSTRAINT wallet_sponsorships_pkey PRIMARY KEY (id);


--
-- Name: ix_aap_assistant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_aap_assistant ON public.agent_assistant_patients USING btree (assistant_id);


--
-- Name: ix_aap_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_aap_patient ON public.agent_assistant_patients USING btree (patient_id);


--
-- Name: ix_agent_assistants_client_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_agent_assistants_client_id ON public.agent_assistants USING btree (client_id);


--
-- Name: ix_agent_assistants_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_assistants_owner ON public.agent_assistants USING btree (owner_subject_id);


--
-- Name: ix_agent_assistants_secret_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_assistants_secret_hash ON public.agent_assistants USING btree (client_secret_hash) WHERE (client_secret_hash IS NOT NULL);


--
-- Name: ix_agent_tokens_assistant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_tokens_assistant ON public.agent_tokens USING btree (assistant_id);


--
-- Name: ix_agent_tokens_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_tokens_expires_at ON public.agent_tokens USING btree (expires_at);


--
-- Name: ix_audit_actor_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_actor_time ON public.audit_log USING btree (actor_subject_id, created_at);


--
-- Name: ix_audit_log_agent_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_agent_token ON public.audit_log USING btree (agent_token_id, created_at);


--
-- Name: ix_audit_log_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_conversation ON public.audit_log USING btree (conversation_id);


--
-- Name: ix_audit_resource_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_resource_time ON public.audit_log USING btree (resource_kind, resource_id, created_at);


--
-- Name: ix_audit_session_actor_patient_last; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_session_actor_patient_last ON public.audit_session_view USING btree (actor_subject_id, patient_id, last_event_at);


--
-- Name: ix_audit_session_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_session_conversation ON public.audit_session_view USING btree (conversation_id);


--
-- Name: ix_audit_session_patient_last; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_session_patient_last ON public.audit_session_view USING btree (patient_id, last_event_at);


--
-- Name: ix_binary_blobs_refcount_zero; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_binary_blobs_refcount_zero ON public.binary_blobs USING btree (refcount) WHERE ((refcount = 0) AND (is_tombstoned = false));


--
-- Name: ix_care_phase_patient_ordinal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_care_phase_patient_ordinal ON public.care_phase USING btree (patient_id, ordinal);


--
-- Name: ix_care_phase_proposal_patient_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_care_phase_proposal_patient_created ON public.care_phase_proposal USING btree (patient_id, created_at DESC);


--
-- Name: ix_care_phase_proposal_patient_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_care_phase_proposal_patient_hash ON public.care_phase_proposal USING btree (patient_id, input_hash);


--
-- Name: ix_care_phase_revision_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_care_phase_revision_patient ON public.care_phase_revision USING btree (patient_id);


--
-- Name: ix_care_phase_revision_phase; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_care_phase_revision_phase ON public.care_phase_revision USING btree (phase_id, revision_no);


--
-- Name: ix_ce_attachments_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ce_attachments_event ON public.clinical_event_attachments USING btree (event_id) WHERE (deleted_at IS NULL);


--
-- Name: ix_ce_attachments_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ce_attachments_patient ON public.clinical_event_attachments USING btree (patient_id) WHERE (deleted_at IS NULL);


--
-- Name: ix_ce_transitions_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ce_transitions_event ON public.clinical_event_transitions USING btree (event_id);


--
-- Name: ix_ce_transitions_event_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ce_transitions_event_created ON public.clinical_event_transitions USING btree (event_id, created_at);


--
-- Name: ix_clinical_events_external_calendar; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_events_external_calendar ON public.clinical_events USING btree (external_calendar_link_id, external_event_id) WHERE (external_calendar_link_id IS NOT NULL);


--
-- Name: ix_clinical_events_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_events_kind ON public.clinical_events USING btree (kind);


--
-- Name: ix_clinical_events_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_events_parent ON public.clinical_events USING btree (parent_event_id) WHERE (parent_event_id IS NOT NULL);


--
-- Name: ix_clinical_events_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_events_patient ON public.clinical_events USING btree (patient_id);


--
-- Name: ix_clinical_events_patient_actual; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_events_patient_actual ON public.clinical_events USING btree (patient_id, actual_start_at DESC NULLS LAST) WHERE ((event_status)::text = ANY ((ARRAY['completed'::character varying, 'missed'::character varying])::text[]));


--
-- Name: ix_clinical_events_patient_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_events_patient_date ON public.clinical_events USING btree (patient_id, event_date DESC);


--
-- Name: ix_clinical_events_patient_status_planned; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_events_patient_status_planned ON public.clinical_events USING btree (patient_id, event_status, planned_start_at) WHERE ((event_status)::text = ANY ((ARRAY['planned'::character varying, 'confirmed'::character varying])::text[]));


--
-- Name: ix_clinical_events_phase; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_events_phase ON public.clinical_events USING btree (phase_id) WHERE (phase_id IS NOT NULL);


--
-- Name: ix_clinical_notes_author_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_notes_author_kind ON public.clinical_notes USING btree (author_kind);


--
-- Name: ix_clinical_notes_created_desc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_notes_created_desc ON public.clinical_notes USING btree (created_at);


--
-- Name: ix_clinical_notes_model_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_notes_model_id ON public.clinical_notes USING btree (model_id);


--
-- Name: ix_clinical_notes_patient_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_notes_patient_id ON public.clinical_notes USING btree (patient_id);


--
-- Name: ix_clinical_notes_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_notes_target ON public.clinical_notes USING btree (target_kind, target_id);


--
-- Name: ix_commits_agent_assistant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_commits_agent_assistant_id ON public.commits USING btree (agent_assistant_id);


--
-- Name: ix_commits_author; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_commits_author ON public.commits USING btree (author_subject_id, created_at);


--
-- Name: ix_commits_parents_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_commits_parents_gin ON public.commits USING gin (parent_hashes);


--
-- Name: ix_commits_patient_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_commits_patient_created ON public.commits USING btree (patient_id, created_at);


--
-- Name: ix_commits_patient_tree; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_commits_patient_tree ON public.commits USING btree (patient_id, tree_hash);


--
-- Name: ix_commits_share_link; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_commits_share_link ON public.commits USING btree (share_link_id);


--
-- Name: ix_consents_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_consents_active ON public.consents USING btree (user_subject_id, kind, revoked_at);


--
-- Name: ix_consents_user_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_consents_user_kind ON public.consents USING btree (user_subject_id, kind);


--
-- Name: ix_content_document_links_content; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_content_document_links_content ON public.content_document_links USING btree (report_content_id);


--
-- Name: ix_content_document_links_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_content_document_links_document ON public.content_document_links USING btree (document_id);


--
-- Name: ix_contributor_payouts_user_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contributor_payouts_user_status ON public.contributor_payouts USING btree (user_subject_id, status);


--
-- Name: ix_credit_ledger_sponsorship; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_ledger_sponsorship ON public.credit_ledger USING btree (sponsorship_id, created_at) WHERE (sponsorship_id IS NOT NULL);


--
-- Name: ix_credit_ledger_user_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_ledger_user_time ON public.credit_ledger USING btree (user_subject_id, created_at DESC);


--
-- Name: ix_dataset_studies_contributor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dataset_studies_contributor ON public.dataset_studies USING btree (contributor_subject_id);


--
-- Name: ix_derivatives_series_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_derivatives_series_id ON public.derivatives USING btree (series_id);


--
-- Name: ix_document_entities_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_entities_document ON public.document_entities USING btree (document_id);


--
-- Name: ix_document_entities_extractor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_entities_extractor ON public.document_entities USING btree (extractor_version, created_at);


--
-- Name: ix_document_kinds_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_kinds_active ON public.document_kinds USING btree (is_active, sort_order);


--
-- Name: ix_document_ocr_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_ocr_document ON public.document_ocr USING btree (document_id);


--
-- Name: ix_document_ocr_engine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_ocr_engine ON public.document_ocr USING btree (ocr_engine, ocr_engine_version);


--
-- Name: ix_document_provenances_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_provenances_active ON public.document_provenances USING btree (is_active, sort_order);


--
-- Name: ix_document_study_links_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_study_links_document ON public.document_study_links USING btree (document_id);


--
-- Name: ix_document_study_links_study; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_study_links_study ON public.document_study_links USING btree (study_id);


--
-- Name: ix_documents_authority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_documents_authority ON public.documents USING btree (patient_id, authority_id);


--
-- Name: ix_documents_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_documents_kind ON public.documents USING btree (patient_id, kind_id);


--
-- Name: ix_documents_original_blob_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_documents_original_blob_hash ON public.documents USING btree (patient_id, original_blob_hash) WHERE (original_blob_hash IS NOT NULL);


--
-- Name: ix_documents_provenance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_documents_provenance ON public.documents USING btree (patient_id, provenance_id);


--
-- Name: ix_duc_requests_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_duc_requests_status ON public.duc_requests USING btree (status);


--
-- Name: ix_email_verification_tokens_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_email_verification_tokens_user ON public.email_verification_tokens USING btree (user_subject_id);


--
-- Name: ix_embedding_errors_failed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_embedding_errors_failed_at ON public.embedding_errors USING btree (failed_at DESC);


--
-- Name: ix_embedding_errors_target_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_embedding_errors_target_model ON public.embedding_errors USING btree (target_kind, target_id, model_id);


--
-- Name: ix_embedding_models_kind_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_embedding_models_kind_active ON public.embedding_models USING btree (kind, is_active);


--
-- Name: ix_embeddings_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_embeddings_target ON public.embeddings USING btree (target_kind, target_id);


--
-- Name: ix_embeddings_vector_cosine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_embeddings_vector_cosine ON public.embeddings USING hnsw (vector public.vector_cosine_ops);


--
-- Name: ix_embeddings_vector_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_embeddings_vector_hnsw ON public.embeddings USING hnsw (vector public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: ix_entity_objects_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entity_objects_created ON public.entity_objects USING btree (created_at);


--
-- Name: ix_entity_objects_delta_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entity_objects_delta_parent ON public.entity_objects USING btree (delta_parent_hash) WHERE (delta_parent_hash IS NOT NULL);


--
-- Name: ix_entity_objects_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entity_objects_kind ON public.entity_objects USING btree (entity_kind);


--
-- Name: ix_erasure_user_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_erasure_user_time ON public.data_erasure_requests USING btree (user_subject_id, requested_at);


--
-- Name: ix_folder_items_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_folder_items_resource ON public.folder_items USING btree (resource_kind, resource_id);


--
-- Name: ix_folders_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_folders_owner ON public.folders USING btree (owner_subject_id);


--
-- Name: ix_folders_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_folders_parent ON public.folders USING btree (parent_folder_id);


--
-- Name: ix_folders_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_folders_patient ON public.folders USING btree (patient_id);


--
-- Name: ix_grants_grantee_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_grants_grantee_resource ON public.grants USING btree (grantee_subject_id, resource_kind, resource_id);


--
-- Name: ix_grants_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_grants_resource ON public.grants USING btree (resource_kind, resource_id);


--
-- Name: ix_groups_parent_org_subject_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_groups_parent_org_subject_id ON public.groups USING btree (parent_org_subject_id);


--
-- Name: ix_idempotency_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idempotency_actor ON public.idempotency_records USING btree (actor_subject_id, created_at);


--
-- Name: ix_idempotency_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idempotency_expires ON public.idempotency_records USING btree (expires_at);


--
-- Name: ix_imaging_studies_event_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_imaging_studies_event_unique ON public.imaging_studies USING btree (clinical_event_id) WHERE (clinical_event_id IS NOT NULL);


--
-- Name: ix_instances_series_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_instances_series_id ON public.instances USING btree (series_id);


--
-- Name: ix_instances_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_instances_uid ON public.instances USING btree (sop_instance_uid);


--
-- Name: ix_jobs_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_expires ON public.jobs USING btree (expires_at);


--
-- Name: ix_jobs_idem_active_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_jobs_idem_active_uniq ON public.jobs USING btree (idempotency_key) WHERE ((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying])::text[]));


--
-- Name: ix_jobs_kind_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_kind_status ON public.jobs USING btree (kind, status);


--
-- Name: ix_jobs_owner_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_owner_active ON public.jobs USING btree (owner_subject_id, status) WHERE ((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying])::text[]));


--
-- Name: ix_jobs_scope_ids_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_scope_ids_gin ON public.jobs USING gin (scope_ids) WHERE (scope_ids IS NOT NULL);


--
-- Name: ix_licensed_datasets_license; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_licensed_datasets_license ON public.licensed_datasets USING btree (license_id);


--
-- Name: ix_llm_rate_cards_provider_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_rate_cards_provider_active ON public.llm_rate_cards USING btree (provider, is_active);


--
-- Name: ix_manifest_kind_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_manifest_kind_entity ON ONLY public.manifest_entries USING btree (entity_kind, entity_id);


--
-- Name: ix_manifest_object; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_manifest_object ON ONLY public.manifest_entries USING btree (object_hash);


--
-- Name: ix_markers_created_at_desc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_markers_created_at_desc ON public.markers USING btree (created_at DESC);


--
-- Name: ix_markers_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_markers_kind ON public.markers USING btree (kind);


--
-- Name: ix_markers_patient_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_markers_patient_target ON public.markers USING btree (patient_id, target_kind, target_id);


--
-- Name: ix_memberships_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memberships_parent ON public.memberships USING btree (parent_subject_id);


--
-- Name: ix_notification_dispatches_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_dispatches_contact ON public.notification_dispatches USING btree (contact_id);


--
-- Name: ix_notification_dispatches_patient_scheduled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_dispatches_patient_scheduled ON public.notification_dispatches USING btree (patient_id, scheduled_at);


--
-- Name: ix_notification_dispatches_pending_scheduled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_dispatches_pending_scheduled ON public.notification_dispatches USING btree (scheduled_at) WHERE ((status)::text = 'pending'::text);


--
-- Name: ix_oauth_codes_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_codes_expires_at ON public.oauth_codes USING btree (expires_at);


--
-- Name: ix_password_reset_tokens_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_password_reset_tokens_expires ON public.password_reset_tokens USING btree (expires_at);


--
-- Name: ix_password_reset_tokens_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_password_reset_tokens_user ON public.password_reset_tokens USING btree (user_subject_id);


--
-- Name: ix_patient_contacts_opt_out_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_patient_contacts_opt_out_token ON public.patient_contacts USING btree (opt_out_token);


--
-- Name: ix_patient_contacts_patient_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_contacts_patient_id ON public.patient_contacts USING btree (patient_id);


--
-- Name: ix_patient_contacts_primary_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_patient_contacts_primary_unique ON public.patient_contacts USING btree (patient_id) WHERE (is_primary IS TRUE);


--
-- Name: ix_patient_document_files_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_document_files_document_id ON public.document_files USING btree (document_id, sequence);


--
-- Name: ix_patient_documents_live; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_documents_live ON public.documents USING btree (patient_id) WHERE (deleted_at IS NULL);


--
-- Name: ix_patient_documents_patient_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_documents_patient_id ON public.documents USING btree (patient_id);


--
-- Name: ix_patient_documents_patient_sha; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_documents_patient_sha ON public.documents USING btree (patient_id, content_sha256);


--
-- Name: ix_patient_documents_purge_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_documents_purge_due ON public.documents USING btree (purge_after) WHERE (deleted_at IS NOT NULL);


--
-- Name: ix_patient_tasks_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_tasks_active ON public.patient_tasks USING btree (patient_id, status, due_at) WHERE ((deleted_at IS NULL) AND ((status)::text = ANY ((ARRAY['pending'::character varying, 'in_progress'::character varying])::text[])));


--
-- Name: ix_patient_tasks_deleted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_tasks_deleted ON public.patient_tasks USING btree (deleted_at) WHERE (deleted_at IS NOT NULL);


--
-- Name: ix_patient_tasks_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_tasks_parent ON public.patient_tasks USING btree (parent_task_id) WHERE (parent_task_id IS NOT NULL);


--
-- Name: ix_patient_tasks_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_tasks_patient ON public.patient_tasks USING btree (patient_id);


--
-- Name: ix_patient_tasks_phase; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_tasks_phase ON public.patient_tasks USING btree (phase_id) WHERE ((phase_id IS NOT NULL) AND (deleted_at IS NULL));


--
-- Name: ix_patient_tasks_snoozed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_tasks_snoozed ON public.patient_tasks USING btree (snooze_until) WHERE (((status)::text = 'snoozed'::text) AND (deleted_at IS NULL));


--
-- Name: ix_patients_cf_normalized; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patients_cf_normalized ON public.patients USING btree (cf_normalized) WHERE (cf_normalized IS NOT NULL);


--
-- Name: ix_patients_managed_by_subject_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patients_managed_by_subject_id ON public.patients USING btree (managed_by_subject_id);


--
-- Name: ix_proposals_patient_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proposals_patient_status ON public.proposals USING btree (patient_id, status);


--
-- Name: ix_proposals_proposer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proposals_proposer ON public.proposals USING btree (proposer_subject_id, status);


--
-- Name: ix_provenance_events_activity_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_provenance_events_activity_recent ON public.provenance_events USING btree (activity, recorded_at DESC);


--
-- Name: ix_provenance_events_agent_assistant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_provenance_events_agent_assistant ON public.provenance_events USING btree (agent_assistant_id, recorded_at DESC) WHERE (agent_assistant_id IS NOT NULL);


--
-- Name: ix_provenance_events_agent_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_provenance_events_agent_subject ON public.provenance_events USING btree (agent_subject_id, recorded_at DESC) WHERE (agent_subject_id IS NOT NULL);


--
-- Name: ix_provenance_events_agent_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_provenance_events_agent_token ON public.provenance_events USING btree (agent_token_id, recorded_at DESC) WHERE (agent_token_id IS NOT NULL);


--
-- Name: ix_provenance_events_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_provenance_events_target ON public.provenance_events USING btree (target_kind, target_id, recorded_at DESC);


--
-- Name: ix_pt_transitions_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pt_transitions_task ON public.patient_task_transitions USING btree (task_id);


--
-- Name: ix_pt_transitions_task_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pt_transitions_task_created ON public.patient_task_transitions USING btree (task_id, created_at);


--
-- Name: ix_redaction_events_applied; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_redaction_events_applied ON public.redaction_events USING btree (applied_at);


--
-- Name: ix_redaction_events_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_redaction_events_target ON public.redaction_events USING btree (target_kind, target_id);


--
-- Name: ix_reflog_patient_ref_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_reflog_patient_ref_time ON public.ref_log USING btree (patient_id, ref_name, created_at);


--
-- Name: ix_refs_commit; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_refs_commit ON public.refs USING btree (commit_hash);


--
-- Name: ix_refs_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_refs_owner ON public.refs USING btree (owner_subject_id);


--
-- Name: ix_registrations_fixed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_registrations_fixed ON public.registrations USING btree (fixed_series_id);


--
-- Name: ix_registrations_moving; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_registrations_moving ON public.registrations USING btree (moving_series_id);


--
-- Name: ix_registrations_status_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_registrations_status_created ON public.registrations USING btree (status, created_at);


--
-- Name: ix_reindex_jobs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_reindex_jobs_status ON public.reindex_jobs USING btree (status);


--
-- Name: ix_reindex_jobs_to_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_reindex_jobs_to_model ON public.reindex_jobs USING btree (to_model_id);


--
-- Name: ix_report_content_citations_content; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_content_citations_content ON public.report_content_citations USING btree (report_content_id);


--
-- Name: ix_report_content_citations_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_content_citations_target ON public.report_content_citations USING btree (target_kind, target_id);


--
-- Name: ix_report_contents_active_canonical; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_contents_active_canonical ON public.report_contents USING btree (clinical_event_id) WHERE (((authority_id)::text = 'canonical_synthesis'::text) AND ((status)::text = 'signed'::text));


--
-- Name: ix_report_contents_agent_assistant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_contents_agent_assistant ON public.report_contents USING btree (agent_assistant_id) WHERE (agent_assistant_id IS NOT NULL);


--
-- Name: ix_report_contents_authority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_contents_authority ON public.report_contents USING btree (authority_id);


--
-- Name: ix_report_contents_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_contents_event ON public.report_contents USING btree (clinical_event_id);


--
-- Name: ix_report_contents_event_authority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_contents_event_authority ON public.report_contents USING btree (clinical_event_id, authority_id);


--
-- Name: ix_report_contents_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_contents_status ON public.report_contents USING btree (status);


--
-- Name: ix_revoked_tokens_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_revoked_tokens_expires ON public.revoked_tokens USING btree (expires_at);


--
-- Name: ix_revoked_tokens_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_revoked_tokens_subject ON public.revoked_tokens USING btree (subject_id);


--
-- Name: ix_segmentations_producer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_segmentations_producer ON public.segmentations USING btree (producer, created_at);


--
-- Name: ix_segmentations_series; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_segmentations_series ON public.segmentations USING btree (series_id);


--
-- Name: ix_series_description_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_description_fts ON public.series USING gin (to_tsvector('simple'::regconfig, COALESCE(series_description, ''::text)));


--
-- Name: ix_series_study_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_study_id ON public.series USING btree (study_id);


--
-- Name: ix_series_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_uid ON public.series USING btree (series_instance_uid);


--
-- Name: ix_share_links_claimed_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_share_links_claimed_by ON public.share_links USING btree (claimed_by_subject_id);


--
-- Name: ix_share_links_grant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_share_links_grant_id ON public.share_links USING btree (grant_id);


--
-- Name: ix_share_links_prepared_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_share_links_prepared_job ON public.share_links USING btree (prepared_job_id) WHERE (prepared_job_id IS NOT NULL);


--
-- Name: ix_studies_description_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_studies_description_fts ON public.imaging_studies USING gin (to_tsvector('simple'::regconfig, COALESCE(study_description, ''::text)));


--
-- Name: ix_studies_owner_org_subject_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_studies_owner_org_subject_id ON public.imaging_studies USING btree (owner_org_subject_id);


--
-- Name: ix_studies_owner_subject_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_studies_owner_subject_id ON public.imaging_studies USING btree (owner_subject_id);


--
-- Name: ix_studies_patient_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_studies_patient_id ON public.imaging_studies USING btree (patient_id);


--
-- Name: ix_studies_public; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_studies_public ON public.imaging_studies USING btree (is_public);


--
-- Name: ix_studies_tier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_studies_tier ON public.imaging_studies USING btree (contribution_tier);


--
-- Name: ix_studies_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_studies_uid ON public.imaging_studies USING btree (study_instance_uid);


--
-- Name: ix_summaries_created_at_desc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_summaries_created_at_desc ON public.summaries USING btree (created_at DESC);


--
-- Name: ix_summaries_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_summaries_target ON public.summaries USING btree (target_kind, target_id, lang);


--
-- Name: ix_tag_aliases_primary; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tag_aliases_primary ON public.tag_aliases USING btree (namespace, primary_value);


--
-- Name: ix_tags_agent_assistant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tags_agent_assistant_id ON public.tags USING btree (agent_assistant_id);


--
-- Name: ix_tags_namespace_value; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tags_namespace_value ON public.tags USING btree (namespace, value);


--
-- Name: ix_tags_patient_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tags_patient_id ON public.tags USING btree (patient_id);


--
-- Name: ix_tags_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tags_source ON public.tags USING btree (source);


--
-- Name: ix_tags_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tags_target ON public.tags USING btree (target_kind, target_id);


--
-- Name: ix_telegram_link_codes_code_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_telegram_link_codes_code_unique ON public.telegram_link_codes USING btree (code);


--
-- Name: ix_telegram_link_codes_contact_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_telegram_link_codes_contact_pending ON public.telegram_link_codes USING btree (contact_id) WHERE (used_at IS NULL);


--
-- Name: ix_text_chunks_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_text_chunks_patient ON public.text_chunks USING btree (patient_id);


--
-- Name: ix_text_chunks_patient_authority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_text_chunks_patient_authority ON public.text_chunks USING btree (patient_id, authority_id) WHERE (authority_id IS NOT NULL);


--
-- Name: ix_text_chunks_patient_filter; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_text_chunks_patient_filter ON public.text_chunks USING btree (patient_id, source_kind, author_kind);


--
-- Name: ix_text_chunks_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_text_chunks_source ON public.text_chunks USING btree (source_kind, source_id);


--
-- Name: ix_text_chunks_text_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_text_chunks_text_tsv ON public.text_chunks USING gin (text_tsv);


--
-- Name: ix_text_embeddings_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_text_embeddings_target ON public.text_embeddings USING btree (target_kind, target_id);


--
-- Name: ix_text_embeddings_vector_cosine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_text_embeddings_vector_cosine ON public.text_embeddings USING hnsw (vector public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: ix_training_consents_study; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_training_consents_study ON public.training_consents USING btree (study_id);


--
-- Name: ix_training_consents_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_training_consents_user ON public.training_consents USING btree (user_subject_id);


--
-- Name: ix_training_licenses_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_training_licenses_status ON public.training_licenses USING btree (status);


--
-- Name: ix_user_api_keys_user_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_api_keys_user_provider ON public.user_api_keys USING btree (user_subject_id, provider);


--
-- Name: ix_viewport_states_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_viewport_states_user ON public.viewport_states USING btree (user_subject_id);


--
-- Name: ix_wallet_sponsorship_audit_sponsorship; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wallet_sponsorship_audit_sponsorship ON public.wallet_sponsorship_audit USING btree (sponsorship_id, created_at);


--
-- Name: ix_wallet_sponsorships_lookup_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wallet_sponsorships_lookup_active ON public.wallet_sponsorships USING btree (sponsored_subject_id, scope_kind, scope_id) WHERE (revoked_at IS NULL);


--
-- Name: ix_wallet_sponsorships_sponsor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wallet_sponsorships_sponsor ON public.wallet_sponsorships USING btree (sponsor_subject_id) WHERE (revoked_at IS NULL);


--
-- Name: manifest_entries_p00_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p00_entity_kind_entity_id_idx ON public.manifest_entries_p00 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p00_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p00_object_hash_idx ON public.manifest_entries_p00 USING btree (object_hash);


--
-- Name: manifest_entries_p01_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p01_entity_kind_entity_id_idx ON public.manifest_entries_p01 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p01_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p01_object_hash_idx ON public.manifest_entries_p01 USING btree (object_hash);


--
-- Name: manifest_entries_p02_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p02_entity_kind_entity_id_idx ON public.manifest_entries_p02 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p02_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p02_object_hash_idx ON public.manifest_entries_p02 USING btree (object_hash);


--
-- Name: manifest_entries_p03_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p03_entity_kind_entity_id_idx ON public.manifest_entries_p03 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p03_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p03_object_hash_idx ON public.manifest_entries_p03 USING btree (object_hash);


--
-- Name: manifest_entries_p04_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p04_entity_kind_entity_id_idx ON public.manifest_entries_p04 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p04_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p04_object_hash_idx ON public.manifest_entries_p04 USING btree (object_hash);


--
-- Name: manifest_entries_p05_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p05_entity_kind_entity_id_idx ON public.manifest_entries_p05 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p05_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p05_object_hash_idx ON public.manifest_entries_p05 USING btree (object_hash);


--
-- Name: manifest_entries_p06_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p06_entity_kind_entity_id_idx ON public.manifest_entries_p06 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p06_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p06_object_hash_idx ON public.manifest_entries_p06 USING btree (object_hash);


--
-- Name: manifest_entries_p07_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p07_entity_kind_entity_id_idx ON public.manifest_entries_p07 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p07_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p07_object_hash_idx ON public.manifest_entries_p07 USING btree (object_hash);


--
-- Name: manifest_entries_p08_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p08_entity_kind_entity_id_idx ON public.manifest_entries_p08 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p08_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p08_object_hash_idx ON public.manifest_entries_p08 USING btree (object_hash);


--
-- Name: manifest_entries_p09_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p09_entity_kind_entity_id_idx ON public.manifest_entries_p09 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p09_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p09_object_hash_idx ON public.manifest_entries_p09 USING btree (object_hash);


--
-- Name: manifest_entries_p10_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p10_entity_kind_entity_id_idx ON public.manifest_entries_p10 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p10_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p10_object_hash_idx ON public.manifest_entries_p10 USING btree (object_hash);


--
-- Name: manifest_entries_p11_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p11_entity_kind_entity_id_idx ON public.manifest_entries_p11 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p11_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p11_object_hash_idx ON public.manifest_entries_p11 USING btree (object_hash);


--
-- Name: manifest_entries_p12_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p12_entity_kind_entity_id_idx ON public.manifest_entries_p12 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p12_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p12_object_hash_idx ON public.manifest_entries_p12 USING btree (object_hash);


--
-- Name: manifest_entries_p13_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p13_entity_kind_entity_id_idx ON public.manifest_entries_p13 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p13_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p13_object_hash_idx ON public.manifest_entries_p13 USING btree (object_hash);


--
-- Name: manifest_entries_p14_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p14_entity_kind_entity_id_idx ON public.manifest_entries_p14 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p14_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p14_object_hash_idx ON public.manifest_entries_p14 USING btree (object_hash);


--
-- Name: manifest_entries_p15_entity_kind_entity_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p15_entity_kind_entity_id_idx ON public.manifest_entries_p15 USING btree (entity_kind, entity_id);


--
-- Name: manifest_entries_p15_object_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX manifest_entries_p15_object_hash_idx ON public.manifest_entries_p15 USING btree (object_hash);


--
-- Name: uq_contributor_payouts_license_user; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_contributor_payouts_license_user ON public.contributor_payouts USING btree (license_id, user_subject_id);


--
-- Name: uq_credit_ledger_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_credit_ledger_idempotency ON public.credit_ledger USING btree (idempotency_key);


--
-- Name: uq_document_study_links_primary_per_study; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_document_study_links_primary_per_study ON public.document_study_links USING btree (study_id) WHERE ((link_kind)::text = 'primary_report'::text);


--
-- Name: uq_duc_members_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_duc_members_user_active ON public.duc_members USING btree (user_subject_id) WHERE (revoked_at IS NULL);


--
-- Name: uq_duc_requests_open_per_license; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_duc_requests_open_per_license ON public.duc_requests USING btree (license_id) WHERE ((status)::text = 'pending'::text);


--
-- Name: uq_embedding_models_default_per_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_embedding_models_default_per_kind ON public.embedding_models USING btree (kind) WHERE ((is_default_for_kind = true) AND (is_active = true) AND (deprecated_at IS NULL));


--
-- Name: uq_folders_root_per_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_folders_root_per_patient ON public.folders USING btree (patient_id) WHERE is_root;


--
-- Name: uq_training_consents_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_training_consents_active ON public.training_consents USING btree (user_subject_id, study_id, tier) WHERE (revoked_at IS NULL);


--
-- Name: uq_user_api_keys_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_user_api_keys_active ON public.user_api_keys USING btree (user_subject_id, provider) WHERE (revoked_at IS NULL);


--
-- Name: manifest_entries_p00_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p00_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p00_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p00_object_hash_idx;


--
-- Name: manifest_entries_p00_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p00_pkey;


--
-- Name: manifest_entries_p01_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p01_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p01_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p01_object_hash_idx;


--
-- Name: manifest_entries_p01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p01_pkey;


--
-- Name: manifest_entries_p02_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p02_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p02_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p02_object_hash_idx;


--
-- Name: manifest_entries_p02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p02_pkey;


--
-- Name: manifest_entries_p03_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p03_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p03_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p03_object_hash_idx;


--
-- Name: manifest_entries_p03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p03_pkey;


--
-- Name: manifest_entries_p04_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p04_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p04_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p04_object_hash_idx;


--
-- Name: manifest_entries_p04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p04_pkey;


--
-- Name: manifest_entries_p05_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p05_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p05_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p05_object_hash_idx;


--
-- Name: manifest_entries_p05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p05_pkey;


--
-- Name: manifest_entries_p06_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p06_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p06_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p06_object_hash_idx;


--
-- Name: manifest_entries_p06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p06_pkey;


--
-- Name: manifest_entries_p07_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p07_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p07_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p07_object_hash_idx;


--
-- Name: manifest_entries_p07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p07_pkey;


--
-- Name: manifest_entries_p08_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p08_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p08_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p08_object_hash_idx;


--
-- Name: manifest_entries_p08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p08_pkey;


--
-- Name: manifest_entries_p09_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p09_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p09_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p09_object_hash_idx;


--
-- Name: manifest_entries_p09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p09_pkey;


--
-- Name: manifest_entries_p10_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p10_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p10_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p10_object_hash_idx;


--
-- Name: manifest_entries_p10_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p10_pkey;


--
-- Name: manifest_entries_p11_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p11_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p11_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p11_object_hash_idx;


--
-- Name: manifest_entries_p11_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p11_pkey;


--
-- Name: manifest_entries_p12_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p12_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p12_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p12_object_hash_idx;


--
-- Name: manifest_entries_p12_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p12_pkey;


--
-- Name: manifest_entries_p13_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p13_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p13_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p13_object_hash_idx;


--
-- Name: manifest_entries_p13_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p13_pkey;


--
-- Name: manifest_entries_p14_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p14_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p14_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p14_object_hash_idx;


--
-- Name: manifest_entries_p14_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p14_pkey;


--
-- Name: manifest_entries_p15_entity_kind_entity_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_kind_entity ATTACH PARTITION public.manifest_entries_p15_entity_kind_entity_id_idx;


--
-- Name: manifest_entries_p15_object_hash_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_manifest_object ATTACH PARTITION public.manifest_entries_p15_object_hash_idx;


--
-- Name: manifest_entries_p15_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_manifest_entries ATTACH PARTITION public.manifest_entries_p15_pkey;


--
-- Name: clinical_events tg_clinical_events_derive_date; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER tg_clinical_events_derive_date BEFORE INSERT OR UPDATE OF planned_start_at, actual_start_at, event_status, timezone ON public.clinical_events FOR EACH ROW EXECUTE FUNCTION public.fn_ce_derive_event_date();


--
-- Name: documents trg_documents_insert_no_orphan; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER trg_documents_insert_no_orphan AFTER INSERT ON public.documents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN ((new.deleted_at IS NULL)) EXECUTE FUNCTION public.enforce_document_in_folder();


--
-- Name: documents trg_documents_restore_no_orphan; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER trg_documents_restore_no_orphan AFTER UPDATE OF deleted_at ON public.documents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (((new.deleted_at IS NULL) AND (old.deleted_at IS NOT NULL))) EXECUTE FUNCTION public.enforce_document_in_folder();


--
-- Name: folder_items trg_folder_items_no_orphan_doc; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER trg_folder_items_no_orphan_doc AFTER DELETE ON public.folder_items DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (((old.resource_kind)::text = 'document'::text)) EXECUTE FUNCTION public.enforce_document_in_folder();


--
-- Name: agent_assistant_patients agent_assistant_patients_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_assistant_patients
    ADD CONSTRAINT agent_assistant_patients_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES public.agent_assistants(id) ON DELETE CASCADE;


--
-- Name: agent_assistant_patients agent_assistant_patients_granted_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_assistant_patients
    ADD CONSTRAINT agent_assistant_patients_granted_by_subject_id_fkey FOREIGN KEY (granted_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: agent_assistant_patients agent_assistant_patients_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_assistant_patients
    ADD CONSTRAINT agent_assistant_patients_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: agent_assistants agent_assistants_owner_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_assistants
    ADD CONSTRAINT agent_assistants_owner_subject_id_fkey FOREIGN KEY (owner_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: agent_tokens agent_tokens_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT agent_tokens_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES public.agent_assistants(id) ON DELETE CASCADE;


--
-- Name: app_settings app_settings_updated_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_updated_by_subject_id_fkey FOREIGN KEY (updated_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: audit_log audit_log_actor_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_actor_subject_id_fkey FOREIGN KEY (actor_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: audit_log audit_log_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: audit_session_view audit_session_view_actor_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_session_view
    ADD CONSTRAINT audit_session_view_actor_subject_id_fkey FOREIGN KEY (actor_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: audit_session_view audit_session_view_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_session_view
    ADD CONSTRAINT audit_session_view_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: audit_session_view audit_session_view_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_session_view
    ADD CONSTRAINT audit_session_view_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: care_phase care_phase_confirmed_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase
    ADD CONSTRAINT care_phase_confirmed_by_user_id_fkey FOREIGN KEY (confirmed_by_user_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: care_phase care_phase_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase
    ADD CONSTRAINT care_phase_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: care_phase_proposal care_phase_proposal_applied_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase_proposal
    ADD CONSTRAINT care_phase_proposal_applied_by_user_id_fkey FOREIGN KEY (applied_by_user_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: care_phase_proposal care_phase_proposal_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase_proposal
    ADD CONSTRAINT care_phase_proposal_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: care_phase_proposal care_phase_proposal_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase_proposal
    ADD CONSTRAINT care_phase_proposal_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: care_phase care_phase_proposed_by_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase
    ADD CONSTRAINT care_phase_proposed_by_agent_id_fkey FOREIGN KEY (proposed_by_agent_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: clinical_event_transitions clinical_event_transitions_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_event_transitions
    ADD CONSTRAINT clinical_event_transitions_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.clinical_events(id) ON DELETE CASCADE;


--
-- Name: clinical_events clinical_events_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_events
    ADD CONSTRAINT clinical_events_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: clinical_notes clinical_notes_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_notes
    ADD CONSTRAINT clinical_notes_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: clinical_notes clinical_notes_author_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_notes
    ADD CONSTRAINT clinical_notes_author_subject_id_fkey FOREIGN KEY (author_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: clinical_notes clinical_notes_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_notes
    ADD CONSTRAINT clinical_notes_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: commits commits_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commits
    ADD CONSTRAINT commits_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: commits commits_author_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commits
    ADD CONSTRAINT commits_author_subject_id_fkey FOREIGN KEY (author_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: commits commits_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commits
    ADD CONSTRAINT commits_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: commits commits_share_link_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commits
    ADD CONSTRAINT commits_share_link_id_fkey FOREIGN KEY (share_link_id) REFERENCES public.share_links(id) ON DELETE SET NULL;


--
-- Name: consents consents_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consents
    ADD CONSTRAINT consents_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: content_document_links content_document_links_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_document_links
    ADD CONSTRAINT content_document_links_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: content_document_links content_document_links_created_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_document_links
    ADD CONSTRAINT content_document_links_created_by_subject_id_fkey FOREIGN KEY (created_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: content_document_links content_document_links_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_document_links
    ADD CONSTRAINT content_document_links_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: content_document_links content_document_links_report_content_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_document_links
    ADD CONSTRAINT content_document_links_report_content_id_fkey FOREIGN KEY (report_content_id) REFERENCES public.report_contents(id) ON DELETE CASCADE;


--
-- Name: contributor_payouts contributor_payouts_license_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contributor_payouts
    ADD CONSTRAINT contributor_payouts_license_id_fkey FOREIGN KEY (license_id) REFERENCES public.training_licenses(id) ON DELETE CASCADE;


--
-- Name: contributor_payouts contributor_payouts_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contributor_payouts
    ADD CONSTRAINT contributor_payouts_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: credit_ledger credit_ledger_sponsorship_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_ledger
    ADD CONSTRAINT credit_ledger_sponsorship_id_fkey FOREIGN KEY (sponsorship_id) REFERENCES public.wallet_sponsorships(id) ON DELETE SET NULL;


--
-- Name: credit_ledger credit_ledger_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_ledger
    ADD CONSTRAINT credit_ledger_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: data_erasure_requests data_erasure_requests_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_erasure_requests
    ADD CONSTRAINT data_erasure_requests_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: dataset_studies dataset_studies_contributor_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dataset_studies
    ADD CONSTRAINT dataset_studies_contributor_subject_id_fkey FOREIGN KEY (contributor_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: dataset_studies dataset_studies_dataset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dataset_studies
    ADD CONSTRAINT dataset_studies_dataset_id_fkey FOREIGN KEY (dataset_id) REFERENCES public.licensed_datasets(id) ON DELETE CASCADE;


--
-- Name: dataset_studies dataset_studies_study_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dataset_studies
    ADD CONSTRAINT dataset_studies_study_id_fkey FOREIGN KEY (study_id) REFERENCES public.imaging_studies(id) ON DELETE RESTRICT;


--
-- Name: derivatives derivatives_series_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.derivatives
    ADD CONSTRAINT derivatives_series_id_fkey FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: document_entities document_entities_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_entities
    ADD CONSTRAINT document_entities_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: document_ocr document_ocr_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_ocr
    ADD CONSTRAINT document_ocr_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: document_ocr document_ocr_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_ocr
    ADD CONSTRAINT document_ocr_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.document_files(id) ON DELETE CASCADE;


--
-- Name: document_study_links document_study_links_created_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_study_links
    ADD CONSTRAINT document_study_links_created_by_subject_id_fkey FOREIGN KEY (created_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: document_study_links document_study_links_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_study_links
    ADD CONSTRAINT document_study_links_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: document_study_links document_study_links_study_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_study_links
    ADD CONSTRAINT document_study_links_study_id_fkey FOREIGN KEY (study_id) REFERENCES public.imaging_studies(id) ON DELETE CASCADE;


--
-- Name: documents documents_authority_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_authority_id_fkey FOREIGN KEY (authority_id) REFERENCES public.document_authorities(id) ON DELETE RESTRICT;


--
-- Name: documents documents_kind_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_kind_id_fkey FOREIGN KEY (kind_id) REFERENCES public.document_kinds(id) ON DELETE RESTRICT;


--
-- Name: documents documents_provenance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_provenance_id_fkey FOREIGN KEY (provenance_id) REFERENCES public.document_provenances(id) ON DELETE RESTRICT;


--
-- Name: duc_members duc_members_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duc_members
    ADD CONSTRAINT duc_members_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: duc_requests duc_requests_license_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duc_requests
    ADD CONSTRAINT duc_requests_license_id_fkey FOREIGN KEY (license_id) REFERENCES public.training_licenses(id) ON DELETE CASCADE;


--
-- Name: duc_requests duc_requests_submitted_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duc_requests
    ADD CONSTRAINT duc_requests_submitted_by_subject_id_fkey FOREIGN KEY (submitted_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: duc_votes duc_votes_member_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duc_votes
    ADD CONSTRAINT duc_votes_member_id_fkey FOREIGN KEY (member_id) REFERENCES public.duc_members(id) ON DELETE CASCADE;


--
-- Name: duc_votes duc_votes_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duc_votes
    ADD CONSTRAINT duc_votes_request_id_fkey FOREIGN KEY (request_id) REFERENCES public.duc_requests(id) ON DELETE CASCADE;


--
-- Name: email_verification_tokens email_verification_tokens_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.users(subject_id) ON DELETE CASCADE;


--
-- Name: care_phase_revision fk_care_phase_revision_phase; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_phase_revision
    ADD CONSTRAINT fk_care_phase_revision_phase FOREIGN KEY (patient_id, phase_id) REFERENCES public.care_phase(patient_id, id) ON DELETE CASCADE;


--
-- Name: clinical_event_attachments fk_ce_attachments_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_event_attachments
    ADD CONSTRAINT fk_ce_attachments_event FOREIGN KEY (patient_id, event_id) REFERENCES public.clinical_events(patient_id, id) ON DELETE CASCADE;


--
-- Name: clinical_events fk_clinical_events_parent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_events
    ADD CONSTRAINT fk_clinical_events_parent FOREIGN KEY (patient_id, parent_event_id) REFERENCES public.clinical_events(patient_id, id) ON DELETE SET NULL;


--
-- Name: clinical_events fk_clinical_events_phase; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_events
    ADD CONSTRAINT fk_clinical_events_phase FOREIGN KEY (patient_id, phase_id) REFERENCES public.care_phase(patient_id, id) ON DELETE SET NULL;


--
-- Name: commits fk_commits_agent_assistant; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commits
    ADD CONSTRAINT fk_commits_agent_assistant FOREIGN KEY (agent_assistant_id) REFERENCES public.agent_assistants(id) ON DELETE SET NULL;


--
-- Name: credit_ledger fk_credit_ledger_caller_subject; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_ledger
    ADD CONSTRAINT fk_credit_ledger_caller_subject FOREIGN KEY (caller_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: patient_tasks fk_patient_tasks_assigned_contact; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_tasks
    ADD CONSTRAINT fk_patient_tasks_assigned_contact FOREIGN KEY (patient_id, assigned_to_contact_id) REFERENCES public.patient_contacts(patient_id, id) ON DELETE SET NULL;


--
-- Name: patient_tasks fk_patient_tasks_parent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_tasks
    ADD CONSTRAINT fk_patient_tasks_parent FOREIGN KEY (patient_id, parent_task_id) REFERENCES public.patient_tasks(patient_id, id) ON DELETE SET NULL;


--
-- Name: patient_tasks fk_patient_tasks_phase; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_tasks
    ADD CONSTRAINT fk_patient_tasks_phase FOREIGN KEY (patient_id, phase_id) REFERENCES public.care_phase(patient_id, id) ON DELETE SET NULL;


--
-- Name: patient_tasks fk_patient_tasks_related_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_tasks
    ADD CONSTRAINT fk_patient_tasks_related_event FOREIGN KEY (patient_id, related_event_id) REFERENCES public.clinical_events(patient_id, id) ON DELETE SET NULL;


--
-- Name: provenance_events fk_provenance_events_agent_assistant; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provenance_events
    ADD CONSTRAINT fk_provenance_events_agent_assistant FOREIGN KEY (agent_assistant_id) REFERENCES public.agent_assistants(id) ON DELETE SET NULL;


--
-- Name: report_contents fk_report_contents_agent_assistant; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_contents
    ADD CONSTRAINT fk_report_contents_agent_assistant FOREIGN KEY (agent_assistant_id) REFERENCES public.agent_assistants(id) ON DELETE SET NULL;


--
-- Name: tags fk_tags_agent_assistant; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT fk_tags_agent_assistant FOREIGN KEY (agent_assistant_id) REFERENCES public.agent_assistants(id) ON DELETE SET NULL;


--
-- Name: tags fk_tags_patient; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT fk_tags_patient FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: folder_items folder_items_folder_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folder_items
    ADD CONSTRAINT folder_items_folder_id_fkey FOREIGN KEY (folder_id) REFERENCES public.folders(id) ON DELETE CASCADE;


--
-- Name: folders folders_owner_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_owner_subject_id_fkey FOREIGN KEY (owner_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: folders folders_parent_folder_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_parent_folder_id_fkey FOREIGN KEY (parent_folder_id) REFERENCES public.folders(id) ON DELETE CASCADE;


--
-- Name: folders folders_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: grants grants_grantee_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grants
    ADD CONSTRAINT grants_grantee_subject_id_fkey FOREIGN KEY (grantee_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: grants grants_grantor_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grants
    ADD CONSTRAINT grants_grantor_subject_id_fkey FOREIGN KEY (grantor_subject_id) REFERENCES public.subjects(id) ON DELETE RESTRICT;


--
-- Name: grants grants_parent_grant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grants
    ADD CONSTRAINT grants_parent_grant_id_fkey FOREIGN KEY (parent_grant_id) REFERENCES public.grants(id) ON DELETE CASCADE;


--
-- Name: grants grants_revoked_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grants
    ADD CONSTRAINT grants_revoked_by_subject_id_fkey FOREIGN KEY (revoked_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: groups groups_parent_org_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.groups
    ADD CONSTRAINT groups_parent_org_subject_id_fkey FOREIGN KEY (parent_org_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: groups groups_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.groups
    ADD CONSTRAINT groups_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: idempotency_records idempotency_records_actor_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT idempotency_records_actor_subject_id_fkey FOREIGN KEY (actor_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: imaging_studies imaging_studies_clinical_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.imaging_studies
    ADD CONSTRAINT imaging_studies_clinical_event_id_fkey FOREIGN KEY (clinical_event_id) REFERENCES public.clinical_events(id) ON DELETE CASCADE;


--
-- Name: instances instances_series_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT instances_series_id_fkey FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: jobs jobs_owner_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_owner_subject_id_fkey FOREIGN KEY (owner_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: licensed_datasets licensed_datasets_license_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.licensed_datasets
    ADD CONSTRAINT licensed_datasets_license_id_fkey FOREIGN KEY (license_id) REFERENCES public.training_licenses(id) ON DELETE CASCADE;


--
-- Name: llm_rate_cards llm_rate_cards_updated_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_rate_cards
    ADD CONSTRAINT llm_rate_cards_updated_by_subject_id_fkey FOREIGN KEY (updated_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: manifest_entries manifest_entries_commit_hash_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.manifest_entries
    ADD CONSTRAINT manifest_entries_commit_hash_fkey FOREIGN KEY (commit_hash) REFERENCES public.commits(commit_hash) ON DELETE CASCADE;


--
-- Name: manifest_entries manifest_entries_object_hash_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.manifest_entries
    ADD CONSTRAINT manifest_entries_object_hash_fkey FOREIGN KEY (object_hash) REFERENCES public.entity_objects(object_hash) ON DELETE RESTRICT;


--
-- Name: markers markers_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.markers
    ADD CONSTRAINT markers_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: markers markers_author_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.markers
    ADD CONSTRAINT markers_author_subject_id_fkey FOREIGN KEY (author_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: markers markers_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.markers
    ADD CONSTRAINT markers_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: memberships memberships_parent_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_parent_subject_id_fkey FOREIGN KEY (parent_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: memberships memberships_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: merge_conflicts merge_conflicts_base_object_hash_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT merge_conflicts_base_object_hash_fkey FOREIGN KEY (base_object_hash) REFERENCES public.entity_objects(object_hash) ON DELETE RESTRICT;


--
-- Name: merge_conflicts merge_conflicts_proposal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT merge_conflicts_proposal_id_fkey FOREIGN KEY (proposal_id) REFERENCES public.proposals(id) ON DELETE CASCADE;


--
-- Name: merge_conflicts merge_conflicts_resolved_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT merge_conflicts_resolved_by_subject_id_fkey FOREIGN KEY (resolved_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: merge_conflicts merge_conflicts_resolved_object_hash_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT merge_conflicts_resolved_object_hash_fkey FOREIGN KEY (resolved_object_hash) REFERENCES public.entity_objects(object_hash) ON DELETE RESTRICT;


--
-- Name: merge_conflicts merge_conflicts_source_object_hash_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT merge_conflicts_source_object_hash_fkey FOREIGN KEY (source_object_hash) REFERENCES public.entity_objects(object_hash) ON DELETE RESTRICT;


--
-- Name: merge_conflicts merge_conflicts_target_object_hash_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.merge_conflicts
    ADD CONSTRAINT merge_conflicts_target_object_hash_fkey FOREIGN KEY (target_object_hash) REFERENCES public.entity_objects(object_hash) ON DELETE RESTRICT;


--
-- Name: notification_dispatches notification_dispatches_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_dispatches
    ADD CONSTRAINT notification_dispatches_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.patient_contacts(id) ON DELETE CASCADE;


--
-- Name: notification_dispatches notification_dispatches_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_dispatches
    ADD CONSTRAINT notification_dispatches_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: organizations organizations_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: password_reset_tokens password_reset_tokens_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.users(subject_id) ON DELETE CASCADE;


--
-- Name: patient_contacts patient_contacts_delegation_grant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_contacts
    ADD CONSTRAINT patient_contacts_delegation_grant_id_fkey FOREIGN KEY (delegation_grant_id) REFERENCES public.grants(id) ON DELETE SET NULL;


--
-- Name: patient_contacts patient_contacts_delegation_share_link_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_contacts
    ADD CONSTRAINT patient_contacts_delegation_share_link_id_fkey FOREIGN KEY (delegation_share_link_id) REFERENCES public.share_links(id) ON DELETE SET NULL;


--
-- Name: patient_contacts patient_contacts_delegation_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_contacts
    ADD CONSTRAINT patient_contacts_delegation_subject_id_fkey FOREIGN KEY (delegation_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: patient_contacts patient_contacts_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_contacts
    ADD CONSTRAINT patient_contacts_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: document_files patient_document_files_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_files
    ADD CONSTRAINT patient_document_files_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: documents patient_documents_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT patient_documents_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: documents patient_documents_uploaded_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT patient_documents_uploaded_by_subject_id_fkey FOREIGN KEY (uploaded_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: patient_task_transitions patient_task_transitions_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_task_transitions
    ADD CONSTRAINT patient_task_transitions_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.patient_tasks(id) ON DELETE CASCADE;


--
-- Name: patient_tasks patient_tasks_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_tasks
    ADD CONSTRAINT patient_tasks_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: patient_tasks patient_tasks_related_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_tasks
    ADD CONSTRAINT patient_tasks_related_document_id_fkey FOREIGN KEY (related_document_id) REFERENCES public.documents(id) ON DELETE SET NULL;


--
-- Name: patients patients_managed_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT patients_managed_by_subject_id_fkey FOREIGN KEY (managed_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: patients patients_notes_updated_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT patients_notes_updated_by_subject_id_fkey FOREIGN KEY (notes_updated_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: patients patients_self_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT patients_self_user_subject_id_fkey FOREIGN KEY (self_user_subject_id) REFERENCES public.users(subject_id) ON DELETE SET NULL;


--
-- Name: proposals proposals_base_commit_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT proposals_base_commit_fkey FOREIGN KEY (base_commit) REFERENCES public.commits(commit_hash) ON DELETE RESTRICT;


--
-- Name: proposals proposals_merge_commit_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT proposals_merge_commit_fkey FOREIGN KEY (merge_commit) REFERENCES public.commits(commit_hash) ON DELETE RESTRICT;


--
-- Name: proposals proposals_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT proposals_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: proposals proposals_proposer_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT proposals_proposer_subject_id_fkey FOREIGN KEY (proposer_subject_id) REFERENCES public.subjects(id) ON DELETE RESTRICT;


--
-- Name: proposals proposals_reviewed_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT proposals_reviewed_by_subject_id_fkey FOREIGN KEY (reviewed_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: proposals proposals_source_head_commit_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT proposals_source_head_commit_fkey FOREIGN KEY (source_head_commit) REFERENCES public.commits(commit_hash) ON DELETE RESTRICT;


--
-- Name: proposals proposals_target_head_commit_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT proposals_target_head_commit_fkey FOREIGN KEY (target_head_commit) REFERENCES public.commits(commit_hash) ON DELETE RESTRICT;


--
-- Name: provenance_events provenance_events_agent_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provenance_events
    ADD CONSTRAINT provenance_events_agent_subject_id_fkey FOREIGN KEY (agent_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: provenance_events provenance_events_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provenance_events
    ADD CONSTRAINT provenance_events_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: redaction_events redaction_events_applied_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.redaction_events
    ADD CONSTRAINT redaction_events_applied_by_subject_id_fkey FOREIGN KEY (applied_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: redaction_events redaction_events_reviewer_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.redaction_events
    ADD CONSTRAINT redaction_events_reviewer_subject_id_fkey FOREIGN KEY (reviewer_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: ref_log ref_log_actor_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_log
    ADD CONSTRAINT ref_log_actor_subject_id_fkey FOREIGN KEY (actor_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: ref_log ref_log_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_log
    ADD CONSTRAINT ref_log_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: refs refs_commit_hash_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_commit_hash_fkey FOREIGN KEY (commit_hash) REFERENCES public.commits(commit_hash) ON DELETE RESTRICT;


--
-- Name: refs refs_owner_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_owner_subject_id_fkey FOREIGN KEY (owner_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: refs refs_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refs
    ADD CONSTRAINT refs_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: registrations registrations_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registrations
    ADD CONSTRAINT registrations_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: registrations registrations_fixed_series_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registrations
    ADD CONSTRAINT registrations_fixed_series_id_fkey FOREIGN KEY (fixed_series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: registrations registrations_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registrations
    ADD CONSTRAINT registrations_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: registrations registrations_moving_series_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registrations
    ADD CONSTRAINT registrations_moving_series_id_fkey FOREIGN KEY (moving_series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: registrations registrations_requested_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registrations
    ADD CONSTRAINT registrations_requested_by_subject_id_fkey FOREIGN KEY (requested_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: reindex_jobs reindex_jobs_created_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reindex_jobs
    ADD CONSTRAINT reindex_jobs_created_by_subject_id_fkey FOREIGN KEY (created_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: report_content_citations report_content_citations_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_content_citations
    ADD CONSTRAINT report_content_citations_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: report_content_citations report_content_citations_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_content_citations
    ADD CONSTRAINT report_content_citations_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.document_files(id) ON DELETE SET NULL;


--
-- Name: report_content_citations report_content_citations_report_content_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_content_citations
    ADD CONSTRAINT report_content_citations_report_content_id_fkey FOREIGN KEY (report_content_id) REFERENCES public.report_contents(id) ON DELETE CASCADE;


--
-- Name: report_contents report_contents_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_contents
    ADD CONSTRAINT report_contents_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: report_contents report_contents_authority_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_contents
    ADD CONSTRAINT report_contents_authority_id_fkey FOREIGN KEY (authority_id) REFERENCES public.document_authorities(id) ON DELETE RESTRICT;


--
-- Name: report_contents report_contents_clinical_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_contents
    ADD CONSTRAINT report_contents_clinical_event_id_fkey FOREIGN KEY (clinical_event_id) REFERENCES public.clinical_events(id) ON DELETE CASCADE;


--
-- Name: report_contents report_contents_created_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_contents
    ADD CONSTRAINT report_contents_created_by_subject_id_fkey FOREIGN KEY (created_by_subject_id) REFERENCES public.subjects(id) ON DELETE RESTRICT;


--
-- Name: report_contents report_contents_endorsed_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_contents
    ADD CONSTRAINT report_contents_endorsed_by_subject_id_fkey FOREIGN KEY (endorsed_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: report_contents report_contents_signed_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_contents
    ADD CONSTRAINT report_contents_signed_by_subject_id_fkey FOREIGN KEY (signed_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: report_contents report_contents_superseded_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_contents
    ADD CONSTRAINT report_contents_superseded_by_id_fkey FOREIGN KEY (superseded_by_id) REFERENCES public.report_contents(id) ON DELETE SET NULL;


--
-- Name: revoked_tokens revoked_tokens_revoked_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.revoked_tokens
    ADD CONSTRAINT revoked_tokens_revoked_by_subject_id_fkey FOREIGN KEY (revoked_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: revoked_tokens revoked_tokens_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.revoked_tokens
    ADD CONSTRAINT revoked_tokens_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: segmentations segmentations_agent_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.segmentations
    ADD CONSTRAINT segmentations_agent_token_id_fkey FOREIGN KEY (agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;


--
-- Name: segmentations segmentations_created_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.segmentations
    ADD CONSTRAINT segmentations_created_by_subject_id_fkey FOREIGN KEY (created_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: segmentations segmentations_series_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.segmentations
    ADD CONSTRAINT segmentations_series_id_fkey FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: series series_study_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series
    ADD CONSTRAINT series_study_id_fkey FOREIGN KEY (study_id) REFERENCES public.imaging_studies(id) ON DELETE CASCADE;


--
-- Name: share_links share_links_ai_sponsorship_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.share_links
    ADD CONSTRAINT share_links_ai_sponsorship_id_fkey FOREIGN KEY (ai_sponsorship_id) REFERENCES public.wallet_sponsorships(id) ON DELETE SET NULL;


--
-- Name: share_links share_links_claimed_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.share_links
    ADD CONSTRAINT share_links_claimed_by_subject_id_fkey FOREIGN KEY (claimed_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: share_links share_links_grant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.share_links
    ADD CONSTRAINT share_links_grant_id_fkey FOREIGN KEY (grant_id) REFERENCES public.grants(id) ON DELETE CASCADE;


--
-- Name: share_links share_links_prepared_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.share_links
    ADD CONSTRAINT share_links_prepared_job_id_fkey FOREIGN KEY (prepared_job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: imaging_studies studies_owner_org_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.imaging_studies
    ADD CONSTRAINT studies_owner_org_subject_id_fkey FOREIGN KEY (owner_org_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: imaging_studies studies_owner_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.imaging_studies
    ADD CONSTRAINT studies_owner_subject_id_fkey FOREIGN KEY (owner_subject_id) REFERENCES public.subjects(id) ON DELETE RESTRICT;


--
-- Name: imaging_studies studies_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.imaging_studies
    ADD CONSTRAINT studies_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE SET NULL;


--
-- Name: tags tags_created_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT tags_created_by_subject_id_fkey FOREIGN KEY (created_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: telegram_link_codes telegram_link_codes_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_link_codes
    ADD CONSTRAINT telegram_link_codes_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.patient_contacts(id) ON DELETE CASCADE;


--
-- Name: telegram_link_codes telegram_link_codes_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_link_codes
    ADD CONSTRAINT telegram_link_codes_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id) ON DELETE CASCADE;


--
-- Name: training_consents training_consents_study_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.training_consents
    ADD CONSTRAINT training_consents_study_id_fkey FOREIGN KEY (study_id) REFERENCES public.imaging_studies(id) ON DELETE CASCADE;


--
-- Name: training_consents training_consents_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.training_consents
    ADD CONSTRAINT training_consents_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: user_api_keys user_api_keys_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_api_keys
    ADD CONSTRAINT user_api_keys_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: users users_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: viewport_states viewport_states_series_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.viewport_states
    ADD CONSTRAINT viewport_states_series_id_fkey FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: viewport_states viewport_states_user_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.viewport_states
    ADD CONSTRAINT viewport_states_user_subject_id_fkey FOREIGN KEY (user_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: wallet_sponsorship_audit wallet_sponsorship_audit_actor_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_sponsorship_audit
    ADD CONSTRAINT wallet_sponsorship_audit_actor_subject_id_fkey FOREIGN KEY (actor_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: wallet_sponsorship_audit wallet_sponsorship_audit_sponsorship_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_sponsorship_audit
    ADD CONSTRAINT wallet_sponsorship_audit_sponsorship_id_fkey FOREIGN KEY (sponsorship_id) REFERENCES public.wallet_sponsorships(id) ON DELETE CASCADE;


--
-- Name: wallet_sponsorships wallet_sponsorships_revoked_by_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_sponsorships
    ADD CONSTRAINT wallet_sponsorships_revoked_by_subject_id_fkey FOREIGN KEY (revoked_by_subject_id) REFERENCES public.subjects(id) ON DELETE SET NULL;


--
-- Name: wallet_sponsorships wallet_sponsorships_sponsor_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_sponsorships
    ADD CONSTRAINT wallet_sponsorships_sponsor_subject_id_fkey FOREIGN KEY (sponsor_subject_id) REFERENCES public.subjects(id) ON DELETE RESTRICT;


--
-- Name: wallet_sponsorships wallet_sponsorships_sponsored_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_sponsorships
    ADD CONSTRAINT wallet_sponsorships_sponsored_subject_id_fkey FOREIGN KEY (sponsored_subject_id) REFERENCES public.subjects(id) ON DELETE CASCADE;


--
-- Name: app_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.app_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: app_settings app_settings_modify; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY app_settings_modify ON public.app_settings USING (public.app_is_service()) WITH CHECK (public.app_is_service());


--
-- Name: app_settings app_settings_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY app_settings_select ON public.app_settings FOR SELECT USING ((((scope)::text = 'public'::text) OR public.app_is_service()));


--
-- Name: binary_blobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.binary_blobs ENABLE ROW LEVEL SECURITY;

--
-- Name: binary_blobs binary_blobs_modify; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY binary_blobs_modify ON public.binary_blobs USING (public.app_is_service()) WITH CHECK (public.app_is_service());


--
-- Name: binary_blobs binary_blobs_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY binary_blobs_select ON public.binary_blobs FOR SELECT USING (true);


--
-- Name: clinical_notes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.clinical_notes ENABLE ROW LEVEL SECURITY;

--
-- Name: clinical_notes clinical_notes_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY clinical_notes_delete ON public.clinical_notes FOR DELETE USING ((public.app_is_service() OR (author_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = clinical_notes.patient_id) AND (p.managed_by_subject_id = public.app_current_subject()))))));


--
-- Name: clinical_notes clinical_notes_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY clinical_notes_insert ON public.clinical_notes FOR INSERT WITH CHECK ((public.app_is_service() OR ((author_subject_id = public.app_current_subject()) AND (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = clinical_notes.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id)))))))))))));


--
-- Name: clinical_notes clinical_notes_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY clinical_notes_select ON public.clinical_notes FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = clinical_notes.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: clinical_notes clinical_notes_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY clinical_notes_update ON public.clinical_notes FOR UPDATE USING ((public.app_is_service() OR (author_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = clinical_notes.patient_id) AND (p.managed_by_subject_id = public.app_current_subject())))))) WITH CHECK ((public.app_is_service() OR (author_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = clinical_notes.patient_id) AND (p.managed_by_subject_id = public.app_current_subject()))))));


--
-- Name: commits; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.commits ENABLE ROW LEVEL SECURITY;

--
-- Name: commits commits_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY commits_insert ON public.commits FOR INSERT WITH CHECK (public.app_is_service());


--
-- Name: commits commits_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY commits_select ON public.commits FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = commits.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: document_files; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.document_files ENABLE ROW LEVEL SECURITY;

--
-- Name: documents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;

--
-- Name: entity_objects; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.entity_objects ENABLE ROW LEVEL SECURITY;

--
-- Name: entity_objects entity_objects_modify; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY entity_objects_modify ON public.entity_objects USING (public.app_is_service()) WITH CHECK (public.app_is_service());


--
-- Name: entity_objects entity_objects_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY entity_objects_select ON public.entity_objects FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.manifest_entries me
  WHERE (me.object_hash = entity_objects.object_hash)))));


--
-- Name: grants; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.grants ENABLE ROW LEVEL SECURITY;

--
-- Name: grants grants_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY grants_delete ON public.grants FOR DELETE USING ((public.app_is_service() OR (grantor_subject_id = public.app_current_subject())));


--
-- Name: grants grants_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY grants_insert ON public.grants FOR INSERT WITH CHECK ((public.app_is_service() OR (grantor_subject_id = public.app_current_subject())));


--
-- Name: grants grants_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY grants_select ON public.grants FOR SELECT USING ((public.app_is_service() OR ((grantor_subject_id = public.app_current_subject()) OR (grantee_subject_id IN ( SELECT principal_set.subject_id
   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))) OR (((resource_kind)::text = 'study'::text) AND (EXISTS ( SELECT 1
   FROM public.imaging_studies s
  WHERE ((s.id = grants.resource_id) AND (s.owner_subject_id = public.app_current_subject()))))) OR (((resource_kind)::text = 'patient'::text) AND (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = grants.resource_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject())))))))));


--
-- Name: grants grants_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY grants_update ON public.grants FOR UPDATE USING ((public.app_is_service() OR (grantor_subject_id = public.app_current_subject()))) WITH CHECK ((public.app_is_service() OR (grantor_subject_id = public.app_current_subject())));


--
-- Name: imaging_studies; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.imaging_studies ENABLE ROW LEVEL SECURITY;

--
-- Name: manifest_entries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.manifest_entries ENABLE ROW LEVEL SECURITY;

--
-- Name: manifest_entries manifest_entries_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY manifest_entries_insert ON public.manifest_entries FOR INSERT WITH CHECK (public.app_is_service());


--
-- Name: manifest_entries manifest_entries_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY manifest_entries_select ON public.manifest_entries FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM (public.commits c
     JOIN public.patients p ON ((p.id = c.patient_id)))
  WHERE ((c.commit_hash = manifest_entries.commit_hash) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: markers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.markers ENABLE ROW LEVEL SECURITY;

--
-- Name: markers markers_modify; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY markers_modify ON public.markers USING (public.app_is_service()) WITH CHECK (public.app_is_service());


--
-- Name: markers markers_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY markers_select ON public.markers FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = markers.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: merge_conflicts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.merge_conflicts ENABLE ROW LEVEL SECURITY;

--
-- Name: merge_conflicts merge_conflicts_modify; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY merge_conflicts_modify ON public.merge_conflicts USING (public.app_is_service()) WITH CHECK (public.app_is_service());


--
-- Name: merge_conflicts merge_conflicts_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY merge_conflicts_select ON public.merge_conflicts FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM (public.proposals pr
     JOIN public.patients p ON ((p.id = pr.patient_id)))
  WHERE ((pr.id = merge_conflicts.proposal_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: document_files patient_document_files_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patient_document_files_delete ON public.document_files FOR DELETE USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM (public.documents pd
     JOIN public.patients p ON ((p.id = pd.patient_id)))
  WHERE ((pd.id = document_files.document_id) AND (p.managed_by_subject_id = public.app_current_subject()))))));


--
-- Name: document_files patient_document_files_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patient_document_files_insert ON public.document_files FOR INSERT WITH CHECK ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM (public.documents pd
     JOIN public.patients p ON ((p.id = pd.patient_id)))
  WHERE ((pd.id = document_files.document_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject())))))));


--
-- Name: document_files patient_document_files_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patient_document_files_select ON public.document_files FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM (public.documents pd
     JOIN public.patients p ON ((p.id = pd.patient_id)))
  WHERE ((pd.id = document_files.document_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: documents patient_documents_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patient_documents_delete ON public.documents FOR DELETE USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = documents.patient_id) AND (p.managed_by_subject_id = public.app_current_subject()))))));


--
-- Name: documents patient_documents_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patient_documents_insert ON public.documents FOR INSERT WITH CHECK ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = documents.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject())))))));


--
-- Name: documents patient_documents_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patient_documents_select ON public.documents FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = documents.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: documents patient_documents_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patient_documents_update ON public.documents FOR UPDATE USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = documents.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()))))))) WITH CHECK ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = documents.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject())))))));


--
-- Name: patients; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.patients ENABLE ROW LEVEL SECURITY;

--
-- Name: patients patients_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patients_delete ON public.patients FOR DELETE USING ((public.app_is_service() OR (managed_by_subject_id = public.app_current_subject())));


--
-- Name: patients patients_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patients_insert ON public.patients FOR INSERT WITH CHECK ((public.app_is_service() OR (managed_by_subject_id = public.app_current_subject())));


--
-- Name: patients patients_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patients_select ON public.patients FOR SELECT USING ((public.app_is_service() OR ((managed_by_subject_id = public.app_current_subject()) OR (self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
   FROM public.grants g
  WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = patients.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
           FROM public.principal_set(public.app_current_subject()) principal_set(subject_id)))))))));


--
-- Name: patients patients_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY patients_update ON public.patients FOR UPDATE USING ((public.app_is_service() OR ((managed_by_subject_id = public.app_current_subject()) OR (self_user_subject_id = public.app_current_subject())))) WITH CHECK ((public.app_is_service() OR ((managed_by_subject_id = public.app_current_subject()) OR (self_user_subject_id = public.app_current_subject()))));


--
-- Name: proposals; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.proposals ENABLE ROW LEVEL SECURITY;

--
-- Name: proposals proposals_modify; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY proposals_modify ON public.proposals USING (public.app_is_service()) WITH CHECK (public.app_is_service());


--
-- Name: proposals proposals_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY proposals_select ON public.proposals FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = proposals.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: redaction_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.redaction_events ENABLE ROW LEVEL SECURITY;

--
-- Name: redaction_events redaction_events_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY redaction_events_insert ON public.redaction_events FOR INSERT WITH CHECK (public.app_is_service());


--
-- Name: redaction_events redaction_events_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY redaction_events_select ON public.redaction_events FOR SELECT USING ((public.app_is_service() OR (public.app_current_subject() IS NOT NULL)));


--
-- Name: ref_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ref_log ENABLE ROW LEVEL SECURITY;

--
-- Name: ref_log ref_log_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY ref_log_insert ON public.ref_log FOR INSERT WITH CHECK (public.app_is_service());


--
-- Name: ref_log ref_log_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY ref_log_select ON public.ref_log FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = ref_log.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: refs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.refs ENABLE ROW LEVEL SECURITY;

--
-- Name: refs refs_modify; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY refs_modify ON public.refs USING (public.app_is_service()) WITH CHECK (public.app_is_service());


--
-- Name: refs refs_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY refs_select ON public.refs FOR SELECT USING ((public.app_is_service() OR (EXISTS ( SELECT 1
   FROM public.patients p
  WHERE ((p.id = refs.patient_id) AND ((p.managed_by_subject_id = public.app_current_subject()) OR (p.self_user_subject_id = public.app_current_subject()) OR (EXISTS ( SELECT 1
           FROM public.grants g
          WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = p.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
                   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))))));


--
-- Name: imaging_studies studies_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY studies_delete ON public.imaging_studies FOR DELETE USING ((public.app_is_service() OR (owner_subject_id = public.app_current_subject())));


--
-- Name: imaging_studies studies_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY studies_insert ON public.imaging_studies FOR INSERT WITH CHECK ((public.app_is_service() OR (owner_subject_id = public.app_current_subject())));


--
-- Name: imaging_studies studies_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY studies_select ON public.imaging_studies FOR SELECT USING ((public.app_is_service() OR (is_public OR (owner_subject_id = public.app_current_subject()) OR ((owner_org_subject_id IS NOT NULL) AND (owner_org_subject_id IN ( SELECT principal_set.subject_id
   FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))) OR ((EXISTS ( SELECT 1
   FROM public.grants g
  WHERE (((g.resource_kind)::text = 'study'::text) AND (g.resource_id = imaging_studies.id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
           FROM public.principal_set(public.app_current_subject()) principal_set(subject_id)))))) OR ((patient_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM public.grants g
  WHERE (((g.resource_kind)::text = 'patient'::text) AND (g.resource_id = imaging_studies.patient_id) AND (g.revoked_at IS NULL) AND (g.valid_from <= now()) AND ((g.valid_until IS NULL) OR (g.valid_until >= now())) AND (g.grantee_subject_id IN ( SELECT principal_set.subject_id
           FROM public.principal_set(public.app_current_subject()) principal_set(subject_id))))))))));


--
-- Name: imaging_studies studies_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY studies_update ON public.imaging_studies FOR UPDATE USING ((public.app_is_service() OR (owner_subject_id = public.app_current_subject()))) WITH CHECK ((public.app_is_service() OR (owner_subject_id = public.app_current_subject())));


--
-- PostgreSQL database dump complete
--


