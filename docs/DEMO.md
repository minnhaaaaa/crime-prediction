# Demo path (Person 3)

This is the live-only deployment demonstration. It uses the same tenant,
review, feature and forecast contracts as production. The demo substitutes
Postgres queueing, shared restricted storage and development personas for cloud
infrastructure, but live video analysis uses the real server-side Reka provider.

## One-command demo (Docker)

```bash
REKA_API_KEY=<server-side-key> docker compose up --build
# dashboard: http://localhost:8080
```

The stack contains Postgres, migrations, API, web, and isolated upload, index
and analyze workers. Processing state, reviews, promoted events, measured
coverage, future features and forecasts survive API/worker restarts.
The UI has no MP4 upload route. It continuously plays a fixed, allowlisted
LADOTD HLS source; analysis captures one 12-second segment on demand. If Reka is
not configured, the analysis control is disabled—no fabricated candidate is
substituted.

## Local development

```bash
# API (terminal 1)
python -m pip install -e ".[api,model]"
uvicorn src.api.app:app --env-file .env --port 8000

# Web (terminal 2)
cd src/web
pnpm install
pnpm dev
# dashboard: http://localhost:5173  (proxies /v1 to :8000)
```

## Demo tenants

| Tenant | Bearer token | Role | Region |
|---|---|---|---|
| Demo Tenant One | `demo-token-one` | admin | Bengaluru |
| Demo Tenant One | `demo-reviewer-one` | reviewer | Bengaluru |
| Demo Tenant One | `demo-viewer-one` | viewer | Bengaluru |
| Demo Tenant Two | `demo-admin-two` | admin | Chennai |
| Demo Tenant Two | `demo-reviewer-two` | reviewer | Chennai |
| Demo Tenant Two | `demo-token-two` | viewer | Chennai |

The web UI switches tokens with the tenant chips. Grids are disjoint by
construction; `tests/api` proves tenant A cannot read tenant B's cells and that
a client-supplied `tenant_id` query parameter is rejected.

## Tests

```bash
python -m pytest tests/api        # tenant isolation, contracts, AI fail-safety
cd src/web && pnpm build           # typecheck + production build
```

## Integrated presentation flow

1. Open the landing page and choose **See live prediction**. Sign in as
   **Tenant admin · Demo One**.
2. On **Live operations**, show the continuously playing CCTV feed and select
   **Analyze next 12 seconds**. Playback continues while the server captures a
   bounded MP4 and persists an upload job.
3. Follow live capture → Reka upload → Vision indexing → candidate analysis in
   the custody rail. Dedicated workers own each provider operation.
4. Open **View evidence** on any unconfirmed candidate. The browser receives a
   private, no-store response only after reviewer authorization; no storage key
   or Reka video ID is exposed.
5. Select **Confirm & predict** once. The immutable review promotes one aggregate
   incident, builds an unlabelled future feature snapshot with `data_as_of <
   interval_start`, and atomically publishes the next six-hour window.
6. Open the updated **Crime prediction** map. The supported cell exposes the
   historical-rate forecast and uncertainty; unsupported cells are suppressed,
   never shown as zero.
7. Switch to **Tenant admin · Demo Two**. Tenant One's jobs, candidates, evidence,
   coverage, events and forecasts are absent because tenant context is resolved
   server-side and enforced by Postgres RLS.

If Reka proposes no incident, say so: that is a valid model result. Capture a
different interval instead of claiming a deterministic detection.

## Restart check

During a queued or running upload:

```bash
docker compose restart api worker-index
docker compose ps
```

Refresh the processing page. Postgres preserves the run and an expired broker
lease becomes available to a restarted worker.

## Near-live API check

Start the services, configure `REKA_API_KEY` in `.env`, then run:

```bash
curl -sS -X POST http://localhost:8000/v1/demo/near-live-cctv/captures \
  -H 'Authorization: Bearer demo-token-one' \
  -H 'Idempotency-Key: near-live-demo-0001' \
  -H 'Content-Type: application/json' \
  -d '{"source_key":"louisiana-dot-i20","duration_seconds":12}'
```

Poll the returned run ID:

```bash
curl -sS http://localhost:8000/v1/ingestion/runs/REPLACE_RUN_ID \
  -H 'Authorization: Bearer demo-token-one'
```

The fixed demo feed is a public LADOTD/511 Louisiana HLS camera. Availability
is outside this application's control. Public reachability does not grant a
general redistribution licence: keep the segment restricted, attribute the
source, apply the one-day demo retention policy, and do not publish raw footage.

The dedicated AWS hackathon composition enables this one fixed source with
`PUBLIC_HLS_DEMO_ENABLED=true`. Set the value to `false` to stop new captures.
The setting does not create a general URL input: the API accepts only the
literal `louisiana-dot-i20` source key, and the capture adapter resolves that
key from its server-side HTTPS HLS allowlist before recording a bounded clip.

## Reka

The copilot selects live Reka Chat when a server-side key is configured and otherwise uses a deterministic provider so the basic map demo runs offline. Phase 2 adds real Reka Vision video management and analysis behind FastAPI.

One server-side secret is used for both capabilities:

```text
REKA_API_KEY=<secret from the Reka platform>
```

There is no separate `REKA_VISION_API_KEY` in this repository. Do not add the key to Vite, React, browser storage, committed Compose files, fixtures, or logs.

The Phase 2 video demo is:

1. tenant admin uploads an approved MP4 to FastAPI;
2. FastAPI calls Reka Vision upload with indexing enabled;
3. the UI shows upload/index/analysis status without exposing the Reka video ID;
4. Reka Q&A/tagging returns structured candidate proposals; a validated empty
   array completes as “No candidate in this segment”;
5. schema-invalid or prohibited output fails safely, and exhausted indexing is
   reported as `reka_index_timeout` rather than as an empty result;
6. a human reviewer confirms one candidate and rejects another;
7. only the confirmed candidate becomes an incident event;
8. the local deterministic model creates the future aggregate forecast;
9. Reka Chat may explain supplied aggregate facts but never creates the numeric risk score.

Near-live capture returns `202 Accepted` immediately with a temporary capture run.
The client polls that run while the bounded HLS segment is recorded; after media
validation, the capture run exposes the asset and the durable upload/index/analyze
jobs continue. A client disconnect therefore does not lose or duplicate accepted
capture work.

Automated tests use fake HLS capture and fake Reka Vision/Chat providers and
make no network calls. The real demo uses the same services with the allowlisted
HLS adapter and server-only Reka key.

The feed provenance is LADOTD's official `GET /api/v2/get/cameras` catalogue:
source `101`, view `2206`, whose documented `VideoUrl` is the server-side HLS
playlist used by the adapter. The catalogue endpoint itself requires a 511LA
developer key and is not called at runtime; the bounded segment is fetched
directly from the official public HLS `VideoUrl`. Do not describe this as an
unkeyed catalogue API, and obtain permission before uploading public footage to
Reka or retaining it beyond the live demonstration.
