# ADR 0014: Long-running operations su pattern jobs Arq

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 2 (convenzioni trasversali) tratta tutti gli endpoint
mutating come sincroni. L'analisi della spec ha identificato che alcune
operazioni sono **inerentemente long-running** e non si prestano a
chiamate sincrone:

- Bulk PATCH > 50 item su documents (Sprint 2).
- Re-OCR di un documento con 50 pagine scansionate (Sprint 3).
- Estrazione entità + indicizzazione (Sprint 4).
- Slice MPR generation per warm-up volume (Sprint 5/6).
- TotalSegmentator (Sprint 6, vedi ADR 0013).
- Cross-modal registration rigid + non-rigid (Sprint 6+).

Il codebase ha già pattern jobs maturo (`db/models/jobs.py`,
`services/jobs.py`, workers Arq) usato per:

- Bulk upload + ingest async con progress + cancel + cross-session
  recovery (commit 8ee374b).
- Cleanup notturno jobs scaduti.
- Async export GDPR.

## Decision

**Riuso del pattern jobs Arq esistente per tutte le long-ops, con
contratto API uniforme.**

Contratto:

1. Endpoint mutating con expected long duration ritorna **202 Accepted**
   invece di 200 quando il job è enqueued, body:
   ```json
   {
     "job_id": "uuid",
     "status_url": "/api/jobs/<job_id>",
     "estimated_duration_seconds": 120
   }
   ```
2. Endpoint generic `GET /api/jobs/:id` (esiste già) ritorna progress.
3. Soglia per il switch sync/async per-endpoint:
   - Bulk PATCH documents: > 50 item -> async.
   - Re-OCR: file > 5 MB OR pagine > 10 -> async.
   - Slice MPR generation: chiamata interactive sempre sync; warm-up
     in background job.
   - TotalSegmentator: sempre async (durata 5-15 minuti CPU).
   - Cross-modal registration: sempre async.
4. Cancel: `DELETE /api/jobs/:id` esiste, riusare.
5. Idempotency-Key (vedi ADR 0002) si applica all'enqueue: stessa key
   + stesso payload + status active del job esistente -> ritorna lo
   stesso `job_id` invece di duplicare.
6. Job result: stashato in `jobs.input["result"]` JSONB (pattern
   esistente). `GET /api/jobs/:id` espone il result quando
   `status=succeeded`.

Provenance:

- I job creati da agent token includono `agent_id`, `model_version`,
  `conversation_id` in `jobs.input["provenance"]`.
- Audit log scrive l'evento "job.enqueued" con stessa provenance.

## Consequences

### Positive

- Niente reinvenzione: progress, cancel, recovery, idempotency già
  funzionanti e testati in produzione.
- Contratto API uniforme per agent: stessa idea (POST -> 202 + status
  url) per tutti i long-ops, riduce confusion.
- Cap globale jobs attivi (esistente, default 20 per user) protegge da
  agent buggy che enqueue migliaia di task.
- Cross-session recovery: agent può controllare lo stato da una
  conversazione successiva (importante per workflow chat che girano
  ore).

### Negative

- Endpoint con response shape variabile (200 sync vs 202 async): client
  must handle both. Mitigato da soglie esplicite e documentazione.
- Logica di "decide se async" duplicata in più endpoint (potenziale
  refactor in helper future).
- Agent rate limit deve considerare jobs attivi, non solo HTTP rate.

### Mitigazioni

- Helper `maybe_async(threshold_fn, sync_fn, async_fn)` in `services/jobs.py`
  per ridurre duplicazione.
- Documentazione MCP: ogni tool che può tornare async lo dichiara nel
  description e nel response schema.

## Alternatives considered

- **Endpoint sempre sync con timeout server-side**: rifiutato.
  Operazioni di 5+ minuti rompono client e load balancer.
- **Endpoint sempre async**: forza il client a polling anche per
  operazioni di 2 secondi. UX scadente per agente.
- **WebSocket / SSE per progress**: appealing ma il pattern jobs
  esistente con polling è battle-tested. Aggiunta SSE come incrementale
  futuro (vedi ROADMAP open questions).
- **Nuovo job system parallelo**: rifiutato, divergerebbe dal pattern
  consolidato.

## Implementation hooks

- `services/jobs.py`: nessuna modifica al core.
- `services/jobs.py`: nuovo helper `enqueue_with_provenance(kind,
  input, scope_ids, agent_id, conversation_id)` per propagare
  provenance.
- `api/patients.py` Sprint 2: bulk PATCH >50 item enqueue jobs.
- `api/patients.py` Sprint 3: re-OCR enqueue jobs su file grandi.
- MCP tool descriptions devono indicare se il tool può tornare async
  (es. nello description del tool).
- Test:
  - Bulk PATCH 100 item -> 202 + job_id.
  - Bulk PATCH 5 item -> 200 + result inline.
  - Replay con stessa Idempotency-Key + job già attivo -> ritorna
    stesso `job_id`.

## Note operative

- Cap di jobs attivi per agent token: configurabile, default 5
  (più stretto di 20 per user, perché agent può scatenare in burst).
- Job result conservato finché `expires_at`, default 7 giorni. Dopo,
  cleanup cron rimuove (audit log resta).
