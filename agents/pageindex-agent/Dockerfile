# syntax=docker/dockerfile:1.7
#
# AgentCore Runtime container for PageIndex.
#
# CRITICAL: AgentCore Runtime is ARM64-only (AWS Graviton).
# Always build with --platform linux/arm64.
#
# Repo layout expected at build time (run docker build from pageindex-agentcore/):
#
#   pageindex-agentcore/
#   ├── pageindex-lib/            ← git clone https://github.com/VectifyAI/PageIndex here
#   │   ├── pageindex/            ← the actual Python package
#   │   │   ├── __init__.py
#   │   │   └── ...
#   │   └── run_pageindex.py
#   ├── runtime/
#   │   ├── requirements.txt
#   │   ├── runtime_handler.py
#   │   └── runtime_entrypoint.py
#   └── Dockerfile

FROM --platform=linux/arm64 python:3.12-slim

# Non-root user — defense-in-depth
RUN groupadd -r agent && useradd -r -g agent -d /app -s /sbin/nologin agent

WORKDIR /app

# Install deps before copying app code so this layer caches on dep-only changes
COPY runtime/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# PageIndex library
# Copy contents of pageindex-lib/ directly into /app/ so the package is at
# /app/pageindex/__init__.py — importable as "from pageindex import ..."
# since PYTHONPATH=/app
# ---------------------------------------------------------------------------
COPY --chown=agent:agent pageindex-lib/PageIndex-main/ /app/

# Runtime handler and entrypoint
COPY --chown=agent:agent runtime/runtime_handler.py /app/runtime_handler.py
COPY --chown=agent:agent runtime/runtime_entrypoint.py /app/runtime_entrypoint.py

# Create writable dirs for PageIndex logs and results before switching to non-root user
RUN mkdir -p /app/logs /app/results && chown -R agent:agent /app/logs /app/results

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PAGEINDEX_MODEL=bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
    AWS_REGION=us-east-1

# AgentCore contract: port 8080
EXPOSE 8080

USER agent

# The bedrock-agentcore SDK's app.run() starts uvicorn on 0.0.0.0:8080
CMD ["python", "runtime_entrypoint.py"]

