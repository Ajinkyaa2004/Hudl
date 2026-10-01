# PuckTrace backend: FastAPI + headless Chromium (Playwright) + a persistent data folder.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt tzdata

COPY backend ./backend
COPY frontend ./frontend
# source configuration from the repo; the persistent disk is mounted at /app/data
COPY data ./data-seed

EXPOSE 8000
# refresh the repo's source configuration on the disk at every start, keep everything else there
CMD ["sh", "-c", "mkdir -p data && cp -r data-seed/. data/ && uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
