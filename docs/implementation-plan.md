# Implementation Plan

All planned stages for version 1.0 are complete. The build log is in the repository root `memory.md`; reasons for key choices are in `docs/decisions.md`.

1. Scaffold backend and frontend; health endpoint. — done
2. Canonical request and report schemas with derived statuses. — done
3. Destination safety: URL policy, public-DNS validation, pinned connections, same-origin GET/HEAD browser gateway, request/size/time bounds. — done; browser network lockdown added in 1.0
4. Deterministic website checks: status, title, H1, console, links, security headers, expected text. — done
5. Report classification, HTML/JSON reports, history. — done
6. Dashboard: preflight, checks, evidence, screenshots. — done
7. Regression tests and CI. — done (ruff, ESLint, backend and frontend tests, Docker build and smoke test)
8. Gemma 4 planning and explanation; API checks; jobs; audit logging; deployment gates. — done
9. Version 1.0:
   - Multi-step user flows; axe-core accessibility.
   - JSON field assertions, JSON Schema validation, request headers, OpenAPI/Swagger import.
   - Run comparison; history view/delete/clear; retention policy.
   - Persistent job records; background-job mode in the UI.
   - Per-user tokens and ownership of runs, jobs and screenshots.
   - Public mode that fails closed at startup and isolates every run in a worker process.
   - Single-container deployment (Dockerfile, compose, Render blueprint); Ollama adapter for local Gemma.

Known limits (by design for one instance): in-memory rate limiting and queue, SQLite storage. Scaling out would need a shared database, rate limiter and queue.
