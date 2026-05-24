# ADR 0019: Remote MCP transport with per-assistant bearer secrets

**Status**: Accepted
**Date**: 2026-05-01
**Supersedes**: [ADR 0018](./0018-remote-mcp-oauth-authentik.md)
**Deciders**: Angelo Leto

## Context

ADR 0018 picked OAuth 2.1 Authorization Code (via Authentik) as the
auth gate for the remote MCP transport. After the integration with
Claude.ai's custom-connector dialog landed end-to-end on staging,
two practical problems emerged:

1. **Wrong abstraction for agents.** OAuth Authorization Code
   centres a human pressing "approve" in a browser. AI assistants
   have no human at the moment of re-auth: when the access token
   expires, the agent stops working until the user re-runs the
   login flow. We can lengthen the token lifetime, but a long-lived
   stateful JWT is the same risk profile as a static bearer with
   extra ceremony.
2. **Authentik is heavy infrastructure for a single use case.**
   The cluster ran an extra Postgres + Redis + server + worker
   StatefulSet pair (~768 MiB RAM, two PVCs, its own Ingress with
   path allow-list, blueprint ConfigMap, password reset playbook)
   solely to mint MCP tokens. phoenix's own user table + bcrypt +
   JWT path was already there and unused for the MCP flow.

What we actually want is a server-issued credential per AI
assistant, scoped to that assistant's permissions, revocable
independently, and shown to the operator exactly once at create
time.

## Decision

**Per-assistant machine-to-machine credentials. The MCP HTTP gate
accepts `Authorization: Bearer <client_secret>`, hashes it,
resolves it via an in-cluster RPC against the phoenix backend, and
builds a Principal from the result.**

### Architecture

```
[Claude.ai]
    | Authorization: Bearer <client_secret>
    v
[mcp.bitvision.example / mcp-http]
    | (1) sha256(client_secret)
    | (2) POST /api/internal/agent-bearer/resolve
    |     X-Internal-Key: <BVP_INTERNAL_API_KEY>
    v
[bitvision.example / phoenix-backend]
    | (3) lookup AgentAssistant.client_secret_hash
    | (4) return owner_subject_id, scope, patient_ids
    v
[mcp-http]  Principal built, request proceeds
    | (5) forwards Bearer <client_secret> on every tool call
    v
[phoenix-backend]
    | re-resolves the bearer hash; applies scope + patient enforcement
```

### Wire format

* `client_id` — `bvp_agt_<uuid_hex>`. VARCHAR(64), unique, indexed.
  Public, surfaces in the UI and in audit logs.
* `client_secret` — `secrets.token_urlsafe(48)`. Plaintext is shown
  to the operator exactly once (create or rotate); the server
  stores only `sha256_hex(secret)` in `client_secret_hash` and the
  first 8 chars in `client_secret_prefix` for UI identification.
* `Authorization: Bearer <client_secret>` arrives at MCP HTTP and
  is also forwarded verbatim to phoenix backend. Both ends do the
  same sha256 + DB lookup.

### Lifecycle

* **Create.** Operator clicks *New AI assistant* in Settings →
  AI assistants. Backend mints `client_id` + `client_secret`, persists
  the hash + prefix, returns the plaintext secret in the
  `AssistantCreatedOut` response. The frontend shows a reveal-once
  card with copy buttons. Closing the card discards the plaintext
  client-side; the secret is unrecoverable from then on.
* **Use.** The operator pastes the URL + client_id + client_secret
  into Claude (or any other MCP-capable client). Each MCP request
  carries the bearer; rate limiting, scope enforcement, patient
  filtering all key off the resolved `AgentAssistant` row.
* **Rotate.** A *Rotate secret* button regenerates
  `client_secret_hash` + prefix and surfaces a fresh
  `AssistantCreatedOut` to the operator. Old secrets stop working
  immediately (modulo the resolve cache TTL, default 60s).
* **Revoke.** Toggling `is_active = false` blocks future requests
  without touching the secret. Re-activating restores access.
* **Delete.** Drops the row and its patient grants.

### Caching

`mcp/src/bvmcp/auth.py` keeps a TTL cache keyed by `secret_hash`:

* Positive entries live `BVP_MCP_BEARER_CACHE_TTL_SECONDS`
  (default 60). Trade-off: longer = less RPC traffic; shorter =
  faster revocation propagation.
* Negative entries live
  `BVP_MCP_BEARER_CACHE_NEG_TTL_SECONDS` (default 10). Non-zero
  to throttle brute-force guess attempts; short to keep
  rotations responsive.

### Internal RPC

`POST /api/internal/agent-bearer/resolve` is mounted on the phoenix
backend and authenticated with a shared key (`X-Internal-Key:
<BVP_INTERNAL_API_KEY>`). The same key is read by mcp-http as
`BVP_MCP_BACKEND_INTERNAL_KEY`. The endpoint is reachable only on
the in-cluster ClusterIP Service; the public Ingress does not
route to it.

Misconfigured deploys fail closed: if `BVP_MCP_BACKEND_INTERNAL_KEY`
is empty, mcp-http refuses every inbound request with HTTP 503
("MCP auth misconfigured").

## Consequences

### Positive

* **Right shape for agents.** No browser, no token refresh, one
  long-lived secret per assistant. Revocation is per-assistant,
  not per-user.
* **Smaller blast radius.** A leaked secret affects exactly one
  assistant's patient subset; rotating is a button click.
* **No SSO infrastructure dependency.** The cluster runs phoenix +
  workers + frontend + mcp-http. External OIDC SSO is opt-in for
  human login but not load-bearing for the AI flow.
* **Reuses existing scope + patient enforcement.** The phoenix
  backend already keys agent capabilities off `AgentAssistant`; the
  bearer resolves to the same row.

### Negative

* **Reveal-once UX.** Operators who lose the plaintext secret must
  rotate. Mitigation: the create / rotate response is rendered in
  a high-contrast card with explicit "save now" copy and copy
  buttons; the prefix surfaces in the assistant list so operators
  can see *which* secret is which.
* **Cache TTL trades latency for revocation lag.** A revoked secret
  may keep working for up to `CACHE_TTL` seconds. For the default
  60s in clinical contexts this is acceptable; tune to 10–15s if
  not.
* **No standard token format.** Tooling that expects JWTs (e.g.
  decoding scopes client-side) cannot inspect the bearer. The
  phoenix UI exposes `client_id` + scope + patient list directly
  instead, which is the right shape for operators anyway.

### Migration

* `0067_connected_ai_oauth.py` (the OAuth-bound model) is left in
  place as historical record; it had not been load-bearing in
  production for any meaningful window before being replaced.
* `0068_assistant_credentials.py` drops `authentik_email` and adds
  `client_id` + `client_secret_hash` + `client_secret_prefix` with
  the indexes required for both lookup paths.
* In-cluster Authentik resources (PG + Redis StatefulSets, server
  + worker Deployments, Ingress, blueprint ConfigMap, secret) are
  removed via `./redeploy.sh cleanup-authentik`. The repo no longer
  ships any Authentik manifests.
