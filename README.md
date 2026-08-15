# TOON vs JSON Benchmark — deployable demo

Two services:
- **api/** — serves the 21 experiment cases (18 format x compression combos + 3 cache modes)
  off a bundled, fixed 10,000-record dataset (`api/data/dataset_flat.json`,
  `api/data/dataset_nested.json`).
- **frontend/** — the "requesting server." Serves a browser UI; when you click Run, the
  *server* (not your browser) fires the repeated requests against the API and returns
  aggregated stats to display.

## 1. Push to GitHub

```bash
cd deploy
git init
git add .
git commit -m "TOON vs JSON benchmark"
git branch -M main
git remote add origin https://github.com/<you>/toon-benchmark.git
git push -u origin main
```

## 2. Deploy on Render

This deploys the two services **in different regions on purpose** —
`toon-benchmark-api` in **Singapore** and `toon-benchmark-frontend` in
**Ohio (US East)** — so the benchmark includes real, substantial network
latency instead of the near-zero localhost latency from earlier tests.

**Important: `API_BASE_URL` must be a public URL, not a private hostname.**
Render's `fromService` / `property: host` returns the service's *private
network* hostname (e.g. `toon-benchmark-api`, no `.onrender.com`), which only
resolves between services **in the same region**. Since these two services
are deliberately in different regions, that private hostname is unresolvable
from the other region — it causes a DNS failure (`Name or service not known`),
not a connection or timeout error. This is why `render.yaml` sets
`API_BASE_URL` as a plain public URL (`https://toon-benchmark-api.onrender.com`)
instead of using `fromService`.

If you deployed before this fix and are still seeing the DNS error: go to
the **frontend service → Environment tab** on Render and manually set
`API_BASE_URL` to the API service's exact public URL shown on its own
dashboard page (copy it exactly — Render sometimes appends a random suffix
to the subdomain if the plain name was taken), then save (this triggers a
redeploy).

**Option A — Blueprint (recommended, one click):**
1. In the Render dashboard: New -> Blueprint -> connect this repo.
2. Render reads `render.yaml` and creates both services automatically, wiring
   `API_BASE_URL` on the frontend to the API service's hostname for you.
3. Click Apply. Both services deploy (free tier). After the api service is
   live, double-check the frontend's `API_BASE_URL` env var matches the
   api service's actual public URL exactly (see note above).

**Option B — Manual (two separate Web Services):**
1. New -> Web Service -> connect repo -> set **Root Directory** to `api`.
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/health`
2. New -> Web Service -> connect repo -> set **Root Directory** to `frontend`.
   - Same build/start commands, health check `/health`.
   - Add env var `API_BASE_URL` = the URL Render gave the api service, e.g.
     `https://toon-benchmark-api.onrender.com`.

## 3. Verify

- `https://<api-service>.onrender.com/health` -> `{"status": "ok"}`
- `https://<api-service>.onrender.com/cases` -> lists all 21 cases
- `https://<frontend-service>.onrender.com/` -> the UI; pick a case, click Run.

## 4. UptimeRobot

Render's free tier spins services down after ~15 minutes idle, causing a slow
"cold start" on the next request. Add both health endpoints as HTTP(s) monitors
in UptimeRobot, checked every 5 minutes, to keep them warm:

- `https://<api-service>.onrender.com/health`
- `https://<frontend-service>.onrender.com/health`

Note: pinging keeps a free-tier service *awake between* pings, but Render free
instances still have a hard monthly runtime cap — check current limits on
Render's pricing page if you plan to keep this running continuously.

## 5. The 21 cases

- Cases 1-18: `format` (json/toon) x `encoding` (identity/gzip/brotli) x
  `structure` (flat/nested) x `n` (10/1000/10000) — see `GET /cases` on the api
  service for the exact list.
- Case 19 (`json_cache`): server caches pre-serialized JSON bytes; repeat requests
  are served from that cache.
- Case 20 (`toon_cache`): same, caching TOON bytes instead.
- Case 21 (`canonical_cache`): cache stores **only TOON** bytes (the smaller
  canonical form); a JSON-format request converts TOON -> JSON on read, on every
  request. Tests whether caching one compact canonical form + converting on
  demand beats caching both formats separately. Flat structure only — the
  TOON-to-JSON decoder bundled here doesn't yet handle nested/indented TOON.

## Known limitations (carry these into any report using this deployment)

- The in-memory cache (`_CACHE` dict in `api/main.py`) is per-process. On
  Render's free tier this is fine (single instance), but it will NOT behave
  correctly if you scale the api service to multiple instances without adding
  a shared cache (e.g. Redis).
- Cross-region network latency between the two Render services (and between
  you and them) is now real, unlike the earlier localhost tests — expect
  latency numbers to look different, and generally more favorable to TOON's
  byte-size advantage, than the localhost runs.
- The bundled dataset is synthetic/randomly generated (fixed seed), not real
  production data.
- Nested-structure TOON encoding is an ad hoc indented-block design for this
  project, not verified against the official TOON spec's nesting syntax.
