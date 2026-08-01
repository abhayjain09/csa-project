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
import json
import logging
import os
import re
import statistics
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


def _flatten_structure(structure: list) -> list[dict]:
    """Return every PageIndex node as a compact, ordered heading record."""
    flattened = []

    def visit(nodes, parents=()):
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip()
            if title:
                flattened.append({
                    "title": title[:500],
                    "path": " > ".join((*parents, title))[:1500],
                    "page_start": node.get("start_index"),
                    "page_end": node.get("end_index"),
                    "summary": str(node.get("summary") or "").strip()[:2500],
                })
            visit(node.get("nodes") or [], (*parents, title) if title else parents)

    visit(structure)
    return flattened


def _json_object(text: str) -> dict:
    """Parse one JSON object from a model response without accepting prose."""
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("coverage response is not a JSON object")
    return value


def _classify_annual_report_coverage(structure: list, report_classes: list[str]) -> dict:
    """Identify only dedicated, substantive sections in an Annual Report.

    PageIndex has already inspected the complete PDF and generated section
    summaries. This second pass classifies those sections; it must not infer
    coverage from a keyword-only heading or a passing mention.
    """
    headings = _flatten_structure(structure)
    requested = list(dict.fromkeys(
        str(value or "").strip().lower() for value in report_classes
        if str(value or "").strip()
    ))
    if not headings or not requested:
        return {"headings": headings, "coverage": {}}

    # Keep the model input bounded and topic-directed. The durable manifest
    # still records every heading, but classification only needs sections whose
    # titles or opening text overlap a requested class or a known close alias.
    aliases = {
        "code of conduct": ("conduct", "ethics", "business integrity"),
        "anti-bribery and corruption policy": ("bribery", "corruption", "anti-corruption"),
        "conflicts of interest policy": ("conflict of interest", "conflicts of interest"),
        "insider trading policy": ("insider trading", "securities trading", "share dealing"),
        "discrimination and harassment policy": ("discrimination", "harassment", "equal opportunity"),
        "supplier code of conduct": ("supplier conduct", "vendor conduct", "responsible sourcing"),
        "whistleblowing mechanism": ("whistle", "speak up", "reporting concerns", "ethics hotline"),
        "sustainability report": ("sustainability", "esg", "environmental social governance"),
        "ghg emission report": ("greenhouse gas", "ghg", "emissions", "scope 1", "scope 2", "scope 3"),
        "environmental policy": ("environmental policy", "environment policy"),
        "environment, health & safety policy": ("environment health safety", "ehs", "hse", "hsse"),
        "occupational health & safety policy": ("occupational health", "workplace safety", "health and safety"),
        "biodiversity policy": ("biodiversity", "nature", "ecosystem"),
        "impact report": ("impact", "social impact", "purpose"),
        "human rights policy": ("human rights",),
        "human rights due diligence": ("human rights due diligence", "human rights impact assessment"),
        "modern slavery statement": ("modern slavery", "human trafficking", "supply chains act"),
        "remuneration report": ("remuneration", "executive compensation", "director compensation"),
        "risk management policy": ("risk management", "enterprise risk", "risk governance"),
        "tax strategy and governance": ("tax strategy", "tax governance", "approach to tax"),
    }
    terms = list(dict.fromkeys(
        term.casefold()
        for report_class in requested
        for term in aliases.get(report_class, (report_class,))
    ))
    relevant = []
    for heading in headings:
        haystack = " ".join((
            heading.get("title", ""),
            heading.get("path", ""),
            heading.get("summary", "")[:2000],
        )).casefold()
        if any(term in haystack for term in terms):
            relevant.append({**heading, "summary": heading.get("summary", "")[:1200]})
    relevant = relevant[:300]
    if not relevant:
        return {"headings": headings, "coverage": {}}

    prompt = f"""You are auditing a company's Annual Report index.

Requested document classes:
{json.dumps(requested, ensure_ascii=False)}

Relevant entries selected from the complete heading index, with page ranges and
grounded section-opening text:
{json.dumps(relevant, ensure_ascii=False)}

For each requested class, decide whether this Annual Report contains a GENUINE,
DEDICATED, SUBSTANTIVE section that can serve as a last-tier reference when no
standalone document exists. A keyword in a heading, risk factor, footnote,
cross-reference, short compliance statement, or passing mention is not enough.
The heading plus summary must show sustained policies, governance, procedures,
commitments, controls, or report disclosures for the requested class.

Be especially strict about near-neighbours: a supplier code is not an employee
code of conduct; risk factors are not a risk-management policy; a general ESG
overview is not automatically a sustainability report; a tax footnote is not a
tax strategy; and mentioning bribery/conflicts/insider trading is not a policy.

Return JSON only in this exact shape:
{{"coverage": {{
  "<exact requested class>": {{
    "match": "substantive_section",
    "heading": "<exact heading>",
    "page_start": <integer>,
    "page_end": <integer>,
    "confidence": "high",
    "evidence": "<concise explanation grounded in the heading summary>"
  }}
}}}}

Omit a class unless confidence is high. Never return medium/low matches and
never invent a heading or page number not present in the supplied index."""
    response = litellm.completion(
        model=MODEL.removeprefix("litellm/"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=8000,
        timeout=180,
    )
    parsed = _json_object(response.choices[0].message.content)
    raw_coverage = parsed.get("coverage") or {}
    by_title = {
        (item["title"].casefold(), item.get("page_start"), item.get("page_end")): item
        for item in relevant
    }
    coverage = {}
    for report_class, match in raw_coverage.items():
        canonical = str(report_class or "").strip().lower()
        if canonical not in requested or not isinstance(match, dict):
            continue
        if match.get("match") != "substantive_section" or match.get("confidence") != "high":
            continue
        try:
            page_start = int(match.get("page_start"))
            page_end = int(match.get("page_end"))
        except (TypeError, ValueError):
            continue
        heading = str(match.get("heading") or "").strip()
        source = by_title.get((heading.casefold(), page_start, page_end))
        if source is None or page_start < 1 or page_end < page_start:
            continue
        coverage[canonical] = {
            "match": "substantive_section",
            "heading": source["title"],
            "page_start": page_start,
            "page_end": page_end,
            "confidence": "high",
            "evidence": str(match.get("evidence") or "").strip()[:1000],
        }
    return {"headings": headings, "coverage": coverage}


def _extract_full_pdf_heading_index(pdf_buf: io.BytesIO) -> list[dict]:
    """Inspect every page and build a heading/content index without relying on
    the printed TOC. Embedded bookmarks are included, while layout-based
    headings cover reports whose TOC omits lower-level policy sections. When a
    page has no extractable text, PyMuPDF OCR is attempted if Tesseract data is
    available in the runtime; otherwise the page remains empty and downstream
    classification fails closed.
    """
    import fitz

    pdf_buf.seek(0)
    doc = fitz.open(stream=pdf_buf.read(), filetype="pdf")
    page_texts = []
    page_lines = []
    font_observations = []
    for page_number, page in enumerate(doc, start=1):
        text_page = None
        page_dict = page.get_text("dict")
        plain = page.get_text("text").strip()
        if len(plain) < 40:
            try:
                text_page = page.get_textpage_ocr(full=True)
                page_dict = page.get_text("dict", textpage=text_page)
                plain = page.get_text("text", textpage=text_page).strip()
                log.info("[annual-coverage] OCR used for page %d", page_number)
            except Exception:
                pass
        page_texts.append(plain)
        lines = []
        for block in page_dict.get("blocks") or []:
            for line in block.get("lines") or []:
                spans = line.get("spans") or []
                text = " ".join(
                    str(span.get("text") or "").strip() for span in spans
                    if str(span.get("text") or "").strip()
                ).strip()
                if not text:
                    continue
                sizes = [float(span.get("size") or 0) for span in spans]
                flags = [int(span.get("flags") or 0) for span in spans]
                max_size = max(sizes or [0])
                chars = max(1, len(text))
                font_observations.extend(
                    float(span.get("size") or 0)
                    for span in spans
                    for _ in range(min(40, max(1, len(str(span.get("text") or "")))))
                    if float(span.get("size") or 0) > 0
                )
                lines.append({
                    "text": re.sub(r"\s+", " ", text),
                    "size": max_size,
                    "bold": any(flag & 16 for flag in flags),
                    "chars": chars,
                })
        page_lines.append(lines)

    body_size = statistics.median(font_observations) if font_observations else 10.0
    topic_marker = re.compile(
        r"\b(?:conduct|ethic|briber|corrupt|conflict|insider|trading|harass|"
        r"discrimin|whistle|speak.?up|sustainab|esg|greenhouse|emission|"
        r"environment|health|safety|biodivers|nature|human rights|slavery|"
        r"remuneration|compensation|risk management|tax strategy|governance)\b",
        re.I,
    )
    candidates = []
    for page_number, lines in enumerate(page_lines, start=1):
        for line in lines:
            title = line["text"].strip(" •\t")
            words = re.findall(r"[A-Za-z][A-Za-z&'/-]*", title)
            if not (2 <= len(words) <= 22 and 4 <= len(title) <= 180):
                continue
            looks_styled = (
                line["size"] >= body_size + 1.2
                or (line["bold"] and line["size"] >= body_size - 0.2)
                or title.isupper()
            )
            if not looks_styled and not topic_marker.search(title):
                continue
            if title.endswith((".", ";")) and not topic_marker.search(title):
                continue
            candidates.append({
                "title": title,
                "page_start": page_number,
                "source": "layout_heading",
            })

    # Embedded outline entries are higher-confidence headings and often carry
    # lower-level sections that visual font heuristics miss.
    try:
        for level, title, page_number, *_ in doc.get_toc(simple=True):
            if title and page_number and page_number > 0:
                candidates.append({
                    "title": re.sub(r"\s+", " ", str(title)).strip()[:500],
                    "page_start": int(page_number),
                    "source": "pdf_bookmark",
                    "level": int(level),
                })
    except Exception:
        pass

    # De-duplicate layout/bookmark overlap, preferring bookmarks.
    deduped = {}
    for item in sorted(
            candidates,
            key=lambda value: (value["page_start"],
                               0 if value["source"] == "pdf_bookmark" else 1)):
        key = (item["title"].casefold(), item["page_start"])
        deduped.setdefault(key, item)
    ordered = sorted(deduped.values(), key=lambda value: value["page_start"])

    headings = []
    for index, item in enumerate(ordered[:2000]):
        next_page = (
            ordered[index + 1]["page_start"]
            if index + 1 < len(ordered) else len(page_texts) + 1)
        page_end = max(item["page_start"], next_page - 1)
        # A compact excerpt from the section's opening pages gives the model
        # substance to distinguish a real policy section from a TOC keyword.
        excerpt_parts = page_texts[
            item["page_start"] - 1:min(page_end, item["page_start"] + 3)
        ]
        headings.append({
            "title": item["title"],
            "path": item["title"],
            "page_start": item["page_start"],
            "page_end": page_end,
            "summary": re.sub(r"\s+", " ", "\n".join(excerpt_parts))[:4000],
            "source": item["source"],
        })
    doc.close()
    return headings


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
    mode   = payload.get("mode", "index")

    if not bucket or not s3_key:
        return {"status": "error", "error": "Missing required fields: 'bucket' and 's3_key'"}

    log.info("[handler] bucket=%r s3_key=%r label=%r mode=%r", bucket, s3_key, label, mode)

    # Complete-document Annual Report coverage mode. This is intentionally a
    # separate lightweight process from ordinary PageIndex generation: it
    # scans layout headings/bookmarks on every page, attempts OCR for image-only
    # pages, and sends grounded section excerpts through a strict classifier.
    if mode == "annual_report_coverage":
        pdf_buf = None
        try:
            pdf_buf = _stream_pdf(bucket, s3_key)
            heading_index = _extract_full_pdf_heading_index(pdf_buf)
            classified = _classify_annual_report_coverage(
                heading_index,
                payload.get("report_classes") or [],
            )
            heading_notes = [{
                "title": item.get("title"),
                "page_start": item.get("page_start"),
                "page_end": item.get("page_end"),
                "source": item.get("source"),
            } for item in heading_index]
            return {
                "status": "ok",
                "doc_name": label,
                "headings": heading_notes,
                "coverage": classified["coverage"],
            }
        except Exception as exc:
            log.exception("[handler] annual-report coverage failed for %s", label)
            return {
                "status": "error",
                "error": "annual_report_coverage_error: {}".format(exc),
            }
        finally:
            if pdf_buf is not None:
                pdf_buf.close()

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

    if mode != "index":
        return {"status": "error", "error": f"Unsupported mode: {mode}"}

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
