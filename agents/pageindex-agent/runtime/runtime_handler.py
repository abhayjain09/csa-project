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
litellm.success_callback = []
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
def _build_opt(page_count: int = 0):
    """
    Return config tuned to document size.

    <=150 pages  — default settings, summaries on
    151-250 pages — larger node limits, summaries on
    >250 pages   — largest node limits, summaries off
                   (too many LLM calls risk hitting the 840s AgentCore timeout)
    """
    if page_count > 250:
        return ConfigLoader().load({
            "model":                   MODEL,
            "max_page_num_each_node":  7,
            "max_token_num_each_node": 20000,
            "if_add_node_summary":     "yes",
        })
    elif page_count > 150:
        return ConfigLoader().load({
            "model":                   MODEL,
            "max_page_num_each_node":  7,
            "max_token_num_each_node": 20000,
            "if_add_node_summary":     "yes",
        })
    else:
        return ConfigLoader().load({
            "model":                   MODEL,
            "max_page_num_each_node":  7,
            "max_token_num_each_node": 20000,
            "if_add_node_summary":     "yes",
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
    mode   = payload.get("mode", "index")   # "index" (default) or "extract_toc"

    if not bucket or not s3_key:
        return {"status": "error", "error": "Missing required fields: 'bucket' and 's3_key'"}

    log.info("[handler] bucket=%r s3_key=%r label=%r mode=%r", bucket, s3_key, label, mode)

    # TOC extraction mode — stream first 20 pages and ask Claude for TOC only.
    # Used by app.py for large PDFs that have no embedded PDF bookmarks.
    if mode == "extract_toc":
        try:
            pdf_buf = _stream_pdf(bucket, s3_key)
            import PyPDF2
            reader     = PyPDF2.PdfReader(pdf_buf)
            page_count = len(reader.pages)
            # Send first 20 pages to Claude for TOC extraction
            from pageindex.utils import get_page_tokens, ConfigLoader
            pdf_buf.seek(0)
            page_list  = get_page_tokens(pdf_buf, model=MODEL)
            toc_pages  = page_list[:20]
            opt        = ConfigLoader().load({"model": MODEL})
            import asyncio
            from pageindex.page_index import check_toc
            toc_result = asyncio.run(check_toc(toc_pages, opt))
            # check_toc returns toc_content (raw text) not a parsed list.
            # Parse into {"title": ..., "page": ...} format for _split_toc_into_chunks()
            toc_items   = []
            toc_content = toc_result.get("toc_content")
            if toc_content and toc_result.get("page_index_given_in_toc") == "yes":
                from pageindex.page_index import toc_transformer
                import asyncio as _asyncio
                parsed = _asyncio.run(toc_transformer(toc_content, MODEL))
                toc_items = [
                    {
                        "title": item.get("title", ""),
                        "page":  item.get("page_number") or item.get("physical_index") or 0
                    }
                    for item in (parsed or [])
                    if item.get("title")
                ]
                log.info("[extract_toc] parsed %d TOC items", len(toc_items))
            return {
                "status": "ok",
                "toc":    toc_items,
                "pages":  page_count,
            }
        except Exception as exc:
            log.exception("[handler] extract_toc failed for %s", s3_key)
            return {"status": "ok", "toc": [], "pages": 0}

    # Chunked path parameters — supplied by app.py for large PDFs
    page_start = payload.get("page_start")
    page_end   = payload.get("page_end")
    toc        = payload.get("toc")   # pre-extracted TOC items list

    # 1. Stream PDF from S3
    try:
        pdf_buf = _stream_pdf(bucket, s3_key)
        pdf_buf.name = label   # used by get_pdf_name() to set doc_name
    except ClientError as exc:
        log.error("[handler] S3 stream failed: %s", exc)
        return {"status": "error", "error": "s3_stream_error: {}".format(exc)}

    # 2. Run PageIndex
    # page_index_main calls asyncio.run() internally. Running it directly here
    # would conflict with uvicorn's already-running event loop and cause the
    # "coroutine was never awaited" warning. Submitting to a ThreadPoolExecutor
    # gives it a fresh thread with no running loop so asyncio.run() works cleanly.
    # os.chdir is process-wide, not thread-local. Two concurrent chunk handlers
    # calling os.chdir simultaneously corrupt each other's working directory.
    # page_index_main only needs /tmp to exist for log/result writes — it uses
    # os.makedirs internally. Keep CWD unchanged; /tmp is always present.
    try:
        import PyPDF2
        # PyMuPDF primary for page count — more reliable for complex PDFs.
        # PyPDF2 fallback if PyMuPDF fails.
        try:
            import fitz
            pdf_buf.seek(0)
            fitz_doc   = fitz.open(stream=pdf_buf.read(), filetype="pdf")
            page_count = fitz_doc.page_count
            fitz_doc.close()
            pdf_buf.seek(0)
            log.info("[pageindex] PyMuPDF page_count=%d for %s", page_count, label)
        except Exception as fitz_err:
            log.warning("[pageindex] PyMuPDF failed (%s) — falling back to PyPDF2", fitz_err)
            pdf_buf.seek(0)
            try:
                full_reader = PyPDF2.PdfReader(pdf_buf)
                page_count  = len(full_reader.pages)
                if page_count == 0:
                    raise ValueError("PyPDF2 returned 0 pages")
                pdf_buf.seek(0)
                log.info("[pageindex] PyPDF2 page_count=%d for %s", page_count, label)
            except Exception as pypdf2_err:
                log.warning("[pageindex] both PyMuPDF and PyPDF2 failed (%s) — assuming 300",
                            pypdf2_err)
                page_count = 300
                pdf_buf.seek(0)

        # If a page range was supplied extract only those pages into
        # a new BytesIO buffer so page_index_main sees a smaller PDF.
        if page_start and page_end:
            try:
                pdf_buf.seek(0)
                full_reader = PyPDF2.PdfReader(pdf_buf)
                from pypdf import PdfWriter
                writer = PdfWriter()
                for p in range(page_start - 1, min(page_end, page_count)):
                    writer.add_page(full_reader.pages[p])
                chunk_buf      = io.BytesIO()
                writer.write(chunk_buf)
                chunk_buf.seek(0)
                chunk_buf.name = label
                pdf_buf.close()
                pdf_buf    = chunk_buf
                page_count = page_end - page_start + 1
                log.info("[pageindex] chunk pages %d-%d (%d pages)",
                         page_start, page_end, page_count)
            except Exception as chunk_err:
                log.error("[pageindex] chunk extraction failed: %s", chunk_err)
                raise
        else:
            pdf_buf.seek(0)

        log.info("[pageindex] page_count=%d label=%s", page_count, label)
        opt = _build_opt(page_count)

        # When processing a page-range chunk, suffix the label with the range so
        # JsonLogger writes distinct filenames for concurrent chunks of the same doc.
        # Without this, all chunks of "report.pdf" write to the same log path and
        # can clobber each other under /tmp/logs/.
        chunk_label = label
        if page_start and page_end:
            chunk_label = f"{label}_p{page_start}-{page_end}"
        pdf_buf.name = chunk_label

        # If pre-extracted TOC was supplied inject it into opt so
        # page_index_main can skip Phases 1 and 2 entirely.
        if toc:
            opt.pre_extracted_toc = toc
            log.info("[pageindex] using pre-extracted TOC (%d items)", len(toc))

        log.info("[pageindex] indexing %s ...", label)
        future = _executor.submit(page_index_main, pdf_buf, opt)
        result = future.result(timeout=840)  # 14 min, under the 15 min read_timeout
        log.info("[pageindex] done — doc_name=%r", result.get("doc_name"))
    except Exception as exc:
        log.exception("[handler] pageindex failed for %s", label)
        return {"status": "error", "error": "pageindex_error: {}".format(exc)}
    finally:
        pdf_buf.close()

    return {"status": "ok", "index": result}

# app.run() is called from runtime_entrypoint.py — not here.
# This keeps the handler importable and testable in isolation.




