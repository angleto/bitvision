# ADR 0015: OpenAPI snapshot first-class

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

La spec Agents API è grande (10+ nuovi endpoint, 17+ MCP tool, contract
con header speciali e errori strutturati). FastAPI genera OpenAPI 3.x
nativamente da Pydantic models. Però:

- Nessun snapshot committato: ogni cambio endpoint richiede review
  manuale di "ho rotto qualcosa di pubblico?".
- Nessun controllo che la documentazione MCP-side resti allineata al
  backend.
- Nessun deliverable formale per gli sviluppatori esterni che vogliono
  integrare via REST.

L'analisi della spec lo segnala come gap di "schema-first" claim.

## Decision

**OpenAPI snapshot committato in repo + check CI di drift.**

Implementazione:

1. **Generazione**: script `scripts/dump_openapi.py` esegue
   `app.openapi()` di FastAPI e scrive l'output in
   `backend/openapi.json` (formato JSON 2-space indent, `sort_keys=True`
   per deterministic diff).
2. **Check CI**: workflow GitHub Actions `openapi-check.yml` rigenera
   lo snapshot in CI e fallisce se diverso da quello committato.
   Messaggio errore include diff e suggerisce
   `python scripts/dump_openapi.py` localmente.
3. **Pre-commit hook (opzionale)**: l'utente può aggiungere
   `pre-commit-config.yaml` hook che esegue il dump prima del commit.
4. **Convenzione**: lo snapshot DEVE essere aggiornato nello stesso
   PR che modifica gli endpoint (no PR separati per `openapi.json`).
   Se PR cambia un endpoint senza aggiornare lo snapshot, CI rifiuta.
5. **Versioning API**: l'OpenAPI ha campo `info.version = bvphoenix.__version__`.
   Le breaking changes sono tracciate da:
   - Bump del minor version (v1.x -> v1.x+1).
   - Sezione changelog in `docs/agents-api/CHANGELOG.md` (Sprint 2+).
6. **MCP tool sync (Sprint 4 stretch)**: tool MCP definiscono il loro
   inputSchema; ci sarà un check secondario che il tool MCP è
   coerente con il backend endpoint corrispondente (manuale per
   ora, automatizzabile in futuro).

## Consequences

### Positive

- Diff-friendly: ogni cambio API è visibile nei PR.
- Documentazione self-updating: `backend/openapi.json` è sempre la
  realtà del momento.
- Onboarding: developer esterni possono generare client (OpenAPI
  Generator) dalla snapshot direttamente.
- Reduced bugs: drift tra docs e reality eliminato.

### Negative

- Snapshot grande (~200-500 KB JSON), rumore in `git log`.
- Conflitti merge frequenti su PR concorrenti che toccano endpoint.
  Mitigato da risoluzione "rigenera dopo merge".
- Setup CI iniziale (~30 min).

### Mitigazioni

- Conflict resolution: mai risolvere a mano lo snapshot. Sempre
  rigenerare con lo script.
- Snapshot in `.gitattributes` come `merge=ours` con autoresolve, poi
  rigenerazione obbligatoria post-merge in CI.

## Alternatives considered

- **Generazione runtime, no snapshot**: stato attuale. Niente diff
  visibility, drift risk.
- **Snapshot YAML invece di JSON**: più human-readable ma diff peggio
  in tool standard. JSON ordinato è sufficiente.
- **Tool tipo `pact-broker` per consumer-driven contracts**:
  iperingegnerizzato per ora, valutare in futuro.
- **Snapshot per-versione con dir `openapi/v1.json, v2.json, ...`**:
  utile quando ci sono breaking changes versionate. Per ora una
  singola snapshot è OK.

## Implementation hooks

- `scripts/dump_openapi.py` (Sprint 1):
  ```python
  from bvphoenix.main import app
  import json
  from pathlib import Path
  schema = app.openapi()
  Path("backend/openapi.json").write_text(
      json.dumps(schema, indent=2, sort_keys=True) + "\n"
  )
  ```
- `.github/workflows/openapi-check.yml`: workflow CI.
- `Makefile`: target `make openapi.dump` e `make openapi.check`.
- Documentazione in `CONTRIBUTING.md`: aggiungere sezione "API changes".
- Test:
  - Endpoint nuovo aggiunto -> snapshot diverso -> CI fallisce se
    snapshot non rigenerato.
  - Pydantic model con campo nuovo -> snapshot diverso -> CI fallisce.

## Note future

- Quando il numero di endpoint pubblici cresce, valutare segregazione:
  un OpenAPI per "Agents API" (`/api/patients/*`, `/api/series/*`,
  `/api/consultations/*`) e uno per "internal admin"
  (`/api/admin/*`). Tagging via FastAPI tags. Sprint 5+.
