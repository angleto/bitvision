# ADR 0006: Soft-delete documenti + purge_after retention

**Status**: Amended by [ADR 0020](./0020-documents-hardlinks-and-no-orphan-invariant.md)
**Date**: 2026-04-30
**Deciders**: Angelo Leto

> **Amendment 2026-05-06 (ADR 0020).** The 30-day automatic purge
> window described below has been retired. `delete_document` now sets
> `purge_after = NULL` by default — the document is soft-deleted and
> restorable indefinitely (git-like model). The `?force=true` admin
> escape-hatch is gated on the new `documents:purge` scope and is
> intended only for GDPR right-to-erasure, never for routine work.
> The trigger-based no-orphan invariant (ADR 0020 § C) replaces the
> "document can be orphan" assumption that some sections below relied
> on.

## Context

Spec sezione 3.3 richiede soft-delete reversibile su `PatientDocument`,
con finestra di restore configurabile (default 30 giorni) prima del
hard-delete via job notturno. Il codebase attuale non implementa
soft-delete su questa entità.

Vincoli ortogonali:

- L'audit log deve sopravvivere al hard-delete del documento
  (compliance retention 10 anni).
- Soft-deleted documents devono mantenere link verso studi (Document↔Study
  link) per ripristinarli al restore.
- Hard-delete da job notturno deve essere idempotent e safe in caso di
  worker restart.

## Decision

Schema:

- Aggiungere campi su `PatientDocument`:
  - `deleted_at TIMESTAMPTZ NULL`
  - `purge_after TIMESTAMPTZ NULL` (computed: `deleted_at + retention_days`)
  - `delete_reason TEXT NULL`
  - `deleted_by_subject_id UUID NULL`
- Default retention: 30 giorni, configurabile via setting
  `BVP_DOCUMENT_SOFT_DELETE_RETENTION_DAYS`.
- Query default `GET /documents` esclude `deleted_at IS NOT NULL`.
- Flag `?include_deleted=true` (richiede scope `documents:read`) include
  i soft-deleted con campo response `is_deleted: true`.
- Endpoint `POST /api/patients/:pid/documents/:did/restore` setta
  `deleted_at = NULL`, `purge_after = NULL`. Ritorna nuovo ETag (commit
  DAG di restore).
- Document↔Study link preservati durante soft-delete; restore li ripristina
  automaticamente.
- Job `purge_expired_documents` (Arq cron, daily, 03:00 UTC) batch
  delete delle righe con `purge_after < now()`. Best-effort delete dei
  file S3, log dei fallimenti.
- L'audit log entry "document.deleted" e "document.hard_deleted" sono
  permanenti.

## Consequences

### Positive

- UX clinica: errore di cancellazione recuperabile per 30 giorni.
- Compliance: audit log preserva history anche post-purge.
- Pattern coerente con `jobs.cleanup_expired_jobs` esistente
  (`workers/src/bvworkers/tasks/cleanup_jobs.py`).

### Negative

- Tabella `patient_documents` mantiene righe "morte" per fino a 30
  giorni. Dimensione contenuta (metadata + ref a S3, no binari).
- Query plan deve sempre filtrare `deleted_at IS NULL`. Indice parziale
  utile.
- Edge case: hard-delete del documento NON elimina il record audit
  associato. La FK `audit_log.resource_id` resta dangling per design.

### Mitigazioni

- Indice parziale: `CREATE INDEX ix_documents_active ON patient_documents (patient_id) WHERE deleted_at IS NULL`.
- View `patient_documents_active` per query del frontend (riduce
  rischio di forgetting filter).
- Audit query supporta resource_id orphan tramite UNION (UI mostra
  "documento non più disponibile").

## Alternatives considered

- **Hard-delete immediato senza soft phase**: irreversibile, non OK in
  contesto clinico. Rifiutato.
- **Soft-delete senza purge automatico**: tabella cresce all'infinito.
  Manutenzione manuale insostenibile.
- **Tabella separata `deleted_documents`**: doppia query per le ricerche
  che includono i deleted. Più complesso del campo nullable.
- **Retention configurabile per-tenant**: non ora; aggiungibile in
  futuro tramite override su setting per `tenant_id`.

## Implementation hooks

- Migration Alembic in Sprint 3.
- `db/models/patients.py`: estensione `PatientDocument`.
- `api/patients.py`: endpoint DELETE + restore.
- `workers/src/bvworkers/tasks/purge_documents.py`: nuovo task con
  registrazione cron in `workers/src/bvworkers/main.py`.
- Test in `tests/test_soft_delete.py`:
  - DELETE → list default esclude → restore → list default include.
  - `?include_deleted=true` mostra entrambi.
  - Doc↔Study link preservati durante soft-delete e restore.
  - Hard-delete simulato (`pytest.fixture` advance time) → audit log
    intatto.

## Note compliance

- Hard-delete da "right to be forgotten" non passa da questo flusso (è
  procedura separata, vedi spec sezione 7).
- `delete_reason` testo libero, soggetto a PHI redaction in log.
