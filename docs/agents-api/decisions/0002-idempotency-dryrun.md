# ADR 0002: Idempotency-Key + dry_run interaction

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

La spec Agents API (sezione 2.2 e 2.3) introduce due primitive
ortogonali ma interagenti:

- `Idempotency-Key` header: client invia UUID, server cache la response
  per 24h, replay con stessa key + stesso body ritorna cache.
- `?dry_run=true` query param: stessa shape di mutating, ma niente
  effetto collaterale; ritorna `diff`.

Domanda aperta: una richiesta con `dry_run=true` e una con
`dry_run=false`, **stessa Idempotency-Key e stesso body**, sono la
stessa richiesta o no?

## Decision

**`dry_run` è parte del request hash usato per il match della
Idempotency-Key.** Una dry-run e una real con stessa key sono richieste
distinte. Il server le tratta come due cache entry separate.

Inoltre:

- Replay esatto di una dry-run (stessa key + stesso body + dry_run=true)
  ritorna cache, **non riesegue** il check di validazione. Se vogliamo
  forzare ricalcolo, l'agente cambia la `Idempotency-Key`.
- Hash del body: **JSON canonical form** (chiavi ordinate ricorsivamente,
  no whitespace, encoding UTF-8) prima dell'hash SHA-256. Niente
  byte-equality.
- I query param `dry_run` (default `false`) entrano nel hash come campo
  esplicito `__dry_run`.

## Consequences

### Positive

- Niente sorprese: replay identico ritorna cache identica.
- Permette il pattern "preview poi conferma" lato chat senza richiedere
  due key diverse.
- Agent può rigenerare il preview (`dry_run`) idempotentemente n volte
  prima dell'apply, senza far crescere lo store cache se la chiave è
  stabile.

### Negative

- 2x cache entry per ogni operazione che passa dal preview.
- Documentazione esplicita necessaria: bug comune sarà "ho fatto
  dry-run, poi ho fatto real con la stessa key, perché non ho visto la
  cache hit?". Risposta: dry_run partecipa all'hash.

## Alternatives considered

- **Ignorare `dry_run` nell'hash**: replay di dry-run dopo la real
  ritornerebbe la response real (già applicata). Confonde, danneggia il
  pattern preview.
- **`dry_run` esclude del tutto la cache**: le dry-run non vengono mai
  cached. Replay di dry-run è sempre un nuovo calcolo. Più semplice ma
  spreca risorse se l'agente fa preview multipli (caso tipico).

## Implementation hooks

- `middleware/idempotency.py` (Sprint 1):
  ```python
  def compute_request_hash(method, path, body_bytes, dry_run: bool) -> str:
      canonical = json.dumps(json.loads(body_bytes), sort_keys=True, separators=(",", ":"))
      return hashlib.sha256(
          f"{method}|{path}|{canonical}|dry={dry_run}".encode()
      ).hexdigest()
  ```
- Tabella `idempotency_records`: chiave `(idempotency_key, request_hash)`.
- Errore `idempotency_conflict` (422) se key esistente ma request_hash diverso.
