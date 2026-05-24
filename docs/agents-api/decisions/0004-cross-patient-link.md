# ADR 0004: Cross-patient invariant per Document-Study link

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 3.5 introduce il link strutturato Document↔Study con
relazione tipizzata (`report_of`, `prep_for`, `follow_up_of`, `related`).

Il codebase ha già un invariante consolidato: **nessun link cross-patient**.
Implementato in `services/evidence_links.py` per la DSL Evidenze
(`@study:UUID`, `@document:UUID`, `@folder:UUID`, ecc.) usata nei
markdown delle consultazioni: ogni reference DEVE appartenere allo
stesso paziente del documento contenitore. Pattern:
`validate_mentions_or_raise(patient_id, mentions)` → 422 su violazione.

L'analisi della spec ha rilevato che il nuovo endpoint Document-Study
**non** richiama esplicitamente questo invariante. Senza enforcement, un
agente confuso (o un client malizioso) potrebbe collegare risorse di
pazienti diversi, rompendo isolation, audit e privacy.

## Decision

**Estendere il pattern di `evidence_links.py` ai nuovi endpoint
Document-Study link.** Implementazione:

- Nuova funzione `validate_document_study_same_patient(document_id, study_id)`
  in `services/evidence_links.py` o modulo gemello dedicato.
- Chiamata in tutti gli endpoint `POST /api/patients/:pid/documents/:did/studies/:sid`
  e in `MCP tool link_document_to_study`.
- Errore strutturato `cross_patient_link_forbidden` (422) con dettaglio
  delle risorse incriminate (UUID dei pazienti coinvolti, mai PHI).
- Acceptance test esplicito (Sprint 2) che tenta link cross-patient e
  verifica 422.

## Consequences

### Positive

- Invariante coerente con il resto del codebase, niente sorprese.
- Audit log indica chiaramente i tentativi cross-patient (forensics
  utile in caso di token compromission).
- Difesa-in-profondità: anche se RLS è aggirata, lo strato app rifiuta.

### Negative

- Una validazione DB extra per ogni link (cost basso: lookup PK).
- Edge case: studi importati da fonti esterne potrebbero avere
  `patient_id` non ancora consolidato. In tal caso il link va rifiutato
  finché lo studio non è collegato al paziente; documentato negli error
  message.

## Alternatives considered

- **Enforce solo via RLS Postgres**: la query del POST passa o fallisce
  in base a riga visibile. Funziona ma 1) errore generico 404 invece di
  422 esplicito, 2) policy RLS ha edge case di mantenibilità, 3) non
  copre il caso dove l'utente ha accesso a entrambi i pazienti.

- **Solo nei test, niente enforcement run-time**: rischio di drift.

## Implementation hooks

- `services/evidence_links.py`: aggiungere
  ```python
  def validate_document_study_same_patient(
      session: Session,
      document_id: UUID,
      study_id: UUID,
  ) -> None:
      """Raise CrossPatientLinkForbidden if document and study belong
      to different patients."""
  ```
- Endpoint `POST /api/patients/{patient_id}/documents/{document_id}/studies/{study_id}`:
  - Valida `document.patient_id == path.patient_id`.
  - Valida `study.patient_id == path.patient_id` (più restrittivo).
- Test cases in `tests/test_document_study_links.py`:
  - Same patient → 200.
  - Document patient A, study patient B → 422.
  - Study senza patient_id collegato → 422.
- MCP tool `link_document_to_study` ritorna l'errore senza traceback,
  user-friendly.
