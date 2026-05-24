# ADR 0001: Document versioning sul DAG git-like

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

La spec Agents API (sezione 3.6) richiede version history per i metadati
documento, con diff per-campo e provenance dell'autore (incluso `agent_id`).
Due opzioni concrete erano sul tavolo:

A. **Riuso del DAG git-like esistente** (`db/models/versioning.py`,
   `services/versioning.py`). Il sistema modella `EntityObject`
   content-addressed, `Commit` con DAG di parent, `Ref` (branch).
   Già usato in produzione per `Consultation` con branch
   `consultation/<id>` e supporto a proposal/merge.

B. **Tabella `document_versions` ad-hoc** append-only con colonne
   `etag`, `changed_at`, `changed_by`, `diff JSONB`, `reason`. Pattern
   semplice, scrittura veloce, niente proposal/merge.

## Decision

**Riuso del DAG git-like esistente.** Ogni `PatientDocument` versionabile ha
un branch dedicato (es. `document/<uuid>`); il PATCH crea un `Commit` con
diff dei campi metadati. L'`ETag` esposto in HTTP è il `commit_hash`
corrente del branch.

## Consequences

### Positive

- Pattern uniforme con consultazioni, riduce surface area cognitiva.
- Apre la porta a feature future già supportate dal DAG: proposta
  multi-autore, merge conflict resolution, branch per "audit reset" di
  uno stato passato.
- Provenance per-commit già modellato (`Commit.author_subject_id`,
  `Commit.message`, custom metadata field).
- Gli ETag sono content-addressable, deterministici, immutabili: meno
  edge case rispetto a counter monotonici.

### Negative

- Latency PATCH aumenta (10-30 ms misurati su entità simili). Va
  validata su workload realistico (target p95 < 100 ms).
- Schema più indiretto: query "quali sono le ultime 10 versioni?" passa
  da una tabella append-only banale a un walk del DAG.
- Backfill: i documenti esistenti non hanno branch; serve migration che
  crea il primo commit "import iniziale" per ognuno.

### Mitigazioni

- Indice apposito su `Commit.created_at` e su `Ref` per ridurre cost del
  walk.
- Backfill in singolo job batch al deploy di Sprint 3 (storia versionata
  solo da quel punto in avanti, OK per audit ai fini clinici).

## Alternatives considered

- **Tabella `document_versions` ad-hoc** (opzione B): più semplice ma
  diverge dal pattern delle consultazioni e blocca feature future
  (proposta/merge) che la spec lascia aperte per Phase 3+.

- **Event sourcing puro** (append-only event log, snapshot rebuild):
  più potente ma overhead di implementazione e operations sproporzionato
  per il caso d'uso "metadati documento".

## Implementation hooks

- `services/versioning.py` esposto come Document API: nuova funzione
  `commit_document_metadata(doc_id, patch, author, reason)`.
- `api/_etag.py` (Sprint 1): `etag_for(doc)` ritorna
  `versioning.head_commit_hash(f"document/{doc.id}")`.
- Migration "init document branches" alla prima release di S3.
