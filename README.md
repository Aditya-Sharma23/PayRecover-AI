# TestPilot AI

An evidence-first testing workbench. Describe what you want to verify; **Gemma 4** turns it into a reviewable test plan; real **Playwright** and HTTP runners execute it under strict safety bounds; and every pass or fail points back to observed evidence. Gemma then explains the results, but it can never change them.

MIT licensed. Built for the Hacktoberfest Hack Day Jaipur × Hack IT Sapiens.

## What it does

This is the final build: the v1.0 workbench (flows, API assertions, jobs, auth, compare) merged with the single-URL security scanner (cookie, exposed-file and active probes, Schemathesis, dashboard), in the scanner's dark dashboard UI.

**Website checks** (real Chromium through the pinned safe runner), 17 in all:
HTTP status · load time · response headers · body has content · page title · exactly one H1 · console errors · link inventory · expected text (optionally after a click) · multi-step user flows · security header policy (CSP, HSTS, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, clickjacking) · cookie flags (Secure, HttpOnly, SameSite) · exposed files (`/.env`, `/.git/HEAD`, `/backup.zip`, HEAD only) · opt-in active probes (inert XSS canary, unauthenticated admin routes) · axe-core WCAG 2.0/2.1/2.2 A+AA with a labelled static fallback · keyboard focus · OpenAPI contract (Schemathesis, GET only).

**Full scan:** one URL, one click, the 14 checks that need no extra input. Active probes stay opt-in.

**API checks** (one bounded GET), 9 in all:
status code · response time · content type · no redirect · response size · JSON field assertions · JSON Schema · sample JSON shape · OpenAPI contract (Schemathesis). Plus custom request headers (values never stored) and OpenAPI / Swagger import.

**Around the runs:**
Gemma 4 planning and explanation, with a built-in summary when Gemma is not configured; a risk level that Gemma can raise but never lower · dashboard totals · per-category report panels · vulnerability cards · HTML and JSON reports · history with view, delete, clear and compare (fixed / regressed) · background jobs in isolated worker processes · retention policy · per-user tokens.

## Safety model, in one paragraph

Only `http(s)` targets whose every DNS answer is a public address are allowed, and the user must confirm they are authorized. The backend makes every request itself, pinned to the addresses it validated, so DNS rebinding cannot redirect it. Chromium has no network of its own: every page request is intercepted and served by that pinned client (same origin, GET/HEAD only), and the browser is launched with unresolvable hosts, a dead proxy and WebRTC UDP disabled. Tests are read-only by construction. Secrets you type (headers, form values) are used once and never stored. See [docs/threat-model.md](docs/threat-model.md).

## Quick start (local)

Requirements: Python 3.12+, Node.js 20.19+ or 22.12+. Already set up in this folder: run `.\scripts\start.ps1` and open http://127.0.0.1:8010.

```powershell
# Backend
cd backend
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m playwright install chromium
copy .env.example .env      # then put your Google AI Studio key in it
uvicorn app.main:app --port 8010   # serves the built dashboard too

# Frontend (second terminal)
cd frontend
npm install
npm run build               # or: npm run dev (http://127.0.0.1:5173, set TESTPILOT_API_PROXY=http://127.0.0.1:8010)
```

Or run everything in one container: `docker compose up --build`, then open http://127.0.0.1:8000.

## Demo target

`demo-site/` holds a sign-in page and an intentionally broken page (no H1, a console error, an image without alt text, an unnamed button). TestPilot blocks localhost by design, so publish it: push to GitHub and the included **Publish demo site** workflow deploys it to GitHub Pages (Settings → Pages → Source: GitHub Actions). See [steps.md](steps.md) for demo scenarios.

## Tests and checks

```powershell
.\scripts\validate.ps1      # ruff + backend tests + eslint + frontend tests + build
```

CI (`.github/workflows/validate.yml`) runs the same, then builds the Docker image, checks it serves the API and dashboard, and checks that public mode refuses to start without its safeguards.

## Deploying

One image serves the API and dashboard on the same origin. Public mode (`TESTPILOT_PUBLIC_DEPLOYMENT=true`) **refuses to start** unless authentication with strong tokens, rate limiting, audit logging, an HTTPS public URL and out-of-image secrets are all configured, and it moves every run into an isolated worker process. A Render blueprint (`render.yaml`) is included. Full guide: [docs/deployment.md](docs/deployment.md).

## Repository layout

- `backend/app/` — FastAPI service: `website_runner.py` (Playwright), `api_runner.py`, `openapi_import.py`, `gemma.py`, `security.py`, `deployment.py`, `storage.py` (SQLite), `jobs.py`, `compare.py`, plus `security_checks.py` (cookie, exposure and active probes), `contract_runner.py` (Schemathesis) and `static_a11y.py` (axe fallback).
- `backend/tests/` — 91 unit and integration tests, including real-browser runs against the demo pages.
- `frontend/src/` — React 19 dashboard; `lib/requestBody.js` holds the tested form logic.
- `demo-site/` — authorized demo target.
- `docs/` — [API contract](docs/api-contract.md), [deployment](docs/deployment.md), [threat model](docs/threat-model.md), [decisions](docs/decisions.md), codebase graph (`python scripts/generate_graph.py`).

## API overview

`GET /api/health` · `GET /api/deployment/readiness` · `GET /api/me` · `GET /api/ai/status` · `POST /api/ai/plan` · `POST /api/ai/explain` · `POST /api/targets/preflight` · `POST /api/tests/website` · `POST /api/tests/api` · `POST /api/openapi/import` · `POST /api/jobs` · `GET /api/jobs` · `GET /api/jobs/{id}` · `POST /api/jobs/{id}/cancel` · `GET /api/runs` · `DELETE /api/runs` · `GET /api/runs/compare?base=&head=` · `GET /api/runs/{id}` · `DELETE /api/runs/{id}` · `GET /api/runs/{id}/html` · `GET /api/artifacts/{id}`. Interactive docs at `/docs`. Details in [docs/api-contract.md](docs/api-contract.md).

## Configuration

All settings are environment variables; [backend/.env.example](backend/.env.example) lists them. If `/api/ai/plan` returns `API_KEY_INVALID`, create a new key in Google AI Studio, put only the raw key in `backend/.env`, and restart the backend.
