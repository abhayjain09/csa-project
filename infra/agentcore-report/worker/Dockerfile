FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY worker/requirements.txt .
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium

# Reuse the production validator and storage logic. The worker never stores a
# browser result unless it passes the same gates as the AgentCore runtime.
COPY agent/agent.py ./agent.py
COPY agent/config ./config
COPY worker/worker.py ./worker.py

CMD ["python", "worker.py"]
