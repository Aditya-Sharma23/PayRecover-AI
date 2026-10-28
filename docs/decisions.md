# Decision log

Short records of choices that shape the code. Newest last.

1. **Evidence decides, AI advises.** Gemma drafts plans and explains reports; deterministic runners produce every status, and the schema validator derives the overall status from check records. A report cannot claim "passed" without passing checks.
2. **Gemma 4 through Google's Generative Language API by default** (`gemma-4-26b-a4b-it`, faster than `31b`). An Ollama adapter (`TESTPILOT_GEMMA_PROVIDER=ollama`) covers local inference. Gemma 4 "thought" parts are ignored.
3. **The backend fetches everything for the browser.** Playwright intercepts every request and serves it from an aiohttp client pinned to validated public addresses. This is what makes SSRF and DNS-rebinding protection hold for a real browser. Chromium additionally gets no DNS, a dead proxy and no WebRTC UDP.
4. **Read-only by construction.** Only GET/HEAD reach the network from the browser, and the API runner sends a single GET with no redirects. Flows can click and type, but form submissions cannot change data. OpenAPI import marks non-GET operations as not runnable.
5. **Never store secrets the user typed.** Request header values and flow `fill` values are used once and only their presence or length is recorded.
6. **SQLite with an owner column** instead of a server database: one file, zero setup, good enough for one instance. Every row is owned; with auth off the owner is `local`.
7. **Public mode fails closed at startup.** Gates check real configuration (token length, HTTPS URL, no `.env` file) instead of self-declared flags, and the server will not start until they pass.
8. **One container, one origin.** FastAPI serves the built dashboard, so production needs no CORS and the token never crosses origins. In development Vite proxies `/api`.
9. **Accessibility via vendored axe-core 4.10.3** (MPL-2.0), injected with `page.evaluate` so the target's CSP cannot block it and no extra network request is made. Critical/serious violations fail; moderate/minor warn.
10. **JSON Schema draft 2020-12 via `jsonschema`**, with no remote `$ref` retrieval. OpenAPI schemas are inlined from local `#/` references only, with depth and size budgets.
11. **Run comparison uses stored reports only.** Nothing is re-run; changes are classified as fixed, regressed, changed, unchanged, added or removed.
12. **MIT licence** for the project, so it qualifies as open source.
