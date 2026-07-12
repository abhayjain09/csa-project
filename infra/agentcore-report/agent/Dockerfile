# AgentCore Runtime is ARM64/Graviton ONLY. Build with: docker buildx build --platform linux/arm64 ...
FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# ca-certificates for HTTPS calls to registries, company sites, Bedrock, S3, and
# DynamoDB.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Playwright connects to the remote AgentCore Browser over CDP. No local Chromium
# binary is installed in this runtime image.
COPY agent.py .
COPY config ./config

# BedrockAgentCoreApp serves /invocations and /ping on 8080.
EXPOSE 8080
CMD ["python", "agent.py"]
