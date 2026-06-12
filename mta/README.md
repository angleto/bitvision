# bvmta — inbound-email MTA adapter

A deliberately dumb SMTP→HTTP adapter (task fbbf5270 §5): it terminates
port 25 for `inbox.<domain>`, validates every `RCPT TO` against the
backend (`POST /api/internal/inbound-email/validate-rcpt`) and forwards
each accepted message verbatim (`POST /api/internal/inbound-email`).

Security posture:

* **no S3 / DB credentials** — storage isolation: all persistence is on
  the backend side of the internal RPC;
* authenticates to the backend with the dedicated
  `BVP_INBOUND_INTERNAL_SECRET` (header `X-Inbound-Key`), distinct from
  the mcp-resolve internal key on least-privilege grounds;
* backend unreachable / 5xx ⇒ SMTP **451** (the sender retries; mail is
  never dropped silently);
* unknown / revoked capability code ⇒ **550** at RCPT time;
* `SIZE` advertised from `BVP_INBOUND_EMAIL_MAX_RAW_BYTES`, oversize ⇒
  **552**;
* opportunistic STARTTLS when a cert/key pair is mounted.

Run locally:

```bash
uv run python -m bvmta.server
```
