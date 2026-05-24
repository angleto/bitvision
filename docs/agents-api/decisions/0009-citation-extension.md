# ADR 0009: Citation file_id, page, bbox extension

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Lo schema `ConsultationCitation` esistente
(`db/models/consultations.py:134`) ha campi `target_kind`, `target_id`,
`excerpt`. Sufficiente per "questo consulto cita questo studio", non
per "questo consulto cita la riga 5 di pagina 2 del documento X
all'interno del file PDF Y".

Spec sezione 4.6 propone estensione con `page`, `bbox`, `slice_idx` per
puntamento granulare. L'analisi della spec ha rilevato un gap: nessun
`file_id`, ma un documento può avere N file (vedi merge ADR 0006 con
`preserve_files_as_attachments`).

## Decision

Estendere `ConsultationCitation` con campi opzionali:

- `file_id UUID NULL` (FK a `PatientDocumentFile`). Default: primo
  file del documento se mono-file. Ambiguo per multi-file: agent deve
  specificare.
- `page INT NULL` (1-indexed). Solo per `target_kind in (document, file)`.
- `bbox JSONB NULL` con shape `{x, y, w, h}` in coordinate relative `[0, 1]`
  della pagina. Permette responsive rendering frontend.
- `slice_idx INT NULL`. Solo per `target_kind in (series)`.
- `lab_value_id UUID NULL`. Solo per `target_kind: lab_value` (FK
  futuro a tabella entities/lab_values quando esiste).

Vincoli applicativi (validati a livello service, non solo DB):

- `bbox` richiede `page` (non c'è bbox senza page).
- `page` richiede `target_kind in (document, file)`.
- `slice_idx` richiede `target_kind: series`.
- `file_id` richiede `target_kind in (document, file)` e
  `file_id.document_id == target_id` (cross-validation).
- Cross-patient: il file/document/series citato DEVE appartenere allo
  stesso paziente del consultation (riuso pattern `evidence_links.py`).

Migration retrocompatibile: tutte le citazioni esistenti hanno i nuovi
campi NULL. Nessun rewrite forzato.

Frontend rendering (Sprint 4):

- Document/file con `bbox` e `page`: highlight box su PDF viewer
  (esistente per uploaded docs).
- Series con `slice_idx`: link a slice viewer con preset `axial` (default).
- Lab value: link a timeline analyte (se `lab_value_id` presente).

## Consequences

### Positive

- Citazioni puntuali aprono UX "click su citazione: salta alla riga
  esatta", che riduce l'overhead del clinico nel verificare il
  consulto.
- Compatible con OCR `bbox_words` (vedi ADR 0007): l'agent può
  generare citation programmaticamente puntando a span del testo OCR.
- Schema esteso ma optional: niente rotture per consumer esistenti.

### Negative

- Validazione cross-field complessa: facile sbagliare a livello UI,
  serve validation Pydantic robusta.
- Frontend deve gestire molte combinazioni di campi opzionali.
- Page numbering 1-indexed può generare bug con array 0-indexed:
  documentare convention esplicita.

## Alternatives considered

- **Solo target_kind + target_id senza estensione**: spec originale
  prima della granularità. Funziona ma UX inferiore.
- **bbox in coordinate pixel**: blocca responsive rendering con zoom.
  Coordinate relative [0,1] sono future-proof.
- **String "anchor" tipo "#page2-paragraph3"**: human-readable ma
  fragile (cambi minimi al layout rompono i link). Coordinate sono
  più robuste.
- **Vincoli cross-field via DB constraint anziché service**: postgres
  CHECK è verbose, gestione errore meno pulita di Pydantic
  validation.

## Implementation hooks

- Migration Alembic in Sprint 4 aggiunge colonne nullable.
- `db/models/consultations.py`: estensione `ConsultationCitation`.
- `services/consultations.py` (esistente): validazione cross-field
  in helper `validate_citation_target(citation)`.
- `api/consultations.py`: response include nuovi campi.
- MCP tool `create_consultation` (existing): schema input esteso.
- Frontend: `frontend/src/components/CitationRenderer.tsx` (nuovo).
- Test:
  - Citation document+page+bbox: ok.
  - Citation series+slice_idx: ok.
  - Citation document+slice_idx: validation error.
  - Citation file_id non appartenente al document: validation error.
  - Citation cross-patient: 422.
