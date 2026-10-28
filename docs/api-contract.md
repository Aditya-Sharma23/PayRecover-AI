# API Contract — 1.0

All bodies are JSON. Unknown fields are rejected (`422`). When authentication is on, send `Authorization: Bearer <token>` (or `X-TestPilot-API-Key`); every run, job and screenshot is scoped to the token's owner. `GET /api/health` and `GET /api/deployment/readiness` never need a token.

## Preflight

`POST /api/targets/preflight` `{"url", "authorization_confirmed": true}` checks URL syntax and that every DNS answer is public. It connects to nothing and is not a test result. `422` with a reason when blocked.

## Website run — `POST /api/tests/website`

```json
{
  "url": "https://authorized-demo.example/",
  "authorization_confirmed": true,
  "checks": ["http_status", "h1", "accessibility", "user_flow"],
  "max_links": 10,
  "timeout_seconds": 20,
  "expected_text": null,
  "click_selector": null,
  "flow_steps": [
    {"action": "fill", "selector": "#email", "value": "ada@example.com"},
    {"action": "click", "selector": "#sign-in-button"},
    {"action": "expect_text", "value": "Password is required"}
  ],
  "capture_screenshot_on_failure": true
}
```

| Check | Passes when | Notes |
|---|---|---|
| `http_status` | 2xx or 3xx | |
| `page_title` | `<title>` is non-empty | |
| `h1` | exactly one non-empty H1 | |
| `console_errors` | no console errors or page errors | |
| `same_origin_links` | never (always `warning`) | inventory only |
| `security_headers` | all four recommended headers present | otherwise `warning` |
| `text_present` | `expected_text` (exact, case-sensitive) is visible | needs `expected_text`; optional single `click_selector` (`error` if the click fails) |
| `accessibility` | no axe-core violations | critical/serious → `failed`, moderate/minor → `warning`; evidence lists rules, impact and up to 3 targets |
| `user_flow` | every step runs and every expectation holds | needs `flow_steps` (1–10) with at least one `expect_text`/`expect_url`. Actions: `click`, `fill`, `press` (key name, optional selector), `wait_for`, `expect_text`, `expect_url`. Each step has a 5 s limit. A failed expectation → `failed`; an action that cannot be performed → `error`; later steps are `not_run`. Runs in a fresh tab. `fill` values are never stored (only `value_length`). |

Every browser request is same-origin GET/HEAD through the pinned client, so clicks cannot submit data. Screenshots (JPEG viewport, ≤ 1 MB) are stored only when a check fails or errors and are referenced as `evidence.screenshot_artifact_id`; the flow check gets the screenshot of its own tab.

## API run — `POST /api/tests/api`

```json
{
  "url": "https://authorized-api.example/users",
  "authorization_confirmed": true,
  "checks": ["api_status", "api_json_assertions", "api_json_schema"],
  "expected_status_code": 200,
  "expected_content_type": null,
  "max_response_time_ms": 5000,
  "max_response_bytes": 2000000,
  "timeout_seconds": 20,
  "request_headers": {"Authorization": "Bearer …"},
  "json_assertions": [
    {"path": "data.items.0.id", "operator": "equals", "expected": 7},
    {"path": "data.items", "operator": "length", "expected": 1},
    {"path": "error", "operator": "not_exists"}
  ],
  "response_schema": {"type": "object", "required": ["data"]}
}
```

| Check | Passes when |
|---|---|
| `api_status` | status equals `expected_status_code` |
| `api_response_time` | elapsed ≤ `max_response_time_ms` |
| `api_content_type` | content type starts with `expected_content_type` (required) |
| `api_redirects` | no 3xx / `Location` (redirects are never followed) |
| `api_response_size` | body ≤ `max_response_bytes` |
| `api_json_assertions` | every assertion holds. Operators: `exists`, `not_exists`, `equals`, `not_equals`, `contains` (string or list), `type` (`string`, `number`, `integer`, `boolean`, `null`, `object`, `array`), `length`. Paths are dot-separated keys or list indexes, or `$` for the root. JSON `true` never equals `1`. |
| `api_json_schema` | body validates against `response_schema` (JSON Schema 2020-12, ≤ 20 KB, no remote `$ref`); up to 10 violations listed |

One GET, no body, no redirects, 2 MB cap. Up to 10 `request_headers`; `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, proxy and hop-by-hop headers are refused, and values may not contain CR/LF. Header values are never stored.

## OpenAPI import — `POST /api/openapi/import`

`{"spec": "<JSON or YAML text, ≤ 500 KB>", "base_url": null}` → `{title, version, base_url, operations[], warnings[]}`. Each operation has `method`, `path`, `url`, `runnable`, `reason` (why not runnable: non-GET, path parameters, required query parameters, no base URL), `expected_status_code`, `expected_content_type` and an inlined `response_schema`. Nothing is executed. YAML anchors/aliases and oversized `$ref` expansion are rejected (`422`).

## Report (both run types)

```json
{
  "schema_version": "1.0",
  "run_id": "3b241101-e2bb-4255-8caf-4136c566a962",
  "target_url": "https://authorized-demo.example/",
  "started_at": "2026-10-25T09:00:00Z",
  "finished_at": "2026-10-25T09:00:03Z",
  "overall_status": "failed",
  "summary": {"passed": 1, "failed": 1, "warnings": 0, "errors": 0, "skipped": 0},
  "checks": [
    {"check_id": "h1", "status": "passed", "duration_ms": 900, "message": "Observed 1 H1 headings; expected exactly one.", "evidence": {"count": 1, "text": ["Sign in to the demo"]}, "limitations": []},
    {"check_id": "user_flow", "status": "failed", "duration_ms": 6400, "message": "Step 3 (expect_text) did not hold; 2 step(s) passed before it.", "evidence": {"steps": ["…"], "final_url": "https://authorized-demo.example/", "screenshot_artifact_id": "…"}, "limitations": ["…"]}
  ],
  "warnings": ["Cross-origin resources and non-GET requests are blocked by the safe runner."]
}
```

The report model rejects summaries that do not match the check records and enforces the overall status: all skipped (or none) → `blocked`; any `failed` → `failed`; else any `error` → `error`; else any `warning`/`skipped` → `warning`; else `passed`. Check statuses: `passed`, `failed`, `warning`, `skipped`, `error`.

A destination blocked at run time produces a `blocked` report with every check `skipped`; an unexpected runner failure produces `error` checks with only the error type.

## Background jobs

`POST /api/jobs` `{"job_type": "website"|"api", "website": {…} | "api": {…}}` → `202` with a job record (`queued`). Jobs run in spawned worker processes (2 at a time, queue of 20; `503` when full). `GET /api/jobs` lists the caller's recent jobs; `GET /api/jobs/{id}` returns `status` (`queued`, `running`, `completed`, `failed`, `cancelled`) and `report_id`; `POST /api/jobs/{id}/cancel`. Records are stored in SQLite; jobs interrupted by a restart become `failed` with `error: "ServerRestarted"`.

## History

- `GET /api/runs?limit=20` — summaries (`run_id`, `target_url`, times, `overall_status`, `summary`, `checks`).
- `GET /api/runs/{id}` — the stored report. `GET /api/runs/{id}/html` — self-contained HTML (screenshots embedded).
- `GET /api/runs/compare?base={id}&head={id}` — per-check `change`: `fixed`, `regressed`, `changed`, `unchanged`, `added`, `removed`, plus `regressions` and `fixes` counts. Nothing is re-run.
- `DELETE /api/runs/{id}` and `DELETE /api/runs` — delete one or all of the caller's runs with their screenshots.
- `GET /api/artifacts/{id}` — a screenshot, only if one of the caller's reports references it.
- Retention: runs older than `TESTPILOT_RETENTION_DAYS` (30) or beyond `TESTPILOT_MAX_RUNS_PER_OWNER` (200) are pruned on each save. `GET /api/me` reports the owner and the policy.

## Gemma

`POST /api/ai/plan` `{"goal", "target_url", "test_type": "website"|"api", "authorized": true}` → `{goal, test_type, checks, expectations, limitations, rationale, suggested_expected_text, suggested_flow_steps, suggested_json_assertions}`. Suggestions are drafts for the user to review; plans with checks from another test type are rejected, and malformed suggestions are dropped with a note. `POST /api/ai/explain` `{"report": {…}}` → `{summary, findings, limitations, next_steps}`, advisory only. `503` when Gemma is not configured, `502` when the provider fails. `GET /api/ai/status` shows provider and model, never the key.

## Added in the final build

- `GET /api/checks` returns every check name per test type and the `full_scan` preset.
- `GET /api/dashboard` returns `total_runs`, `failed_runs`, `security_issues`, `accessibility_issues` and `vulnerabilities` for the caller's stored runs.
- `POST /api/ai/explain` no longer fails when Gemma is not configured: it returns a built-in summary with `source: "Built-in summary"`. Every explanation carries `risk_level` (`HIGH`, `REVIEW`, `LOW`, `UNKNOWN`), which Gemma can raise but never lower.
- Website requests accept `max_load_time_ms` (default 5000) and the checks `response_time`, `response_headers`, `body_not_empty`, `keyboard_focus`, `cookie_flags`, `exposed_resources`, `active_probes`, `openapi_contract`.
- API requests accept `expected_json_example` and the checks `api_json_shape` and `openapi_contract`.
- Security checks put review items in `evidence.findings` as `{title, severity, detail, recommendation}`.
