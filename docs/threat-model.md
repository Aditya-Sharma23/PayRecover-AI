# Threat model

TestPilot AI makes network requests to URLs that users type in, runs a real browser against them, and sends report data to an AI model. That makes it a server-side request forgery (SSRF) tool if it is careless. This document lists what we defend against and how.

## Assets

- The network the server runs in (cloud metadata at 169.254.169.254, internal services, localhost admin ports).
- Users' API tokens, request headers they supply (often credentials), and values typed into flows.
- Each user's run history and screenshots.
- The Gemma API key.

## Threats and mitigations

| Threat | Mitigation | Test |
|---|---|---|
| SSRF to private, loopback, link-local or metadata addresses | URL policy accepts only `http(s)`; every DNS answer must be `ip.is_global`; literal IPs checked too | `test_url_policy` (10.x, 169.254.169.254, 0.0.0.0, `[::1]`, `[fe80::1]`, `file://`, `ftp://`, `.localhost`) |
| DNS rebinding (public on check, private on connect) | Every request is resolved once, validated, and the connection pinned to those addresses (`PinnedResolver`) | `test_runner_helpers` |
| Browser makes its own connections (subresources, prefetch, WebRTC) | All requests intercepted by `context.route` and served by the pinned client; cross-origin refused; Chromium started with unresolvable hosts, a dead proxy and WebRTC UDP disabled; WebSockets and service workers blocked | `test_cross_origin_subresources_are_never_fetched` |
| A test changes data on the target (form posts, deletes) | Browser: only GET/HEAD reach the network; anything else gets 405. API runner: GET only, no body, no redirects. OpenAPI import only makes GET operations runnable | `test_user_flow_*`, `test_openapi_import` |
| Resource exhaustion | Request budget (100), 2 MB response cap, 1 MB screenshot cap, run and step timeouts, bounded job queue and workers, YAML aliases refused, `$ref` expansion budget, rate limiting | `test_jobs`, `test_openapi_import` |
| Header injection / host override via custom headers | Header names must be RFC 7230 tokens; `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, proxy headers and others refused; values may not contain CR, LF or NUL | `test_unsafe_headers_and_bad_inputs_are_rejected` |
| Secrets leaking into reports, logs or Gemma | Header values and typed flow values are never stored (only lengths); secret-looking query parameters redacted; page text is not stored; audit log has no credentials or query strings; Gemma only sees the stored report | `test_custom_headers_are_sent_but_never_recorded`, flow test |
| One user reading another's data | Every run, job and screenshot row has an owner; every query filters by the caller's owner; screenshots are served only if one of the caller's reports references them | `test_auth_is_required_and_each_user_sees_only_their_runs` |
| Token guessing / timing | Tokens ≥ 32 characters in public mode; comparison of SHA-256 digests with `hmac.compare_digest`; rate limiting | `test_security`, `test_deployment` |
| XSS through target-controlled evidence | React escapes all text; HTML report escapes every value and embeds screenshots as data URIs; accessibility help links are shown only for `https://dequeuniversity.com/`; strict CSP on app responses | `test_report_html` |
| Untrusted page tampering with in-page checks | Recorded as a limitation on the accessibility check; status checks that matter (HTTP status, console) are observed from outside the page | — |
| AI inventing results | Gemma never executes anything; plans are validated against the allowed checks; the overall status is derived by the schema validator from check records only; explanations are labelled advisory | `test_gemma`, `test_schemas` |
| Unsafe public deployment | Public mode refuses to start without auth, strong tokens, rate limiting, audit logging, HTTPS and secrets outside the image; runs move into isolated worker processes | `test_server_refuses_to_start_when_public_gates_fail` |

## Out of scope / residual risk

- A single instance only: rate limiting and the queue are in memory.
- Authorization to test a target is self-declared by the user (a checkbox). Public deployments should be limited to trusted users with tokens.
- Chromium vulnerabilities: the browser runs unsandboxed inside the container (Playwright default), as a non-root user. Keep the image updated.
