# ADR 0018: Remote MCP transport + OAuth 2.1 via Authentik

**Status**: Superseded by [ADR 0019](./0019-remote-mcp-per-assistant-bearer.md)
**Date**: 2026-04-30
**Superseded**: 2026-05-01
**Deciders**: Angelo Leto

> **Why superseded.** OAuth Authorization Code requires a browser
> login flow per token mint, which is the wrong shape for AI agent
> connections (the agent has no human at the keyboard for re-login,
> and per-user tokens conflate identity with capability). The
> follow-up architecture mints per-assistant `client_id`/`client_secret`
> pairs server-side and resolves the bearer hash via an internal
> RPC. See ADR 0019 for the rationale and the as-built design.

## Context

Il connettore MCP attuale (`mcp/src/bvmcp/server.py`) gira in stdio
come processo locale. Onboarding richiede al utente:

1. Installare Python + uv + il package `bvmcp`.
2. Configurare token via env (`BVP_MCP_USER_TOKEN`, `BVP_MCP_AGENT_TOKEN`).
3. Editare la config di Claude Desktop (o IDE) per registrare lo
   stdio command.

Anthropic ha rilasciato in beta i "custom connectors" su Claude.ai
(web UI), che accettano un **remote MCP server URL** + OAuth Client ID/Secret
opzionali. Questo permette onboarding zero-install: l'utente apre
claude.ai, incolla l'URL, fa login OAuth, e i tool MCP sono disponibili
in qualsiasi conversazione.

Per phoenix questo è strategicamente rilevante: trasforma il connettore
da feature per power user (Claude Desktop locale) a feature per
qualsiasi clinico con browser. L'aderenza al modello "agent on the
cloud" è la direzione di Anthropic e dell'ecosistema MCP in generale.

Lo stack già contiene Authentik (`infra/docker-compose.yml` profile
`auth`, v2025.2; `make deploy.apply svc=authentik` per K8s) come
identity provider. Authentik supporta OAuth 2.1 + OIDC con discovery,
PKCE, e provider configuration via blueprint YAML.

## Decision

**HTTP Streamable transport per il server MCP, OAuth 2.1 via Authentik
come authorization server, deploy nel cluster Kubernetes sotto
`mcp.bitvision.example`.**

### Architettura

```
[Claude.ai]
    | (1) GET /.well-known/oauth-authorization-server
    | (2) Authorization Code + PKCE flow
    v
[Authentik OAuth Provider]
    | (3) issues JWT (audience=mcp, scope=...)
    v
[Claude.ai]
    | (4) MCP requests con Bearer JWT
    v
[mcp.bitvision.example / Traefik]
    v
[MCP HTTP server (bvmcp)]
    | (5) propaga JWT come Bearer al backend
    v
[backend.bitvision.example / Phoenix REST API]
```

### Componenti

1. **MCP HTTP transport**: nuovo entry point `mcp/src/bvmcp/server_http.py`
   usando il transport HTTP del MCP SDK Python (Streamable HTTP).
   Riusa i tool registry esistenti (`_TOOL_MODULES`); cambia solo il
   transport layer.

2. **Authentik OAuth Provider**: configurato come blueprint YAML
   versionato in `infra/authentik/blueprints/mcp-provider.yaml`. Crea:
   - OAuth2 Provider "bitvision-mcp" con grant types
     `authorization_code` + `refresh_token`, PKCE required.
   - Application "Bitvision MCP" con redirect URI
     `https://claude.ai/api/mcp/auth_callback` (e localhost per
     dev).
   - Custom scope mappings: `documents:read`, `documents:write_metadata`,
     `consultations:write`, ecc. (catalogo allineato con
     `auth/scopes.py` del backend).

3. **Discovery endpoint**: `https://mcp.bitvision.example/.well-known/oauth-authorization-server`
   ritorna JSON con `authorization_endpoint`,`token_endpoint`,
   `issuer`, `scopes_supported` puntando ad Authentik.

4. **JWT propagation**: il MCP HTTP server estrae Bearer token
   dall'header `Authorization` della richiesta MCP, lo valida con la
   JWKS di Authentik, e lo passa come Bearer al backend phoenix nel
   tool dispatch (riuso di `mcp/src/bvmcp/tools/client.py`).

5. **Token mapping MCP -> backend**: il JWT Authentik ha il proprio
   formato; il backend phoenix ha il proprio JWT format. Tre opzioni:
   - **A. Same JWT, dual issuer trust**: backend valida sia JWT
     phoenix che JWT Authentik (più issuer trusted in `auth/tokens.py`).
     Più semplice, niente exchange.
   - **B. Token exchange**: MCP HTTP scambia JWT Authentik per JWT
     phoenix via endpoint dedicato. Più segreti da gestire.
   - **C. JWT phoenix issued da Authentik**: Authentik diventa l'unico
     issuer, il backend phoenix smette di emettere JWT propri.
     Migrazione invasiva.

   **Decisione: opzione A** per Sprint 1.5. C valutabile in roadmap
   come consolidamento futuro.

### Deploy

- Container `bvmcp-http` (immagine derivata dall'attuale `mcp/`),
  exposed su porta 8080, manifest K8s in `infra/k8s/mcp-http.yaml`.
- Service `mcp-http` + Traefik IngressRoute con TLS Let's Encrypt
  per `mcp.bitvision.example`.
- Authentik già accessibile su `auth.bitvision.example` (assumed
  dal layout delle ingress).
- Metric Prometheus: `mcp_http_requests_total{tool, status}`.
- Rate limit: slowapi-based, default 50 req/s per token, 200 req/s
  per IP.

### OAuth scopes catalog

I scope OAuth disponibili tramite Authentik mappano 1:1 quelli del
backend phoenix:

- `mcp:profile`: lettura profilo subject.
- `documents:read`, `documents:read_content`, `documents:write_metadata`,
  `documents:delete`.
- `studies:read`, `studies:read_pixels`.
- `annotations:write`.
- `consultations:write`, `consultations:finalize` (quest'ultimo
  rifiutato per agent token, vedi ADR 0010).
- `patient:full` wildcard per debug/admin.

Default scope al consent screen Authentik: `mcp:profile documents:read
studies:read consultations:write`. L'utente può estendere su richiesta.

## Consequences

### Positive

- Onboarding 30 secondi dal browser, niente install.
- Multi-device: stesso connector funziona da laptop a tablet.
- OAuth 2.1 con PKCE riduce rischio token leak vs bearer statico
  in clipboard.
- Authentik sa già gestire MFA, password policies, session management:
  mai reinventato.
- Discovery + JWKS rotation: gestito da Authentik, niente
  manutenzione custom.
- Pattern "agent on cloud" abilita futuri client (mobile, agenti A2A,
  altri MCP host).

### Negative

- Endpoint pubblico `mcp.bitvision.example` aumenta surface attack.
  Mitigato da rate limit + audit log + scope ristretti.
- PHI in transit: HTTPS obbligatorio, ma anche log gateway, metriche
  Prometheus, eventuali bug di routing diventano vettori. Audit
  compliance separato.
- Dipendenza forte da Authentik: se va giù, MCP è inaccessibile.
  Mitigato da SLA cluster e fallback "claude desktop stdio" sempre
  disponibile come backup.
- Operations: certificate renewal, version bump Authentik, migration
  schema se Authentik major release rompe.
- L'opzione A (dual issuer trust) lascia due path di authn nel backend.
  Rischio di drift tra le due. Mitigato da test contract.

### Mitigazioni

- Rate limit aggressivo per scope `*:write*` (5-20 req/s).
- Audit log dedicato `mcp_http_audit` con `actor_subject_id, tool,
  patient_id, ip, status`.
- Healthcheck Authentik in `/health/auth` del MCP HTTP server: se
  JWKS unreachable, ritorna 503 invece di 5xx ambiguo.
- Token revocation (vedi ADR 0016) si applica anche ai token
  Authentik tramite `/api/admin/oauth2/tokens/<id>/` di Authentik.

## Alternatives considered

- **Bearer-only senza OAuth (campi optional di Claude.ai)**: quick
  start ma il bearer statico in clipboard / screenshot è fragile per
  contesto clinico. Per dati PHI, OAuth con PKCE è il minimo
  accettabile.

- **OAuth provider esterno (Auth0, Clerk, AWS Cognito)**: costo
  ricorrente + dipendenza vendor + duplicazione di Authentik già
  in stack. Rifiutato.

- **Phoenix backend come OAuth issuer (no Authentik)**: feasible ma
  reinvento un OAuth server. Authentik già fa tutto questo bene.

- **gRPC + custom client (no HTTP MCP)**: Claude.ai non lo supporta.

- **Posporre a Sprint 7+ (post-MVP)**: scelta originale del piano.
  Sostituita da Sprint 1.5 perché lo strategic value (UX onboarding)
  è alto e le dipendenze tecniche sono indipendenti dal foundation
  cross-cutting.

## Implementation hooks

- `mcp/src/bvmcp/server_http.py` (Sprint 1.5): entry HTTP, MCP SDK
  Streamable transport. Riuso `_TOOL_MODULES`.
- `mcp/src/bvmcp/auth.py` (Sprint 1.5): JWKS fetch, JWT validation,
  scope check.
- `mcp/src/bvmcp/discovery.py` (Sprint 1.5): handler per
  `/.well-known/oauth-authorization-server`.
- `infra/authentik/blueprints/mcp-provider.yaml`: blueprint YAML
  per Authentik, applicato via API o Authentik admin.
- `infra/k8s/mcp-http.yaml`: Deployment + Service + IngressRoute.
- `backend/src/bvphoenix/auth/tokens.py`: trust dell'issuer Authentik
  oltre all'issuer phoenix esistente (opzione A).
- `mcp/Dockerfile.http`: Dockerfile dedicato (può essere stesso del
  stdio con CMD diverso).
- Test:
  - Local: `mcp/tests/test_http_transport.py`, mock Authentik JWKS.
  - Staging: smoke test con Claude.ai sandbox.
  - Production: canary deploy.

## Note operative

- Versionamento del blueprint Authentik: stessa convention degli ADR
  (committato in repo, review obbligatoria su PR).
- Roll-out: Sprint 1.5 deploya il connector come "beta", invitato
  privato. General availability dopo Sprint 2 acceptance test.
- Documentazione utente: pagina dedicata `docs/agents-api/onboarding-mcp.md`
  (Sprint 1.5) con screenshot del flusso "Add custom connector" su
  Claude.ai.
- Token revocation: l'admin può revocare i token MCP da Authentik UI
  senza necessità di toccare phoenix.

## Riferimenti

- MCP transport spec (Streamable HTTP):
  https://modelcontextprotocol.io/specification/basic/transports#streamable-http
- Anthropic custom connectors:
  https://support.anthropic.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp
- Authentik OAuth2 provider:
  https://docs.goauthentik.io/docs/providers/oauth2/
- OAuth 2.1 (draft):
  https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/
- RFC 8414 (Authorization Server Metadata):
  https://datatracker.ietf.org/doc/html/rfc8414
