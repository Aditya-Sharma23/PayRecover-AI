# syntax=docker/dockerfile:1
# One image: FastAPI serves the API and the built dashboard from the same origin.

FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TESTPILOT_DB_PATH=/data/testpilot.db \
    TESTPILOT_AUDIT_LOG_PATH=/data/audit.log \
    TESTPILOT_FRONTEND_DIST=/app/frontend/dist
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY backend/app ./app
COPY --from=frontend /build/dist /app/frontend/dist
# Run as an unprivileged user; /data holds the SQLite database, audit log and screenshots.
RUN useradd --create-home --uid 10001 testpilot \
    && mkdir -p /data \
    && chown testpilot:testpilot /data
USER testpilot
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=4)"
# PORT is set by most hosting platforms. Trust X-Forwarded-* only from the
# addresses in FORWARDED_ALLOW_IPS (the platform's proxy).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1} --no-server-header"]
