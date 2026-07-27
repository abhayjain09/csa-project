# syntax=docker/dockerfile:1.7
#
# AgentCore Runtime container for the PageIndex ReAct agent.
#
# CRITICAL: AgentCore Runtime is ARM64-only (AWS Graviton).
# Always build with --platform linux/arm64.

FROM --platform=linux/arm64 python:3.12-slim

# Non-root user — defense-in-depth
RUN groupadd -r agent && useradd -r -g agent -d /app -s /sbin/nologin agent

WORKDIR /app

# Install deps before copying app code so this layer caches on dep-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY --chown=agent:agent . .

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# AgentCore contract: port 8080
EXPOSE 8080

USER agent

# The bedrock-agentcore SDK's app.run() starts uvicorn on 0.0.0.0:8080
CMD ["python", "runtime_entrypoint.py"]

