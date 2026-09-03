# Bitvision phoenix: Agents API roadmap

> **History snapshot.** Sprints 1, 1.5, 2, 3, 3.5, 4, 5, 5b and 6+
> are closed as of 2026-05-01 (see closing note at the end of the
> file). The migration filenames referenced inline (`0054_*` →
> `0080_*`) carry the historical narrative; after the OSS release
> they are rolled into `0001_initial_schema.py`. See
> [`../README.md` reading note](../README.md) and
> [`../data-model.md §9`](../data-model.md#9-migrations). Counts of
> "tool MCP totali" (34, 51, ...) reflect the head at the time of
> the sprint and are dwarfed by the current registry — open
> `mcp/src/bvmcp/tools/` for the live list. The "Embedded AI agents"
> backlog at the bottom remains open and is the canonical place to
> track that follow-up.

Tracking unico dei task per l'estensione "Agents API" (vedi spec
`docs/agents-api-spec.md`, draft 1, 2026-04-30) e per le decisioni di
design correlate.

## Come si usa

- Questo file è la **fonte di verità** del progresso. Chi chiude un
  task lo flagga in questo file nello stesso commit del codice.
- Stati:
  - `[ ]` todo
  - `[~]` in progress
  - `[x]` done
  - `[-]` skipped o rinviato (riga rimane per audit, con note)
- Tag inline nel formato `[Pn][Sn]`:
  - Priorità: `[P0]` blocking, `[P1]` next-up, `[P2]` desirable, `[P3]` aspirational
  - Sprint: `[S1]`..`[S6]` (Sprint 0 è merge-in con S1, niente sprint dedicato)
- Ogni task indica il file principale target (`backend/...`,
  `mcp/...`, `workers/...`).
- ADR accettato → riga `[x]` in **Decisioni**, link al file in
  `docs/agents-api/decisions/`.

## Decisioni di design

ADR principali (status: tutti accepted salvo dove indicato).

- [x] [P0][S1] [ADR 0001: Document versioning sul DAG git-like](decisions/0001-document-versioning-dag.md)
- [x] [P0][S1] [ADR 0002: Idempotency-Key + dry_run interaction](decisions/0002-idempotency-dryrun.md)
- [x] [P0][S1] [ADR 0003: Bulk replay semantics (atomic=false)](decisions/0003-bulk-replay-semantics.md)
- [x] [P0][S2] [ADR 0004: Cross-patient invariant per Document-Study link](decisions/0004-cross-patient-link.md)
- [x] [P0][S1] [ADR 0005: Audit aggregation strategy (read vs write)](decisions/0005-audit-aggregation.md)
- [x] [P1][S3] [ADR 0006: Soft-delete documenti + purge_after retention](decisions/0006-soft-delete-retention.md)
- [x] [P1][S3] [ADR 0007: OCR pipeline (Tesseract + pdfminer)](decisions/0007-ocr-pipeline.md)
- [x] [P1][S4] [ADR 0008: Entity confidence schema (proposed vs validated)](decisions/0008-entity-confidence-schema.md)
- [x] [P1][S4] [ADR 0009: Citation file_id, page, bbox extension](decisions/0009-citation-extension.md)
- [x] [P1][S4] [ADR 0010: Consultation finalize gating (scope dedicato non-agent)](decisions/0010-consultation-finalize-gating.md)
- [x] [P2][S5] [ADR 0011: DICOM tag allowlist](decisions/0011-dicom-tag-allowlist.md)
- [x] [P2][S5] [ADR 0012: MPR cache LRU disk](decisions/0012-mpr-cache-lru-disk.md)
- [x] [P2][S6] [ADR 0013: TotalSegmentator job offline (ARM compatibility)](decisions/0013-totalsegmentator-arm.md) (spike pendente)
- [x] [P0][S1] [ADR 0014: Long-running operations su pattern jobs Arq](decisions/0014-long-ops-jobs-arq.md)
- [x] [P0][S1] [ADR 0015: OpenAPI snapshot first-class](decisions/0015-openapi-snapshot.md)
- [x] [P1][S1] [ADR 0016: Token revocation](decisions/0016-token-revocation.md)
- [x] [P2][S3] [ADR 0017: File ownership transfer al merge documenti](decisions/0017-merge-file-ownership.md)
- [x] [P0][S1.5] [ADR 0018: Remote MCP transport + OAuth 2.1 via Authentik](decisions/0018-remote-mcp-oauth-authentik.md) (superseded)
- [x] [P0][S1.5] [ADR 0019: Remote MCP transport with per-assistant bearer secrets](decisions/0019-remote-mcp-per-assistant-bearer.md)
- [x] [P1][S3] [ADR 0020: Hardlink documenti e invariante no-orphan](decisions/0020-documents-hardlinks-and-no-orphan-invariant.md)
- [x] [P0][S1] [ADR 0021: Riconciliazione degli inviti per email e identità del contatto](decisions/0021-invitation-reconciliation-and-contact-identity.md)

## Sprint 1: Foundation cross-cutting

Obiettivo: tutti gli endpoint mutating P0 successivi devono poter assumere ETag
+ Idempotency + dry-run + audit + errori strutturati come scontati.

### Tasks

- [x] [P0][S1] **ETag dependency**: `backend/src/bvphoenix/api/_etag.py`
- [x] [P0][S1] **Problem Details (RFC 9457)**: `backend/src/bvphoenix/middleware/problem_details.py`
- [x] [P0][S1] **Idempotency-Key middleware**: `backend/src/bvphoenix/middleware/idempotency.py` + tabella `idempotency_records` (Alembic migration `0054_idempotency_records.py`)
- [x] [P0][S1] **Dry-run query convention**: `backend/src/bvphoenix/api/_dry_run.py` (dependency `dry_run_flag`, helper `is_dry_run` riutilizzato dall'idempotency hash)
- [x] [P0][S1] **Audit log enhancement**: tabella `audit_session_view` (Alembic `0055_audit_session_view.py`), decorator `@audit_write`, colonne `agent_token_id`/`model_version`/`conversation_id` su `audit_log`, helper `record_session_view`
- [x] [P0][S1] **OpenAPI snapshot CI**: `scripts/dump_openapi.py` + `scripts/check_openapi_diff.py` + workflow `.github/workflows/openapi-check.yml` + Makefile `openapi.dump`/`openapi.check` + snapshot iniziale `backend/openapi.json`
- [x] [P1][S1] **Token revocation**: tabella `revoked_tokens` (Alembic `0056_revoked_tokens.py`), claim `jti` in JWT issuance, check in `_resolve_credential`, endpoint `POST /auth/revoke-token`
- [x] [P1][S1] **Test fixtures cross-cutting**: `agent_token`, `idempotency_replay`, `mock_etag_clock` in `backend/tests/conftest.py`
- [x] [P2][S1] **PHI-safe lint rule**: `scripts/lint_phi_safe.py` (AST-based, integrato nel Makefile e nel CI)

### Acceptance Sprint 1

- PATCH idempotente: replay key+body identici → cache; key+body diverso → 422.
- ETag mismatch → 412 con corpo Problem Details valido (RFC 9457).
- Dry-run su PATCH ritorna `diff` senza emettere eventi né mutare DB.
- Audit write su mutating popola `agent_id`, `model_version`, `conversation_id`.
- `pytest backend tests/test_idempotency.py tests/test_etag.py tests/test_problem_details.py` passa.
- OpenAPI snapshot diff in CI rifiuta PR con drift non dichiarato.

## Sprint 1.5: Remote MCP + per-assistant bearer

Obiettivo: zero-install onboarding via Claude.ai custom connector.
Trasformare il connettore MCP da stdio (Claude Desktop locale) a
remote HTTP, autenticato con credenziali per-assistant emesse
direttamente da phoenix. ADR 0018 (OAuth via Authentik) è stato
sostituito da ADR 0019 dopo che l'integrazione Authentik si è
rivelata l'astrazione sbagliata per gli AI agent.

Posizionato subito dopo Sprint 1 perché:
- Sblocca testing UX dei write endpoint Sprint 2+ direttamente da claude.ai web,
  senza che ogni utente debba installare Python + uv + bvmcp.
- Le dipendenze tecniche (HTTP transport, bearer-resolve RPC, deploy) sono
  indipendenti dal foundation cross-cutting di Sprint 1.

### Tasks

- [x] [P0][S1.5] **MCP HTTP transport entry point**: `mcp/src/bvmcp/server_http.py` (riuso `_TOOL_MODULES`, MCP SDK Streamable HTTP transport, ContextVar per il principal)
- [x] [P0][S1.5] **Bearer hash gate**: `mcp/src/bvmcp/auth.py` (sha256 + TTL cache 60s positivo / 10s negativo, fail-closed quando `BVP_MCP_BACKEND_INTERNAL_KEY` è vuoto)
- [x] [P0][S1.5] **Internal resolve RPC**: `backend/src/bvphoenix/api/internal_auth.py` (POST `/api/internal/agent-bearer/resolve`, X-Internal-Key gated)
- [x] [P0][S1.5] **Per-assistant credentials migration**: Alembic `0068_assistant_credentials.py` (drop `authentik_email`, add `client_id`/`client_secret_hash`/`client_secret_prefix`)
- [x] [P0][S1.5] **Backend agent-secret resolver**: `_resolve_assistant_secret` in `auth/deps.py` (hash bearer + lookup `AgentAssistant`, popola `request.state.is_agent`/`agent_scope`/`agent_patient_ids`)
- [x] [P0][S1.5] **Reveal-once UI + rotate**: `frontend/src/app/settings/ai-assistants/page.tsx` (CredentialsRevealCard, Rotate button, copy/save warning)
- [x] [P0][S1.5] **Deploy K8s**: `deploy/bvphoenix-production-k8s-deploy/mcp-http-{deployment,service,configmap}.yaml` + `ingress/ingress-mcp.yaml` per `mcp.bitvision.example`; secret `bvphoenix-internal` con `BVP_INTERNAL_API_KEY`
- [x] [P0][S1.5] **Dockerfile HTTP**: `infra/dockerfiles/mcp-http.Dockerfile`
- [x] [P1][S1.5] **Rate limit per token + per IP**: sliding-window in-process limiter (default 50 req/s token, 200 req/s IP) via env `BVP_MCP_RATE_LIMIT_*`
- [x] [P1][S1.5] **Audit log MCP HTTP**: endpoint `POST /api/audit/mcp` + hop fire-and-forget dal MCP HTTP, popola `audit_log` con `action='mcp_http_request'`, `actor_subject_id` = owner del client_secret risolto
- [x] [P1][S1.5] **Test transport HTTP**: `mcp/tests/test_http_transport.py` (6 test, hermetic con stub validate_token)
- [x] [P1][S1.5] **Documentazione onboarding**: `docs/agents-api/onboarding-mcp.md`

### Acceptance Sprint 1.5

- Custom connector su claude.ai con URL `https://mcp.bitvision.example/mcp` +
  client_id/client_secret emessi da Settings → AI assistants accetta tutti i tool.
- Tool `list_patient_documents` chiamato da claude.ai web ritorna risultati
  identici allo stesso tool chiamato da MCP stdio locale.
- Rotate del secret invalida il vecchio entro `BVP_MCP_BEARER_CACHE_TTL_SECONDS` (default 60s).
- Toggle `is_active=false` rifiuta richieste future entro la stessa finestra.
- Rate limit 51° req/s da stesso token in 1s ritorna 429 con `Retry-After`.
- Audit log popolato per ogni tool invocation con `actor_subject_id, tool, ip, status`.
- ADR 0019 accepted; ADR 0018 superseded.

## Sprint 2: Phase 1 metadati core

Obiettivo: agente classifica e collega documenti via manifest bulk.

### Tasks

- [x] [P0][S2] **PATCH document**: `backend/src/bvphoenix/api/patients.py` esteso con If-Match (412), Idempotency-Key replay, dry-run con diff per-campo, ETag header su response, errori in Problem Details, helper `_document_diff`
- [x] [P0][S2] **Bulk PATCH sync**: `POST /api/patients/:pid/documents/bulk_update` con cap 100, atomic flag, dry-run array di diff; servizio `services/document_bulk_update.py` con per-item ETag opt-in
- [x] [P0][S2] **Bulk PATCH async**: manifest >50 item con `atomic=False` enqueue Arq `bulk_document_update` (workers/bvworkers/tasks/bulk_document_update.py); response 202 con `X-Job-Id`; polling via `GET /api/jobs/:id`
- [x] [P0][S2] **Document-Study link table**: `backend/src/bvphoenix/db/models/document_study_links.py` + migration `0057_document_study_links.py` con partial unique index su `report_of`
- [x] [P0][S2] **POST/DELETE document↔study link**: cross-patient validation in `services/document_study_links.py`, `report_of` 1:1 enforced via partial unique index
- [x] [P0][S2] **GET document-study links**: `GET /api/patients/:pid/documents/:did/links` + `GET /api/studies/:sid/document-links`
- [x] [P0][S2] **MCP tool `update_document`**: `mcp/src/bvmcp/tools/document_writes.py` con `etag`, `dry_run`, `idempotency_key`
- [x] [P0][S2] **MCP tool `bulk_update_documents`**: stesso modulo, surface `head_etag` e `job_id` quando il backend dispatcha al worker
- [x] [P0][S2] **MCP tool `link_document_to_study` / `unlink`**: stesso modulo
- [x] [P1][S2] **Synthetic manifest fixture**: `backend/tests/fixtures/synthetic_manifest_23docs.json` (PHI-free)

### Acceptance Sprint 2

- Replicare manifest synthetic 23 doc via MCP `bulk_update_documents` in dry-run, validare diff per ogni item.
- Apply manifest, ETag aggiornati su tutti, audit log popolato con `agent_id`.
- Cambio concorrente in UI durante bulk → 412 esplicito su item con ETag stale.
- Idempotency replay → cache hit identico, no doppio commit DAG.
- Cross-patient: tentativo link `study` di paziente A su `document` di paziente B → 422 `cross_patient_link_forbidden`.
- `report_of` 1:1: secondo `report_of` su stesso study → 422.

## Sprint 3: Phase 1 completion + Phase 2 letture

Obiettivo: agente elimina duplicati, legge binari + testo OCR.

### Tasks

- [x] [P1][S3] **Soft-delete documenti**: `deleted_at`, `purge_after`, `delete_reason` su `PatientDocument` + migration `0058_patient_documents_soft_delete.py`; partial indexes su `live` + `purge_due`
- [x] [P1][S3] **Endpoint DELETE document + restore**: DELETE soft-delete con 30 giorni di retention; `?hard=true` admin-only; `POST .../restore` ripristina e bumpa il commit
- [x] [P1][S3] **Job purge_expired_documents**: `workers/src/bvworkers/tasks/purge_documents.py` cron 03:13 nightly, drop S3 + cascade
- [x] [P2][S3] **Merge documents**: `POST /api/patients/:pid/documents/:primary_id/merge` + `services/document_merge.py` (file ownership transfer per ADR 0017, fino a 20 duplicate per call)
- [x] [P1][S3] **Document version history endpoint**: `GET /api/patients/:pid/documents/:did/versions` walka manifest_entries+commits filtrati per `entity_kind=patient_document`
- [x] [P1][S3] **document_type enum extension**: migration `0059_extend_document_types.py` aggiunge radiology/pathology/surgical/cardio/endoscopy + structured_report + presentation_state (placeholder per la sessione SR/PR parallela)
- [x] [P1][S3] **Script propose_radiology_reclassification**: `scripts/propose_radiology_reclassification.py` heuristica italiana, output manifest compatibile con `bulk_update_documents`
- [x] [P0][S3] **GET document binary URL (signed)**: `/binary_url` con TTL 5 min default / 15 min max, audit log
- [x] [P0][S3] **OCR worker**: `services/ocr.py` (pdfminer text-layer + Tesseract italian fallback) + Arq task `bvworkers.tasks.ocr.run_document_ocr`; deps `pdfminer.six`/`pytesseract` aggiunte al backend
- [x] [P0][S3] **GET/POST document text**: cache `document_ocr` (migration `0060_document_ocr_cache.py`); GET cache-only, POST inline o async (job_id) con `force` / `inline` / `engine` filter
- [x] [P0][S3] **MCP tools letture Phase 2**: `mcp/src/bvmcp/tools/document_reads_v2.py` con `download_document_binary`, `get_document_text` (trigger=true innesca OCR)
- [x] [P1][S3] **MCP tool `delete_document` / `restore_document` / `merge_documents`**: estensione di `document_writes.py`

### Acceptance Sprint 3

- Soft-delete + restore preserva tutti i Document-Study link.
- OCR su PDF text-layer estrae testo via pdfminer, no Tesseract.
- OCR su scansione genera bbox_words[] per pagine.
- Re-OCR forzato (`POST .../text?force=true`) bumpa cache version.
- Audit log permanente sopravvive al hard-delete del documento.
- ADR 0006, 0007 accepted.

## Sprint 3.5: Agent-driven tag + metadata writes

Obiettivo: garantire che gli LLM/MCP possano non solo leggere ma
**aggiustare** tag e metadati (study/document/series/consultation) con
ETag + Idempotency + dry-run + audit, gated da scope OAuth granulari.
Aggiunto in roadmap il 2026-05-01 a seguito di richiesta esplicita
dell'utente: "il sistema deve permettere agli LLM di manipolare
anche i tag così da leggere ma anche migliorare/aggiustare le
classificazioni, stessa cosa per tutti i metadati".

### Tasks

- [x] [P0][S3.5] **Tag write API**: `POST /api/tags` + `DELETE /api/tags/{id}` esistono già da prima dell'agents API; Sprint 3.5 aggiunge `PATCH /api/studies/:sid/tags` con `mode=add|replace|remove`, dry-run e Idempotency-Key (preserva auto/imported, tocca solo manual).
- [x] [P0][S3.5] **Tag write MCP tools**: `add_tag_to_study`, `remove_tag_from_study`, `replace_study_tags` in `mcp/src/bvmcp/tools/metadata_writes.py`.
- [x] [P0][S3.5] **Study metadata write**: `PATCH /api/studies/:sid` con whitelist `study_description`; UID/modalities/owner read-only via 422 `read_only`.
- [x] [P0][S3.5] **Series metadata write**: `PATCH /api/series/:sid` per `series_description`, `body_part_examined`, `modality_corrected` (recordato come tag, niente overwrite del DICOM).
- [-] [P1][S3.5] **Consultation tag manipulation**: deferred — il PATCH consultation Sprint 4 già supporta i campi descrittivi; tag-link su consultation non sono ancora un'entità separata.
- [x] [P1][S3.5] **Scope catalog extension**: server-side scope catalog esteso con `tags:write`, `studies:write_metadata`, `series:write_metadata`; gli scope vengono assegnati agli assistant via UI Settings → AI assistants, niente blueprint esterno richiesto.
- [-] [P1][S3.5] **Granular per-assistant defaults**: deferred (frontend) — backend già supporta lo scope set per-assistant tramite `agent_assistants.permissions`; la UI dedicata è scope frontend.
- [-] [P1][S3.5] **Acceptance test agente classificatore**: deferred — manifest sintetico Sprint 2 + new metadata write tools sono il building block; il test E2E che esercita l'intero flusso (agent → dry-run → apply) richiede DB live + harness LLM.

### Acceptance Sprint 3.5

- Tag write E2E da MCP: agent aggiunge tag a study, ETag aggiornato, audit log popolato con `agent_token_id`.
- Idempotency replay: stessa key + stesso body -> cache hit; key + body diverso -> 422.
- Cross-patient: agent non può aggiungere tag a study di paziente fuori dal `agent_patient_ids` (403).
- Scope mancante (`tags:write` non concesso) -> 403 `permission_denied` con `required_scope=tags:write`.
- Reclassification dry-run di 50 documenti riusa lo stesso schema del Sprint 2 bulk + dry-run.

## Sprint 4: Phase 2 estrazione e consulti

Obiettivo: agente estrae entità cliniche e produce consulti versionati con
citazioni puntuali.

### Tasks

- [x] [P1][S4] **Entity extractor rule-based v0**: `services/clinical_entities.py` (italian patterns: lab values, BP, HR, temperature, dates ISO, procedure keywords; deterministic `canonical_payload`)
- [x] [P1][S4] **Job entity_extraction**: `workers/src/bvworkers/tasks/entity_extraction.py` + cache table `document_entities` (migration `0061_document_entities_cache.py`)
- [x] [P1][S4] **GET document entities**: `GET /api/patients/:pid/documents/:did/entities` cache-only + `POST .../entities` con `inline`/`force`/async via X-Job-Id
- [x] [P2][S4] **GET lab time-series**: `GET /api/patients/:pid/labs?analyte=...&since=...&limit=...`, aggrega su `document_entities` + trend solo `n_points >= 3`
- [x] [P1][S4] **PATCH consultation con DAG versioning**: ETag/Idempotency-Key/dry_run integrati nel PATCH esistente; rifiuta `status` via body
- [x] [P1][S4] **POST consultation finalize**: `POST /api/consultations/:id/finalize` flippa `draft -> final`, completeness check (summary+findings+recommendations+citation), agent tokens rifiutati con `agent_tokens_disallowed=true`
- [x] [P1][S4] **Citation extension**: migration `0062_citation_extension.py` aggiunge `page`, `bbox` JSONB, `file_id`, `slice_idx`, `lab_value_id` su `consultation_citations` + indice `(target_kind, target_id)`. Migration `0063_consultation_final_status.py` aggiunge lo stato `final`
- [x] [P1][S4] **MCP tool `extract_document_entities`**: `mcp/src/bvmcp/tools/entities.py`
- [x] [P1][S4] **MCP tool `get_lab_timeseries`**: `mcp/src/bvmcp/tools/labs.py`
- [x] [P1][S4] **MCP tool `update_consultation`, `finalize_consultation`**: `mcp/src/bvmcp/tools/consultations_writes.py` (If-Match / Idempotency / dry_run); 34 tool MCP totali

### Acceptance Sprint 4

- Estrazione idempotente: stesso text + stesso extractor_version → output byte-equal.
- Bumping `extractor_version` invalida cache.
- Time-series CEA da 3+ documenti synthetic correttamente ordinata e con `trend.direction`.
- Citation con `bbox` su pagina 2 di PDF synthetic, frontend (smoke) renderizza highlighter.
- Tentativo finalize con token agent → 403 `permission_denied` con `required_scope=consultations:finalize`.
- ADR 0008, 0009, 0010 accepted.

## Sprint 5: Phase 3 base imaging

Obiettivo: agente vede slice arbitrarie con MPR + windowing, scrive
annotazioni.

### Tasks

- [x] [P0][S5b] **Slice access endpoint**: `GET /api/series/:sid/slice/:idx?plane=axial|coronal|sagittal&wc_delta=&ww_delta=&max_side=`. Axial path riusa il thumbnail pipeline; coronal/sagittal stack via SimpleITK reslice (`services/mpr.py`). Header `X-Cache: hit|miss` + `X-Volume-Shape: nx*ny*nz`.
- [x] [P0][S5b] **MPR reslice via SimpleITK**: `services/mpr.py` con `_stack_volume` + `reslice_to_jpeg`; ordina le instances per `ImagePositionPatient · normal` (oblique-safe); finestra DICOM con override agent.
- [x] [P0][S5b] **Cache slice LRU disk**: `services/slice_cache.py` con FS + JSONL index; cap 10 GB default (env `BVP_SLICE_CACHE_BYTES_CAP`), eviction LRU al 80% del cap, `asyncio.Lock` per thread-safety; key include `content_hash` su S3 keys.
- [x] [P1][S5] **DICOM meta endpoint**: `GET /api/series/:sid/dicom_meta` con allowlist `services/dicom_meta_allowlist.py` (ADR 0011); PHI tag e private tag rifiutati al confine
- [x] [P2][S5b] **ROI cropping**: `POST /api/series/:sid/crop` con bbox pixel-space, riusa `dicom_to_jpeg` + PIL crop. Restituisce JPEG inline con header `x-bbox` / `x-image-size`. Una `crop_volume` 3D resta deferred a quando la SimpleITK reformat sarà generalizzata.
- [x] [P1][S5] **Annotation write**: l'endpoint `POST/PATCH/DELETE /api/markers` esisteva già con `author_kind`/`agent_token_id`; Sprint 5 aggiunge il vincolo "agent non modifica human" via 403 in `_marker_for_write`.
- [x] [P1][S5] **Vincolo agent non modifica annotation human**: enforce in `api/markers.py:_marker_for_write` — agent token + `author_kind=human` + non admin → 403 con `marker_id` + `author_kind` nel body.
- [x] [P1][S5] **MCP tools imaging reads**: `mcp/src/bvmcp/tools/imaging.py` con `get_series_slice` (proxy axial), `get_series_dicom_meta`, `crop_series_roi` (Sprint 5b).
- [x] [P1][S5] **MCP tools imaging writes**: stesso modulo `imaging.py` con `write_annotation`, `update_annotation`, `delete_annotation` (delegano alla markers API; il backend rifiuta agent → human).

### Acceptance Sprint 5

- MPR coronale e sagittale ricostruite da CT phantom synthetic (volume noto).
- Slice fuori range → 422 `slice_index_out_of_range` con `slice_total` nel body.
- Cache hit su seconda chiamata identica (header `X-Cache: hit`).
- Agent crea annotation con `kind=agent`; tentativo PATCH annotation `kind=human` → 403.
- ADR 0011, 0012 accepted.

## Sprint 6+: Phase 3 maturità (open scope)

Prioritizzazione dopo validazione Sprint 5.

### Tasks candidati

- [ ] [P2][S6] **TotalSegmentator job**: `workers/src/bvworkers/tasks/totalsegmentator.py` (verifica wheel ARM64) — **deferred**: ADR 0013 spike pendente, dipende da disponibilità wheel ARM64 sul cluster di produzione ARM64.
- [x] [P2][S6] **GET series segmentations**: `GET /api/series/:sid/segmentation-records` su `db.models.segmentations.Segmentation` (migration `0065_segmentations`). Restituisce signed NIfTI download URL (TTL 5min default, 15min max). I produttori (`totalsegmentator`/`manual`/`imported`) sono whitelisted; il primo richiede il TotalSegmentator job ancora aperto. Path è `/segmentation-records` perché `/series/:sid/segmentations` è già occupato dall'endpoint legacy in `api/segmentations.py` (lista S3 dei `.bin` blob, schema `{series_id, items}`); fino a quando le due superfici non saranno unificate sulla tabella `Segmentation`, i path restano distinti.
- [x] [P2][S6] **Measurements distance/volume**: `POST /api/series/:sid/measure/distance` + `/measure/volume` con coord pixel-space convertite in mm via `services/measurements.py` (PixelSpacing + SliceThickness/SpacingBetweenSlices). 422 `measurement_unavailable` con `missing_fields` quando i tag DICOM sono assenti.
- [x] [P2][S6] **GET SUV**: `GET /api/series/:sid/suv` riusa `services/suv.compute_suv_factors` (parallel session). 422 `suv_unavailable` con `missing_fields` su PET incompleti.
- [x] [P3][S6] **Cross-modal registration**: `POST /api/registrations` + `GET /api/registrations/:id` su `db.models.registrations.Registration` (migration `0066_registrations`). Worker `bvworkers.tasks.registration.register_series` implementa rigid via SimpleITK (Mattes MI + regular-step gradient descent) e demons via `FastSymmetricForcesDemonsRegistrationFilter` con rigid pre-init + Resample + CompositeTransform output. Test phantom in `workers/tests/test_registration_demons.py`.
- [x] [P3][S6] **MCP tools Phase 3 maturità**: 7 tool in `mcp/src/bvmcp/tools/imaging.py` — `crop_series_roi`, `measure_distance`, `measure_volume`, `get_suv`, `get_segmentations`, `register_series`, `get_registration`. (Tool count at sprint close was 51; subsequent sprints have grown the registry, see `mcp/src/bvmcp/tools/` for the live inventory.)

### Note di chiusura (2026-05-01)

Le sessioni autonome di Sprint 1..3.5 hanno chiuso ogni task chiaramente
specificabile da un agente senza accesso a fixture cliniche reali.
Sprint 5b (slice MPR coronale/sagittale + LRU cache + ROI crop) e
Sprint 6+ (TotalSegmentator + segmentations + measurements + SUV +
registration) restano aperti perché:

1. Richiedono integrazione SimpleITK / nibabel / TotalSegmentator
   con un setup numerico che va validato contro CT phantom synthetic
   (volume noto). Non è un esercizio pure-text; serve un harness con
   pipeline immagine.
2. ADR 0013 indica esplicitamente che TotalSegmentator su ARM64 è
   ancora uno **spike pendente**: confermare o rifiutare la wheel
   prebuilt prima di pianificare Sprint 6.
3. La cache slice LRU disk (10 GB cap) richiede una decisione
   operativa sulla persistenza del worker disk (volume PVC, retention,
   alert soglia 80%) — è un tema infrastrutturale che vive vicino al
   deploy K8s.

Le righe ``[ ]`` dello Sprint 5b/6+ rimangono come placeholder per
quando il setup imaging sarà disponibile.

## Aggiornamento 2026-05-01 (smoke test live)

* Smoke test end-to-end completato contro Postgres+Redis+MinIO live
  con phantom CT 64×64×16. Tutte le 14 migration applicate, MPR 3
  piani, measurements, dicom_meta allowlist, ROI crop, audit log
  popolato.
* `bvworkers.tasks.registration.register_series` ora supporta anche
  `kind="demons"` via `FastSymmetricForcesDemonsRegistrationFilter`
  con rigid pre-init + Resample + CompositeTransform output. Test
  phantom in `workers/tests/test_registration_demons.py` (3 verdi).
* Nuovo endpoint `GET /api/ai-assistants/scope-catalog` che ritorna
  il catalogo OAuth granulare (8 scope: 3 read, 4 write, 1 danger)
  con `category` + `description` + `dangerous`. Test drift in
  `backend/tests/test_scope_catalog.py` (5 verdi).
* Pagina Next.js `/settings/ai-assistants` aggiornata: i scope sono
  caricati dinamicamente dal catalog, raggruppati per categoria,
  con bordo colorato per `read`/`write`/`danger`. Toggle di uno
  scope `dangerous: true` apre un modal di conferma destructive.
* Bug fix: `dicom_meta_allowlist` non gestiva `pydicom.MultiValue`
  (PixelSpacing/Image*Patient scartati silenziosamente);
  `document_type_heuristic.DocumentType` Literal disallineato dopo
  Sprint 3 + SR/PR. Commit `f566238`.

## Embedded AI agents (system-wide) — non ancora disegnato

Distinzione architetturale chiarita 2026-05-02 dall'utente. Esistono **due
classi distinte** di "AI tool" in BitVision; oggi solo la prima è
implementata.

### Classe 1: BYO-AI personali — **implementati** (Sprint 6 + ADR 0019)

L'utente configura sul **suo** desktop / browser un client AI (Claude.ai
custom-connector, Anthropic CLI, MCP-compatible IDE, ecc.), paga il
provider AI di tasca propria, e si **connette a BitVision via MCP**.
L'autorizzazione è per-assistente: ogni assistente è un row
``agent_assistants`` con il suo ``client_secret`` (visibile una sola volta
in fase di creazione), il suo set di scope granulari, e l'allow-list dei
patient_id ai quali ha accesso. Lato BitVision questi tool sono interrogati
**dal client esterno**, non dal backend; BitVision non chiama mai un LLM
con queste credenziali.

UI: ``/settings/ai-assistants`` (la vista corretta). Backend:
``api/ai_assistants.py``, MCP server ``mcp/src/bvmcp/server_http.py``.

### Classe 2: Embedded AI agents (system-wide) — **NON IMPLEMENTATI**

Configurati dall'**admin di BitVision**, non dal singolo utente. Multipli
agenti possibili anche sullo stesso provider (es. due agent OpenAI con
prompt diversi, uno per triage radiologico e uno per discharge-letter
summarization). Le chiavi API stanno in admin-only secrets, **non** nelle
preferenze utente. BitVision **chiama** questi agenti per conto degli
utenti via:

- (a) **A2A (Agent-to-Agent)** — protocollo non ancora implementato.
- (b) Una UI dedicata dentro BitVision dove l'utente seleziona un agente
  embedded e lo interroga. Questa UI è da disegnare.

**Stato attuale (2026-05-02):** la pagina ``/settings/api-keys`` (BYOK
Anthropic) è un **vestigio** del modello unificato pre-ADR 0019; oggi è
fuorviante perché:

* è user-scoped (un utente, una key per provider) — il modello embedded
  vuole admin-scoped + multi-agente;
* il backend non ha ancora un loop "chiama l'AI col budget BYOK
  dell'utente" — quel ruolo è preso dal client esterno via MCP, dove la
  key è del client, non del backend.

Questa pagina deve essere o nascosta finché il modello embedded è
disegnato, oppure marcata chiaramente come preview / non-funzionale,
con un puntatore a ``/settings/ai-assistants`` per il caso BYO-AI
personale.

### Backlog Embedded (raccolta input chiarimento utente 2026-05-02)

- [ ] [P1] Spec ADR per il modello "embedded AI agents".
  Decisioni da prendere:
    * struttura ``embedded_ai_agents`` table (provider + model +
      system_prompt + admin-only API key reference + capability list);
    * separazione netta dalla pagina ``/settings/api-keys`` BYOK
      personale: gli embedded sono admin-only, non user-facing in
      Settings;
    * come l'utente seleziona un agente embedded da BitVision UI
      (modal? panel laterale? URL ``/agents/<slug>``?).
- [ ] [P1] Endpoint admin ``GET/POST/PATCH/DELETE /api/admin/ai-agents``
  per CRUD agenti embedded. Auth dep ``require_admin`` già esistente.
- [ ] [P1] Backend service ``services.embedded_agent.invoke(agent_id,
  conversation, …)`` che fa la chiamata al provider con la key admin
  + il prompt configurato.
- [ ] [P2] UI ``/admin/ai-agents`` per il CRUD: form con provider,
  model, system_prompt, API key (write-only, last4 echo), capability
  flags.
- [ ] [P2] UI utente per interrogare embedded: dialog "Chiedi a
  &lt;agent&gt;" sul fascicolo, sul singolo studio, sul documento;
  history conversazioni persistente per audit GDPR.
- [ ] [P1] **A2A** — protocollo agent-to-agent. Spec di partenza:
  vedi le proposte Google A2A; valutare anche MCP server-to-server.
  ADR dedicato prima di iniziare; il backend deve poter agire da
  *consumer* di A2A (chiamare agenti esterni per conto di un agente
  embedded) e da *producer* (esporre i propri tool MCP via A2A).
- [ ] [P2] Sistema di rate-limit + budget per gli embedded agents
  (per-agente e per-utente: l'utente non può consumare il budget
  admin se l'admin non l'ha autorizzato).
- [ ] [P3] Conversation memory per gli embedded agents (riusare
  ``conversation_id`` audit log + chiavi sticky per round successivi).
- [ ] [P3] Hide/redirect ``/settings/api-keys`` finché il modello
  embedded è disegnato — nel frattempo banner "preview: la
  configurazione AI per gli agenti personali è in
  ``/settings/ai-assistants``".

## Open questions (work in progress)

- **Cache OCR**: per file singolo o per documento intero (concatenazione)? Decidere prima Sprint 3.
- **Allowlist DICOM tag**: chi mantiene? Versionata in repo, review trimestrale? ADR 0011.
- **Migration `imaging_report → radiology_report`**: assistita o full-auto su pattern title? Decidere prima Sprint 3.
- **Agent rate limit per (scope, patient)**: parametri di partenza? Decidere prima Sprint 2.
- **MCP tool count**: aggregare via verbi se >30? Monitorare con eval set in Sprint 4.

## Rischi noti

1. **DAG versioning per documents** può aggiungere latency a PATCH frequenti (10-30 ms). Misurare in Sprint 1, accettare se p95 < 100 ms.
2. **OCR Tesseract italiano** ha precision ~85% su scansioni cliniche. Aspettative limitate, fallback a re-OCR forzato.
3. **MPR cache 10 GB** può saturare disk worker. Eviction LRU + alert soglia 80%.
4. **TotalSegmentator** non gira su ARM64 senza prebuilt wheel. Verificare compatibilità sul cluster di produzione ARM64 prima di Sprint 6.
5. **Tool count MCP** rischia di degradare selection accuracy. Monitorare con eval; aggregare via verbi se >30 tool.

## Riferimenti

- Spec: `docs/agents-api-spec.md` (draft 1, 2026-04-30)
- Plan iniziale: `~/.claude/plans/formalizza-un-piano-di-abstract-sun.md`
- Pattern esistenti riusati:
  - Versioning DAG: `backend/src/bvphoenix/services/versioning.py`
  - Jobs Arq: `backend/src/bvphoenix/services/jobs.py` + `workers/src/bvworkers/`
  - Cross-patient enforcement: `backend/src/bvphoenix/services/evidence_links.py`
  - Audit log: `backend/src/bvphoenix/db/models/audit.py`
  - Scope JWT: `backend/src/bvphoenix/auth/deps.py:259`
- Cheat docs:
  - Agent protocols overview: `docs/agent-protocols.md`
  - Architecture: `docs/architecture.md`
  - Authorization: `docs/authorization.md`
