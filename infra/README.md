# infra — development and deployment artefacts

## Development

`docker-compose.yml` starts only **infrastructure** services locally:

- **postgres** (pgvector/pgvector:pg16) — Postgres 16 with pgvector
  enabled. Bootstrap SQL in `postgres/01-init.sql`.
- **redis** — for Arq task queue and caching.
- **minio** (+ `minio-init`) — S3-compatible dev storage on port 9000
  (console on 9001). Buckets `bvphoenix-raw` and `bvphoenix-derivatives`
  are created automatically.

Application services (**backend**, **workers**, **crawler**, **mcp**,
**frontend**) are run on the host for faster iteration:

```sh
make up                  # start the infra above
make backend.dev         # API on :8000
make workers.dev         # Arq worker
make frontend.dev        # Next.js on :3000
```

Stop:

```sh
make down
```

## Production

`dockerfiles/` contains multi-stage Dockerfiles for the application
services:

- `backend.Dockerfile`
- `workers.Dockerfile` (includes `dcm2niix` and imaging libs)
- `crawler.Dockerfile`
- `mcp.Dockerfile` — stdio transport
- `mcp-http.Dockerfile` — HTTP transport (ADR 0019)
- `frontend.Dockerfile`

Images are built from the repo root:

```sh
docker build -f infra/dockerfiles/backend.Dockerfile -t bvphoenix-backend .
```

The production deployment lives in the sibling repo
`deploy/bvphoenix-production-k8s-deploy/` (Scaleway Kapsule, fr-par,
ARM nodes, Traefik + Let's Encrypt). Secrets ride External Secrets
Operator backed by Scaleway Secret Manager (project `bitvision`); see
the memory `scaleway_secret_manager_via_eso` for the cutover history.
Helm charts are intentionally not used: raw manifests under
`deploy/.../{deployment,service,ingress,middleware-*,network-policies}*.yaml`
plus a `./redeploy.sh` script.

Pod security baseline (set in 3.7.9 hardening):
`runAsNonRoot: true`, `allowPrivilegeEscalation: false`,
`capabilities: drop ALL`, `seccompProfile: RuntimeDefault`. Frontend
and `mcp-http` additionally enable `readOnlyRootFilesystem`.

## Object storage in production

Dev uses MinIO because it's local and free. Production pick is
S3-compatible (leading candidate: **Cloudflare R2** for free egress;
**Backblaze B2** for cost; **AWS S3** / **Scaleway** depending on
residency). Swap is one config change (`BVP_S3_ENDPOINT_URL`, keys,
bucket names). Nothing in application code is provider-specific.
