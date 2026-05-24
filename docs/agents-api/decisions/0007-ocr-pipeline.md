# ADR 0007: OCR pipeline (Tesseract + pdfminer)

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 4.2 richiede testo OCR cached server-side, esposto via
endpoint `GET /api/patients/:pid/documents/:did/text`. Use case: agente
estrae entità cliniche, cita testualmente, indicizza per search.

Stato attuale: il campo `text` esiste su `PatientDocument` ma è
popolato in modo ad-hoc, niente pipeline strutturata, niente cache
versionata, niente bbox per page.

Vincoli:

- PDF text-layer (vendor referti standard): testo già nel PDF, OCR è
  spreco di CPU.
- Scansioni JPEG/PNG: serve OCR, output con bounding box per
  citazioni granulari (spec 4.6).
- Lingua principale italiana, possibile latino/inglese in referti.
- Re-OCR forzato deve invalidare la cache.

## Decision

Pipeline a due engine, scelta automatica:

1. **PDF text-layer** → estrazione via `pdfminer.six` (puro Python, no
   external binary). Veloce, preserva layout, niente bbox per parola
   nativamente ma `bbox per linea` deducibile.
2. **Scansione (JPEG/PNG, PDF senza text-layer rilevabile)** → Tesseract
   5 con language pack `ita+eng`. Output include `bbox_words[]` con
   coordinate normalizzate.
3. **Force re-OCR** (`POST /api/patients/:pid/documents/:did/text?force=true`):
   bypassa lo skip e usa Tesseract anche su PDF text-layer (utile se il
   text-layer è corrotto).

Cache:

- Schema chiave: `(file_id, content_sha256, ocr_engine_version)`.
- Persistenza in tabella nuova `document_ocr_cache`:
  ```
  id (UUID)
  file_id (FK PatientDocumentFile)
  content_sha256 (TEXT)
  ocr_engine (TEXT, "pdfminer-X.Y" | "tesseract-5.3-ita")
  language (TEXT)
  text (TEXT)
  pages JSONB ([{page, text, bbox_words: [...]}])
  ocr_confidence FLOAT NULL
  extracted_at TIMESTAMPTZ
  ```
- Invalidation: cache è naturalmente invalidata dal cambio di
  `content_sha256` (file replaced) o di `ocr_engine_version`
  (engine bump).

Esecuzione:

- Sync per file < 5 MB e PDF text-layer (latency target < 2s).
- Async via Arq job per scansioni e file grandi: enqueue, polling, vedi
  `services/jobs.py` esistente.
- Workers GPU non richiesti (Tesseract CPU-only).

## Consequences

### Positive

- Skip Tesseract su PDF text-layer riduce 80%+ del compute totale
  (tipico in dataset clinico italiano).
- Cache deterministico via sha256: replay sicuro.
- Bbox per word permette citazioni granulari (spec 4.6) lato frontend.

### Negative

- Tesseract italiano ha precision ~85% su scansioni cliniche. Aspettativa
  realistica documentata; chi vuole maggiore qualità usa re-OCR forzato
  con `?engine=paddleocr` (futuro).
- Tabella cache cresce con il fascicolo. Eviction su `extracted_at <
  now() - 1y` valutabile, ma il costo storage è basso (testo + JSON).
- Dipendenza esterna su Tesseract binary (incluso nell'immagine
  workers).

## Alternatives considered

- **PaddleOCR sostituto di Tesseract**: precision migliore ma
  dipendenza più pesante (PaddlePaddle). Aggiungibile in futuro come
  engine alternativo via `?engine=paddleocr`.
- **OCR cloud (Google Document AI, AWS Textract)**: precision ottima ma
  conflitta con compliance (PHI fuori da nostre boundary). Rifiutato.
- **OCR client-side (browser)**: latency interactive, ma costo CPU sul
  client e UX inconsistent. Rifiutato.
- **Cache per documento intero anziché per file**: documenti
  multi-file richiederebbero concatenazione manuale e re-OCR di tutto
  al cambio di un singolo file. Più semplice cachare per `file_id`.

## Implementation hooks

- `workers/src/bvworkers/tasks/ocr.py` (Sprint 3): nuovo task
  `ocr_document_files(document_id)`.
- `services/ocr.py` (Sprint 3): orchestrator, decisione engine,
  cache lookup/insert.
- `db/models/document_ocr_cache.py`: nuovo modello con migration
  Alembic.
- `api/patients.py`: endpoint GET + POST text.
- Pacchetto `tesseract-ocr` + `tesseract-ocr-ita` + `tesseract-ocr-eng`
  nel Dockerfile workers.
- Lib Python: `pytesseract` (binding) e `pdfminer.six`.

## Open questions

- Cache OCR per documento intero (concat di file ordinati) come API di
  alto livello? Da decidere prima Sprint 3 (vedi ROADMAP open
  questions). Decisione preliminare: l'endpoint
  `GET /documents/:did/text` aggrega in concatenazione ordinata; la
  cache resta per-file.
