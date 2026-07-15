# AgentCore Runtime is ARM64/Graviton ONLY. Build with: docker buildx build --platform linux/arm64 ...
FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# ca-certificates for HTTPS calls (Bedrock, Gateway MCP, S3, DynamoDB, and the
# CDP WebSocket to AgentCore Browser all go over TLS).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# NOTE: no `playwright install` / `install-deps` step. We only ever call
# chromium.connect_over_cdp(...) against AWS's REMOTE managed AgentCore
# Browser — no local browser is ever launched in this container, so no
# browser binary and no OS-level browser dependencies are needed here.


COPY agent.py report_specs.py registry_tier.py ./

# BedrockAgentCoreApp serves /invocations and /ping on 8080.
EXPOSE 8080
CMD ["python", "agent.py"]