# ADR 0016: Token revocation

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

JWT scope-based auth (esistente in `auth/deps.py`) ha proprietà
"stateless": il server verifica firma + scadenza senza consultare DB.
Vantaggio: scaling. Svantaggio: se un token viene compromesso (agent
buggy, leak in log, dispositivo rubato), non c'è meccanismo di revoca
fino alla scadenza naturale.

Per token agent in particolare:

- Spec specifica scope ristretti per-paziente.
- Token possono essere short-lived (default 1h) ma per workflow lunghi
  si usano refresh.
- Compromissione di un agent token espone il fascicolo del paziente
  associato.

L'analisi della spec ha rilevato la mancanza di un meccanismo di
revoca veloce.

## Decision

**Tabella `revoked_tokens` con check obbligatorio in `require_user`,
indice ottimizzato per low-latency lookup.**

Schema:

```sql
CREATE TABLE revoked_tokens (
    jti UUID PRIMARY KEY,                  -- JWT ID claim
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,       -- == JWT exp claim
    revoked_by_subject_id UUID NOT NULL,
    reason TEXT NULL
);

CREATE INDEX ix_revoked_tokens_expires_at ON revoked_tokens (expires_at);
```

Implementazione:

1. **JWT issuance**: ogni token (user e agent) include claim `jti`
   (JWT ID, UUID v4 random) e `exp`.
2. **Verification path**: `require_user` (e dependencies derivate)
   estraggono `jti`, controllano se presente in `revoked_tokens`. Se
   sì, 401 con `error: token_revoked`.
3. **Revocation API**: `POST /api/auth/revoke-token`,
   richiede scope `auth:admin` (umani con permission)
   o `auth:self_revoke` (subject revoca i propri token).
   Body: `{"jti": "...", "reason": "..."}`.
4. **Bulk revocation**: `POST /api/auth/revoke-all-for-subject` per
   incident response (compromissione subject).
5. **Cleanup**: cron daily rimuove righe con `expires_at < NOW()` (il
   token è scaduto comunque, niente bisogno di tenere il record).
6. **Caching server-side**: lookup `jti -> revoked` cached in Redis
   (TTL 60s). Latency revoca: ~60s worst-case dopo POST revoke.

## Consequences

### Positive

- Revoca pratica: ~60s di latency tipica, immediata in uno stesso
  worker (no caching tra richieste sequenziali).
- Compatible con JWT stateless: il check è O(1) cache hit, fallback
  Redis O(1), DB query solo su miss.
- Scope basso: solo i token revocati sono in tabella, non tutti i
  token attivi.

### Negative

- Aggiunta verifica DB/cache per ogni richiesta autenticata. Mitigato
  da Redis caching aggressivo.
- Cache 60s significa "fino a 60s di esposizione post-revoke". Per
  incident response gravi, va bypass (vedi mitigazioni).
- Gestione cache invalidation: dopo POST revoke, invalidare tutte le
  cache distribuite. Pattern Redis pubsub.

### Mitigazioni

- "Hard revoke" endpoint admin che invalida cache Redis pubsub
  immediatamente -> latency < 1s globale.
- Audit log di ogni revoke con reason e revoked_by.

## Alternatives considered

- **Token short-lived (5 min) + refresh**: niente revoca esplicita,
  scadenza naturale veloce. Ma molti workflow non funzionano con TTL
  così corto. Refresh token diventa la nuova superficie d'attacco.
- **Allowlist invece di denylist**: ogni token attivo in tabella,
  lookup verifica presenza. Tabella enorme. Rifiutato.
- **OAuth introspection endpoint**: standard ma richiede call
  out-of-band, latency peggiore.
- **Revoca implicita via cambio password / logout**: incrementa
  contatore generation di subject, JWT con generation < current sono
  invalidi. Più sofisticato, valutare per Sprint 5+.

## Implementation hooks

- `db/models/revoked_tokens.py` (Sprint 1): modello + migration.
- `auth/tokens.py`: aggiunta claim `jti` in issuance.
- `auth/deps.py`: estensione `require_user` con check.
- `services/redis_cache.py`: cache layer.
- `api/auth.py`: endpoint POST revoke-token, revoke-all-for-subject.
- `workers/src/bvworkers/tasks/cleanup_revoked_tokens.py`: cron daily.
- Test:
  - Token attivo -> 200.
  - Token revoked -> 401.
  - Revoca invalidate cache pubsub -> token revoked < 1s.
  - Cleanup rimuove righe expired.

## Note compliance

- Audit log della revoke conserva `subject_id`, `reason`, `revoked_by`.
- Per token utente (non agent), revoca è soggetta a "right to remove
  session" GDPR. Procedura e form documentati separatamente.
