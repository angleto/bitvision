# Fascicolo consultation — end-to-end multimodal agent

A self-contained reference that demonstrates the
**"analizza il fascicolo di un paziente"** flow against bitvision
phoenix, using the REST API + the Anthropic Messages API directly —
**no MCP, no LangChain, no orchestration framework**.

The script simulates the planned M3 (`get_fascicolo_bundle`) + M1
(`get_series_thumbnail`) MCP tools client-side by composing the
underlying REST calls, then invokes Claude with a multimodal prompt
(text + image blocks) and POSTs the structured response back as a
`Consultation` record.

It is the **headless** counterpart to the interactive Claude Desktop
setup ([docs/claude-desktop-quickstart.md](../../docs/claude-desktop-quickstart.md)).

## What it does

```
 ┌───────────────┐        ┌───────────────────┐        ┌──────────────┐
 │ bitvision API │──(1)──▶│   agent.py        │──(4)──▶│ Anthropic    │
 │  (fascicolo,  │        │ build bundle +    │        │ Messages API │
 │  thumbnails)  │◀─(2)───│ multimodal prompt │◀─(5)───│  (Claude)    │
 └───────────────┘        └────────┬──────────┘        └──────────────┘
          ▲                        │(6) parse
          │(7) POST /consultations │
          └────────────────────────┘
```

1. `GET /api/patients/{id}` — profile
2. `GET /api/patients/{id}/index` — fascicolo index
3. `GET /api/patients/{id}/timeline` — chronological events
4. `GET /api/studies?patient_id=…` — studies with series listing
5. `GET /api/series/{id}/thumbnail` — JPEG per series → base64
6. `POST https://api.anthropic.com/v1/messages` — Claude multimodal
7. `POST /api/consultations` — persist Claude's output

Output: the URL of the created consultation (`/consultations/{id}`).

## Requirements

- Python >= 3.11 and [`uv`](https://docs.astral.sh/uv/)
- A bitvision phoenix backend you can reach
- An **agent token** with `scope=read+write` for the target patient
  (see [claude-desktop-quickstart.md](../../docs/claude-desktop-quickstart.md)
  step 2)
- An **Anthropic API key** with access to a vision-capable Claude
  model (default: `claude-opus-4-7-1m`)

Deps: only [`httpx`](https://www.python-httpx.org/) and the official
[`anthropic`](https://pypi.org/project/anthropic/) SDK.

## Usage

```bash
cd examples/fascicolo_consultation
uv sync

export BVP_BASE_URL=http://localhost:8000
export BVP_AGENT_TOKEN=<jwt-from-/api/agent-tokens-or-/api/auth/login>
export ANTHROPIC_API_KEY=sk-ant-...
export PATIENT_ID=<patient-uuid>

uv run python agent.py
```

Optional flags (override env):

```bash
uv run python agent.py \
  --base-url https://app.bit.vision \
  --patient-id 8b2a34de-4e0e-4c0f-9d1b-3f0a1b2c3d4e \
  --model claude-opus-4-7-1m \
  --max-thumbs-per-study 3 \
  --dry-run     # skip the POST /api/consultations step
```

Exit codes: `0` success · `1` HTTP / auth / Anthropic error · `2`
bad CLI / missing env.

## Expected output

```
[1/6] Fetching patient profile…
[2/6] Fetching fascicolo index + timeline…
[3/6] Fetching 3 studies (8 series)…
[4/6] Fetching thumbnails…
       ✓ series a1b2… (180 KB)
       ✓ series c3d4… (172 KB)
       …
[5/6] Invoking Claude (claude-opus-4-7-1m, 11 content blocks)…
[6/6] POST /api/consultations…

Consultation created: http://localhost:8000/consultations/7f3a…
```

## Notes

- **Privacy**: if the agent token is configured with `deidentify=ON`,
  the bundle and metadata reaching Claude are already scrubbed of
  direct identifiers by the backend. The script does no further
  scrubbing.
- **Thumbnails only, no full volumes**: `get_series_thumbnail` returns
  a 2D slice (middle by default). For full-volume analysis, extend
  the script to iterate slices — see backend
  `/api/series/{id}/thumbnail?index=N` and `x-slice-count` response
  header.
- **Missing `/api/consultations`?** If your backend hasn't landed
  C4/M4 yet (the Consultation resource), pass `--dry-run` and the
  script will print the payload it *would* have posted.

## See also

- [docs/claude-desktop-quickstart.md](../../docs/claude-desktop-quickstart.md) — interactive version with Claude Desktop
- [docs/agent-protocols.md](../../docs/agent-protocols.md) — full MCP / A2A / REST architecture
- [`examples/doctor_agent/`](../doctor_agent/) — minimal A2A reference client
