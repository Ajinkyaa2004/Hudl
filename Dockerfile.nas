# PuckTrace backend for a small home NAS (Synology DS223: ARM64, 2 GB RAM).
# No headless browser: LIGHT_MODE reads every page with plain requests, which is what report
# searches use anyway. Built for amd64 and arm64 by .github/workflows/image.yml.
FROM python:3.11-slim

WORKDIR /app
ARG APP_VERSION=""
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC MALLOC_ARENA_MAX=2 LIGHT_MODE=1 MAX_SEARCHES=8 APP_VERSION=$APP_VERSION
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt tzdata

COPY backend ./backend
COPY frontend ./frontend
COPY data ./data-seed

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/healthz', timeout=8)"
CMD ["sh", "-c", "mkdir -p data && cp -r data-seed/. data/ && uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-keep-alive 30"]
