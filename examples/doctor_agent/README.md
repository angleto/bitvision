# Doctor-Agent Reference Client (A2A)

A minimal, self-contained reference implementation of an **A2A v1.0** client for
bitvision phoenix. It shows external developers how to drive the
Agent-to-Agent protocol end-to-end: discover the Agent Card, send a task,
poll until it reaches a terminal state, and handle `INPUT_REQUIRED` turns.

The client talks to the endpoints exposed by bitvision phoenix:

- `GET  /.well-known/agent-card.json` — Agent Card (discovery)
- `POST /api/a2a` — JSON-RPC 2.0 task endpoint

Only stdlib + [`httpx`](https://www.python-httpx.org/) are used — no LLM
frameworks, no orchestration libraries. The whole client is ~200 lines.

## Prerequisites

1. **Backend running** on `http://localhost:8000` (or pass `--backend`).
   From the repo root:

   ```bash
   make up.infra
   make db.migrate
   make backend.dev
   ```

2. **A JWT token** issued by bitvision phoenix. Register a user and log in:

   ```bash
   # register (first time only)
   curl -s -X POST http://localhost:8000/api/auth/register \
     -H 'Content-Type: application/json' \
     -d '{"email":"doc@example.com","password":"changeme","display_name":"Doc"}' \
     | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])"

   # or log in
   export JWT=$(curl -s -X POST http://localhost:8000/api/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"email":"doc@example.com","password":"changeme"}' \
     | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
   ```

## Quickstart

```bash
cd examples/doctor_agent
uv sync
uv run python agent.py --token "$JWT" --query "search for chest CT from 2024"
```

Flags:

| Flag              | Default                   | Description                               |
|-------------------|---------------------------|-------------------------------------------|
| `--token`         | (required)                | JWT Bearer token.                         |
| `--backend`       | `http://localhost:8000`   | Backend base URL.                         |
| `--query`         | (required)                | Initial natural-language request.         |
| `--poll-interval` | `1.0`                     | Seconds between `agent/getTask` polls.    |
| `--timeout`       | `30.0`                    | Overall timeout in seconds.               |

Exit codes: `0` on `COMPLETED`, `1` on `FAILED` (or auth/network error),
`2` on `CANCELED` or timeout.

## Example queries

Each query exercises a different skill declared in the Agent Card.

```bash
# dicom-search
uv run python agent.py --token "$JWT" \
  --query "search for chest CT from 2024"

# similarity-search
uv run python agent.py --token "$JWT" \
  --query "find similar cases to study 3f1c2a9e-..."

# patient-fascicolo
uv run python agent.py --token "$JWT" \
  --query "show fascicolo for patient 8b2a..."

# image-analysis
uv run python agent.py --token "$JWT" \
  --query "describe series 7c41-..."

# radiology-consultation (multi-step)
uv run python agent.py --token "$JWT" \
  --query "trova studi TC toracici con sospette lesioni polmonari"
```

The current backend router is keyword-based: `dicom-search` executes a
real query, while the other skills typically return help-text pointing
to the IDs they need. When a skill asks for more details the client
transitions the task to `INPUT_REQUIRED` and prompts you on stdin; type
a follow-up (e.g. a study ID) to continue the same `contextId`.

## Protocol walkthrough

The client performs exactly four kinds of calls. See
[`docs/agent-protocols.md`](../../docs/agent-protocols.md) for the full
architecture.

1. **Discovery** — `GET /.well-known/agent-card.json` returns the
   Agent Card (name, version, skills, security schemes). No auth.

2. **Send message** — JSON-RPC `agent/sendMessage`. Creates a new task
   (or continues one by passing `taskId`). The `message` object follows
   A2A: `{"role":"user","parts":[{"type":"text","text":"..."}],"messageId":"..."}`.

3. **Poll** — JSON-RPC `agent/getTask` with `{"taskId": "..."}` until
   `status.state` is one of `completed`, `failed`, `canceled`, or
   `input-required`.

4. **Resume** — On `input-required` the client reads a follow-up from
   stdin and calls `agent/sendMessage` again with the same `taskId` and
   `contextId`, looping until terminal.

Artifacts are printed from `task.artifacts[*].parts[*].text`.

Other JSON-RPC methods the backend supports (not used by this client,
but available for exploration): `agent/listTasks`, `agent/cancelTask`,
`agent/getAgentCard`.

## Manual test recipe

```bash
# 1. smoke-test (no backend needed)
uv run python agent.py --help
uv run python -c "import agent; print('ok')"

# 2. end-to-end (backend on :8000, valid JWT)
uv run python agent.py --token "$JWT" --query "search for chest CT"
# expect: agent card summary, task id, artifacts, exit 0
```

If you cannot reach the backend the client prints a clear network error
and exits `1`. `401` responses surface as "token expired/invalid".
