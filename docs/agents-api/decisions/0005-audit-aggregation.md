# ADR 0005: Audit aggregation strategy (read vs write)

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 2.7 richiede che l'audit log includa **lette E scritte**:
"Per dati sanitari familiari è precondizione di compliance".

L'analisi ha identificato un problema di volume: con rate limit read di
100/s per token, N agenti concorrenti, M pazienti, il volume di INSERT
sull'audit cresce velocemente. Una singola sessione di triage di un
paziente può generare 50-200 GET (timeline, documents, fascicolo
bundle, slice DICOM, ecc.). Insert di pari numero su tabella append-only
indicizzata diventa un costo non trascurabile in DB.

## Decision

**Strategia a due livelli**:

1. **Read events**: aggregati in tabella `audit_session_view`. Schema:
   ```
   id (UUID)
   actor_subject_id
   patient_id (nullable: read globali ammessi)
   agent_id (nullable: human session)
   conversation_id (nullable)
   first_event_at
   last_event_at
   read_count
   resource_kinds_touched (JSONB array di stringhe)
   ip_address
   user_agent
   ```
   Una "session view" copre N minuti di lettura (default 15) sullo
   stesso paziente da parte dello stesso subject. Update incrementale
   su `last_event_at` e contatore. Niente per-resource granularity:
   l'invariante è "questo subject ha guardato fascicolo X tra T0 e T1".

2. **Write events**: insert immediato in `audit_log` (esistente, vedi
   `db/models/audit.py`). Granularità per-action come oggi: ogni PATCH,
   POST, DELETE è un record. Diff before/after persistito in metadata
   JSONB.

## Consequences

### Positive

- Volume read INSERT ridotto di 1-2 ordini di grandezza rispetto a
  per-GET log.
- Compliance preservata: la session view dimostra chi ha avuto accesso
  a quale paziente quando, sufficiente per audit interno e esterno.
- Write granular su tutte le mutating, niente compromesso.
- Aggregazione esistente facilita reporting: "tutti gli accessi al
  fascicolo del paziente X negli ultimi 30 giorni" è una query semplice.

### Negative

- Granularità persa sui read: non sappiamo *quale specifico documento*
  l'agent ha letto, solo che ha letto qualcosa del fascicolo. Trade-off
  accettabile per il volume risparmiato.
- Window di 15 min richiede tuning in base al pattern reale. Possibile
  parametro per-tenant in futuro.
- Edge case: un singolo GET seguito da 14 min di idle e poi altri GET
  vengono raggruppati. È desiderato? Sì, è la definizione di "session view".

## Mitigazioni e estensioni future

- Conservare gli IP/user-agent: se cambiano in mid-session, splittare la
  session view in due record (sentinel di security).
- Per i casi P0 di audit (es. lettura di documento marcato come
  riservato), endpoint specifici emettono record `audit_log` granulare
  invece di passare dalla session view. Decidere lista esatta in
  Sprint 3.
- Hot/cold tier: `audit_log` in hot per 90 giorni, archive S3 Glacier
  per 10 anni (vedi roadmap).

## Alternatives considered

- **Read log granulare per-GET**: massima fedeltà, costo I/O proibitivo
  a scala.
- **Read log solo aggregato per giornata**: minimal storage, perde la
  granularità di "che ora ha guardato il fascicolo".
- **Sampling**: log solo 1 GET su N. Rifiutato per implicazioni
  compliance, sample non garantisce di catturare gli accessi sospetti.
- **Audit log in tabella separata su Postgres con TTL aggressive**:
  preserva granularità ma forza retention < 30 giorni, conflitta con
  retention 10 anni richiesta.

## Implementation hooks

- `db/models/audit_session_view.py` (Sprint 1).
- `middleware/audit_dependency.py`: estendere il dependency esistente
  per attivare la session view aggregation sui GET.
- Decorator `@audit_write(action, resource_kind)` per mutating, scrive
  diretto su `audit_log`.
- Endpoint `GET /api/patients/:pid/audit?since=...` (esiste, da
  estendere) ritorna unione di `audit_log` + `audit_session_view`
  ordinata per timestamp, distinguendo `kind: write|session_view`.

## Note compliance

- Conservazione 10 anni vale anche per `audit_session_view`.
- PHI in user_agent / IP: minimizzare. Hash IP per record > 90 giorni se
  policy lo richiede (decidere con privacy officer).
