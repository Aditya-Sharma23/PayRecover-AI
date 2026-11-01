# Deploying PayRecover AI

PayRecover AI ships as one Docker image. FastAPI serves the API and the built dashboard from the same origin, so no CORS setup is needed. Data (SQLite database, audit log and screenshots) lives in `/data`; mount a persistent volume there.

## Two modes

| | Local (default) | Public (`TESTPILOT_PUBLIC_DEPLOYMENT=true`) |
|---|---|---|
| Who can call it | Anyone who can reach the port (bind to 127.0.0.1) | Only holders of an API token |
| Where checks run | In the API process | Always in a spawned, isolated worker process |
| History | One shared owner, `local` | Separate history per token owner |
| Startup | Always starts | **Refuses to start** unless every gate below passes |

## Public-mode gates (checked at startup and at `GET /api/deployment/readiness`)

| Gate | Requirement |
|---|---|
| Authentication | `TESTPILOT_AUTH_ENABLED=true` |
| Tokens | `TESTPILOT_API_TOKENS="alice:<token>,bob:<token>"` (or one `TESTPILOT_API_TOKEN`, owner `default`). Every token is at least 32 characters. |
| Rate limiting | `TESTPILOT_RATE_LIMITING=true` (60 requests per minute per user and path) |
| Audit log | `TESTPILOT_AUDIT_LOGGING=true` (JSON lines in `/data/audit.log`, never credentials or query strings) |
| TLS | `TESTPILOT_PUBLIC_URL=https://…`, the address users open. The platform terminates TLS; the app then sends HSTS. |
| CORS | `TESTPILOT_CORS_ORIGINS` is optional and, if set, lists only explicit `https://` origins |
| Secrets | No `backend/.env` file in the deployment. Put secrets in the platform's secret store as environment variables. |

Generate a token with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Controls that are always on (enforced in code, not configuration)

- **Destination policy**: only `http(s)` URLs whose every DNS answer is a public address; no credentials, fragments or local hostnames.
- **Connection pinning**: every request is made by the backend's own HTTP client, pinned to the addresses that were validated for that request, so DNS rebinding cannot redirect it.
- **Browser network lockdown**: Chromium never connects anywhere itself. Every page request is intercepted and served by the pinned client (same origin, GET/HEAD only). Chromium is also started with an unresolvable host map, a dead proxy and WebRTC UDP disabled, so a request that escaped interception could not leave the container.
- **Bounds**: 100 requests and 2 MB per response; WebSockets and service workers blocked; time limits on every run and step.
- **Retention**: runs older than `TESTPILOT_RETENTION_DAYS` (default 30) or beyond `TESTPILOT_MAX_RUNS_PER_OWNER` (default 200) are deleted with their screenshots.

For defence in depth on your own infrastructure, you can additionally block outbound traffic from the container to private ranges (RFC 1918, 169.254.0.0/16, ::1, fc00::/7) with a firewall or network policy.

## Run the container locally

```powershell
docker compose up --build
# open http://127.0.0.1:8000
```

`docker-compose.yml` reads `backend/.env` if it exists (for the Gemini key) and binds to 127.0.0.1 only.

## Deploy to Render (blueprint included)

`render.yaml` creates three services: **payrecover-ai** (backend and dashboard, Docker, 1 GB disk at `/data`),
**payrecover-main-store** and **payrecover-vuln-store** (the two Sandbox demo stores, free plan).

1. Push the repository to GitHub.
2. In Render: **New → Blueprint**, choose the repository. Render lists the values marked `sync: false`.
3. Fill them in (the stores' URLs are `https://<service-name>.onrender.com` unless Render gives another):

| Service | Setting | Value |
|---|---|---|
| both stores | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | your Sandbox app's Client ID and Secret |
| payrecover-ai | `TESTPILOT_PAYPAL_CLIENT_ID`, `TESTPILOT_PAYPAL_CLIENT_SECRET` | the same Client ID and Secret |
| payrecover-ai | `TESTPILOT_PUBLIC_URL` | `https://payrecover-ai.onrender.com` |
| payrecover-ai | `TESTPILOT_STORE_URL` | `https://payrecover-main-store.onrender.com` |
| payrecover-ai | `TESTPILOT_VULN_STORE_URL` | `https://payrecover-vuln-store.onrender.com` |
| payrecover-ai | `TESTPILOT_GOOGLE_API_KEY` | your Gemini key |
| payrecover-ai | `TESTPILOT_ZAPIER_WEBHOOK_URL` | your Zapier Catch Hook URL |
| payrecover-ai | `TESTPILOT_BREVO_API_KEY`, `TESTPILOT_ALERT_SENDER` | your Brevo API key and the sender email you verified in Brevo |
| payrecover-ai | `TESTPILOT_PAYPAL_WEBHOOK_ID` | leave empty for now (step 6) |

4. Deploy. Check `https://payrecover-ai.onrender.com/api/deployment/readiness` shows `"ready": true`.
5. Open the site, expand **API token**, and paste `TESTPILOT_API_TOKEN` from the Render environment page.
6. PayPal webhooks: on developer.paypal.com open your Sandbox app → **Webhooks → Add webhook**, URL
   `https://payrecover-ai.onrender.com/api/paypal/webhook`, events **Payment capture completed** and
   **Customer dispute created**. Copy the webhook's **ID** into `TESTPILOT_PAYPAL_WEBHOOK_ID` and redeploy.
   PayRecover verifies every event with PayPal before reading it.

Free Render services sleep after about 15 minutes idle and take 30 to 60 seconds to wake; open the two stores
first before a demo. The Vulnerable store is deliberately insecure (reflected XSS, editable prices); it only
ever talks to the PayPal Sandbox, so never give it live PayPal keys.

A persistent disk needs a paid Render plan. Without one, history is lost on each deploy.

## Deploy anywhere else that runs a container

Fly.io, Railway, Google Cloud Run, Azure Container Apps and a plain VM all work the same way:

```bash
docker build -t payrecover-ai .
docker run -d -p 8000:8000 -v payrecover-data:/data \
  -e TESTPILOT_PUBLIC_DEPLOYMENT=true \
  -e TESTPILOT_AUTH_ENABLED=true \
  -e TESTPILOT_API_TOKENS="alice:$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  -e TESTPILOT_RATE_LIMITING=true \
  -e TESTPILOT_AUDIT_LOGGING=true \
  -e TESTPILOT_PUBLIC_URL=https://testpilot.example.org \
  -e TESTPILOT_GOOGLE_API_KEY=... \
  -e FORWARDED_ALLOW_IPS='*' \
  payrecover-ai
```

Put a TLS-terminating proxy (the platform's, Caddy or nginx) in front. Set `FORWARDED_ALLOW_IPS` to the proxy's address, or `*` when the platform proxy is the only way in. The platform must provide at least 1 GB of memory and 512 MB of shared memory for Chromium.

## Scaling limits

Run **one instance**. Rate limiting, the job queue and SQLite are per process. Scaling out would need a shared database (for example PostgreSQL), a shared rate limiter and a shared queue.

## A local model instead of Google's API

Set `TESTPILOT_AI_PROVIDER=ollama`, `TESTPILOT_OLLAMA_URL` (default `http://127.0.0.1:11434`) and `TESTPILOT_AI_MODEL` (for example `llama3.2`) to use a local Ollama server instead.
