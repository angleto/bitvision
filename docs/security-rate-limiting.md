# Rate limiting and brute-force protection

Scope: two concentric layers.

1. **Credential / share endpoints** — tight limits so an attacker cannot
   brute-force their way into a tenant.
2. **Expensive write / inference endpoints** — looser limits, sized for
   a human using the product at high speed but tight enough that a
   runaway client or stolen token cannot flood ingestion or burn a
   BYOK/platform LLM budget.

| Endpoint                                            | Limit (per IP) | Rationale                                               |
| --------------------------------------------------- | -------------- | ------------------------------------------------------- |
| `POST /api/auth/login`                              | 5 / minute     | Interactive retry budget for humans.                    |
| `POST /api/auth/register`                           | 3 / minute     | Throttles signup-flood abuse.                           |
| `POST /api/shared/{t}/verify`                       | 10 / minute    | Shared-link password guessing.                          |
| `POST /api/dicom/studies`                           | 10 / minute    | Drag-drop upload. A human retrying a stuck batch still fits. |
| `POST /api/dicom/stow-rs`                           | 30 / minute    | DICOMweb STOW from clinical workstations (machine).     |
| `POST /api/upload/bulk`                             | 3 / minute     | Whole-folder / DICOMDIR ingestion is heavy per call.    |
| `GET /api/search`, `/api/search/hybrid`, `/api/similar-to/{id}` | 60 / minute | Tolerates lively UI exploration; blocks enumeration abuse. |
| `GET /api/search/semantic`                          | 30 / minute    | Each miss runs a CLIP / MiniLM forward pass.            |
| `POST /api/series/{id}/llm/describe`, `/api/llm/stream`, `/api/summaries/generate` | 20 / minute | Every call can reach a paid upstream. Defence in depth on top of BYOK / credit gating. |
| `POST /api/a2a`                                     | 30 / minute    | JSON-RPC to the agent surface, including anonymous card lookups. |

Exceeding the per-IP limit returns HTTP 429 with a JSON body of the form
`{"detail": "rate limit exceeded: 5 per 1 minute"}`.

The LLM limit is defence in depth on top of the F7 credit gateway: the
ledger already caps spend by balance, but a 429 here contains the blast
radius of a loop-bug client *before* the user runs out of credits, and
gives BYOK callers the same safety without relying on their provider's
quota enforcement.

## Implementation

The limiter lives in [`backend/src/bvphoenix/services/rate_limit.py`](../backend/src/bvphoenix/services/rate_limit.py)
and is built on [`slowapi`](https://github.com/laurentS/slowapi). It is
attached to the FastAPI app at startup (`main.py` wires
`app.state.limiter` and registers the `RateLimitExceeded` handler).

State is persisted in Redis (via `BVP_REDIS_URL`) so counters are shared
across uvicorn workers and across container restarts. When Redis is
unreachable the limiter falls back to an in-process memory store, so
tests and dev environments do not require a live Redis.

Route handlers opt in with `@limiter.limit("5/minute")` and must accept
a `request: Request` parameter — slowapi reads the client address from
that argument. The `SlowAPIMiddleware` is added to the app so decorated
routes are actually evaluated.

When a route is called from an in-process consumer (an A2A skill
dispatching to `similar_to`, an MCP tool reusing a helper), the
request handler is factored into a plain async function (e.g.
`find_similar_studies`) that the decorated HTTP endpoint wraps. The
internal caller invokes the plain helper, so it does not pay the
IP-based budget twice and does not need to forge a `Request`.

### Trust model behind a proxy

`get_remote_address` uses `request.client.host`. When the API sits
behind a reverse proxy set `--proxy-headers` on uvicorn (and forward
`X-Forwarded-For`) so the client address is the real remote IP rather
than the proxy. Without this, every request appears to come from the
proxy's IP and the limiter degrades to a global cap.

## Progressive account lockout

Per-IP limits stop an attacker from hammering one origin but do not
help against a distributed credential-stuffing attempt against a single
account. We therefore also maintain a per-email failure counter.

* Key: `login:lockout:{sha256(email)}` — email is hashed so we never
  write raw addresses into Redis.
* After `LOGIN_LOCKOUT_THRESHOLD = 5` consecutive failures the account
  is locked for `LOGIN_LOCKOUT_SECONDS = 15 * 60` (15 minutes).
* A successful login clears the counter immediately.
* While locked out, login returns HTTP 423 `Locked` with
  `{"detail": "account temporarily locked due to repeated failed logins"}`.
  The DB is not touched on a locked request.

The counter itself has a 15-minute sliding TTL. If the attacker pauses
long enough for the counter to expire, failures restart from zero —
this is a standard trade-off that prevents stale counters from
permanently shaping a user's login experience.

## Operational notes

* To adjust thresholds edit the module-level constants in
  `services/rate_limit.py`; they are intentionally kept together so
  tuning is one edit.
* To observe lockouts in production, watch for
  `status_code=423` on `/api/auth/login` or scan Redis for
  `KEYS 'login:lockout:*'` (prefer `SCAN` in production).
* The limiter is configured with no default limit — only decorated
  routes are rate-limited. This keeps the blast radius of a
  misconfiguration bounded to the three endpoints above.
