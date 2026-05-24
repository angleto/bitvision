# ADR 0003: Bulk replay semantics (atomic=false)

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

L'endpoint `PATCH /api/patients/:pid/documents/bulk` (spec sezione 3.2)
accetta `atomic: true|false`:

- `atomic=true`: tutto-o-niente. Una failure → rollback completo.
- `atomic=false` (default): best-effort. Failure su un item non blocca
  gli altri. Response include `results[]` con stato per item e summary
  `{ok, error}`.

Domanda: come si comporta il **replay** con `Idempotency-Key`
(ADR 0002) quando `atomic=false` e alcuni item sono falliti?

Caso concreto: 100 patches, 90 ok, 10 falliti per `etag_mismatch`.
L'agente vuole ritentare i 10. Se manda lo stesso payload con stessa
Idempotency-Key, cosa succede?

## Decision

**Il replay restituisce la response originale per ogni item, anche per i
falliti. Gli item falliti vanno ritentati con nuova `Idempotency-Key`
e ETag aggiornati.**

Convention applicativa per gli agenti:

1. Eseguire bulk con key K1 → 90 ok, 10 fail.
2. **Non** rinviare la stessa request con K1 sperando di riprocessare i
   10. Replay con K1 ritorna la cache (90 ok + 10 fail come prima).
3. Costruire una nuova bulk request **solo con i 10 item**, ognuno con
   ETag fresco (preso dalla GET corrente del document), e nuova
   Idempotency-Key K2.

Documentato in:

- Docstring di `bulk_update_documents` (REST) e MCP tool corrispondente.
- Test acceptance dedicato in Sprint 2.
- Esempio di flusso negli ADR e nella documentazione MCP.

## Consequences

### Positive

- Garanzia idempotente forte: stessa key + stesso body = stessa
  response, sempre.
- Evita ambiguità "ha riprocessato i fallimenti automaticamente?" (no).
- Permette al client di decidere la strategia di retry (immediate,
  exponential backoff, manual review prima del retry).

### Negative

- Confusion potenziale per chi pensa che "stessa key = retry intelligente".
  Mitigato da docstring esplicita e error message del replay (es.
  header `X-Idempotency-Replay: true`).
- Più ronde di chiamate per smaltire bulk con molti fallimenti misti.

## Alternatives considered

- **Replay riprocessa solo i falliti**: utile ma rompe l'invariante
  "stessa request = stessa response" e introduce magic non
  ispezionabile. La cache diventa una coda di retry non documentata.
- **Server genera nuova key per i falliti automaticamente**:
  iperingegnerizzato, viola REST minimalism, l'header rimane fonte di
  verità solo se l'agent può controllare la chiave.
- **`atomic=false` non supporta replay**: semplifica ma forza
  agent a cambiare key sempre (perdendo il safety net del replay
  parzialmente, anche per le 90 patches già ok).

## Implementation hooks

- Docstring esplicita su endpoint REST e MCP tool.
- Test scenario in `tests/test_documents_bulk.py`:
  ```
  test_bulk_atomic_false_replay_returns_original_response
  test_bulk_atomic_false_partial_fail_retry_with_new_key_succeeds
  test_bulk_atomic_false_replay_with_modified_body_returns_409
  ```
- Header response `X-Idempotency-Replay: true` per chiarezza in debug.
