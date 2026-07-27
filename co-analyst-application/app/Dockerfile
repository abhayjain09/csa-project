FROM python:3.11-slim

# Non-interactive, no .pyc, unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# The API and one-off browser worker intentionally share one image so the
# existing ECR/deploy pipeline publishes an atomic backend+worker version.
# Playwright uses Debian's Chromium package; no browser binary is downloaded
# from the public internet during application startup.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (layer caching)
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy application code
COPY backend/ /app/backend/
COPY static/  /app/static/

# Static dir env so app.py serves the SPA
ENV STATIC_DIR=/app/static

EXPOSE 8080

WORKDIR /app/backend

# Gunicorn — single worker process, threaded, long timeout for AgentCore
# browser runs. MUST stay at 1 worker: _BULK_COMPANY_EXECUTOR in app.py is an
# in-memory ThreadPoolExecutor that caps concurrent company downloads at
# BULK_COMPANY_CONCURRENCY (default 3). Gunicorn workers are separate forked
# processes, each with its OWN copy of that executor — with >1 worker the cap
# is only enforced per-process (invisible across processes), so N companies
# triggered one-by-one can land on different workers and all run at once
# instead of being queued. The heavy AgentCore invoke already happens in a
# background thread via that executor (the HTTP request returns immediately
# after submitting), so one process + several threads is enough for HTTP
# responsiveness without needing more worker processes.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "900", \
     "--keep-alive", "5", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info", \
     "app:app"]
