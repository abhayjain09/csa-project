"""
runtime_handler.py — AgentCore Runtime handler for PageIndex.

Uses the bedrock-agentcore SDK to expose the handler over HTTP on
port 8080 as required by the AgentCore contract.

Expected invocation payload:
    {
        "bucket": "my-bucket",          # S3 bucket
        "s3_key": "paccar/report.pdf",  # S3 key of the PDF to index
        "label":  "report"              # optional; defaults to stem of s3_key
    }

Returns:
    {
        "status": "ok" | "error",
        "index":  <page index dict>,    # present on success
        "error":  <message>             # present on failure
    }
"""

import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# PageIndex writes logs/results relative to cwd — use /tmp which is always
# writable by any user including the non-root agent user in the container
os.chdir("/tmp")
os.makedirs("/tmp/logs", exist_ok=True)
os.makedirs("/tmp/results", exist_ok=True)

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# ---------------------------------------------------------------------------
# PageIndex repo is cloned into pageindex-lib/PageIndex-main/ and copied to
# /app/ in the container. Package is at /app/pageindex/__init__.py
# /app is already in PYTHONPATH via ENV so no sys.path manipulation needed.
# ---------------------------------------------------------------------------
import litellm
from pageindex import page_index_main
from pageindex.utils import ConfigLoader

# Increase output token limit for Claude Sonnet via extended output beta.
# Set globally so all LiteLLM calls from PageIndex pick it up automatically.
litellm.extra_headers = {"anthropic-beta": "output-128k-2025-02-19"}
litellm.max_tokens = 64000
litellm.success_callback = []   # ← add these two
litellm.failure_callback = [] 

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL  = os.environ.get(
    "PAGEINDEX_MODEL",
    "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("pageindex-runtime")

# ---------------------------------------------------------------------------
# BedrockAgentCoreApp — wires /ping and /invocations on port 8080
# ---------------------------------------------------------------------------
app = BedrockAgentCoreApp()
_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# S3 helper
# ---------------------------------------------------------------------------
def _get_s3():
    return boto3.client(
        "s3",
        region_name=REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def _stream_pdf(bucket: str, s3_key: str) -> io.BytesIO:
    """Stream a PDF from S3 into memory — no local file written."""
    s3 = _get_s3()
    log.info("[s3] streaming s3://%s/%s", bucket, s3_key)
    buf = io.BytesIO()
    s3.download_fileobj(bucket, s3_key, buf)
    buf.seek(0)
    log.info("[s3] streamed %d bytes", buf.getbuffer().nbytes)
    return buf


# ---------------------------------------------------------------------------
# PageIndex helper
# ---------------------------------------------------------------------------
def _build_opt():
    return ConfigLoader().load({
        "model":                   MODEL,
        "max_page_num_each_node":  5,     # only split nodes larger than 5 pages
        "max_token_num_each_node": 8000,  # only split nodes larger than 8000 tokens
    })


# ---------------------------------------------------------------------------
# Invocation handler — @app.entrypoint is the correct decorator
# ---------------------------------------------------------------------------
@app.entrypoint
def handler(payload: dict) -> dict:
    """
    AgentCore invocation handler.

    Parameters
    ----------
    payload : dict
        {
            "bucket": str,   # S3 bucket name
            "s3_key": str,   # S3 object key for the PDF
            "label":  str    # optional; defaults to stem of s3_key
        }

    Returns
    -------
    dict
        {
            "status": "ok" | "error",
            "index":  <page index dict>,   # present on success
            "error":  <message>            # present on failure
        }
    """
    bucket = payload.get("bucket")
    s3_key = payload.get("s3_key")
    label  = payload.get("label") or Path(s3_key or "unknown").stem

    if not bucket or not s3_key:
        return {"status": "error", "error": "Missing required fields: 'bucket' and 's3_key'"}

    log.info("[handler] bucket=%r s3_key=%r label=%r", bucket, s3_key, label)

    # 1. Stream PDF from S3
    try:
        pdf_buf = _stream_pdf(bucket, s3_key)
    except ClientError as exc:
        log.error("[handler] S3 stream failed: %s", exc)
        return {"status": "error", "error": "s3_stream_error: {}".format(exc)}

    # 2. Run PageIndex
    # page_index_main calls asyncio.run() internally. Running it directly here
    # would conflict with uvicorn's already-running event loop and cause the
    # "coroutine was never awaited" warning. Submitting to a ThreadPoolExecutor
    # gives it a fresh thread with no running loop so asyncio.run() works cleanly.
    original_cwd = os.getcwd()
    os.chdir("/tmp")
    try:
        opt = _build_opt()
        log.info("[pageindex] indexing %s ...", label)
        future = _executor.submit(page_index_main, pdf_buf, opt)
        result = future.result(timeout=840)  # 14 min, under the 15 min read_timeout
        log.info("[pageindex] done — doc_name=%r", result.get("doc_name"))
    except Exception as exc:
        log.exception("[handler] pageindex failed for %s", label)
        return {"status": "error", "error": "pageindex_error: {}".format(exc)}
    finally:
        pdf_buf.close()
        os.chdir(original_cwd)

    return {"status": "ok", "index": result}

# app.run() is called from runtime_entrypoint.py — not here.
# This keeps the handler importable and testable in isolation.


