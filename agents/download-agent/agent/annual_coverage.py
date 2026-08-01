"""Annual Report section coverage for the Download Agent runtime.

This module is intentionally independent of the PageIndex agent. It reads the
already stored Annual Report, extracts bookmarks, grounded printed-TOC entries,
and topic-bearing headings with pypdf, then uses the Download Agent's model
callback for strict high-confidence classification.
"""

from __future__ import annotations

import json
import os
import re
from io import BytesIO
from typing import Callable

from pypdf import PdfReader


ALIASES = {
    "code of conduct": ("conduct", "ethics", "business integrity"),
    "anti-bribery and corruption policy": (
        "bribery", "corruption", "anti-corruption"),
    "conflicts of interest policy": (
        "conflict of interest", "conflicts of interest"),
    "insider trading policy": (
        "insider trading", "securities trading", "share dealing"),
    "discrimination and harassment policy": (
        "discrimination", "harassment", "equal opportunity"),
    "supplier code of conduct": (
        "supplier conduct", "vendor conduct", "responsible sourcing"),
    "whistleblowing mechanism": (
        "whistle", "speak up", "reporting concerns", "ethics hotline"),
    "sustainability report": (
        "sustainability", "esg", "environmental social governance"),
    "ghg emission report": (
        "greenhouse gas", "ghg", "emissions", "scope 1", "scope 2", "scope 3"),
    "environmental policy": ("environmental policy", "environment policy"),
    "environment, health & safety policy": (
        "environment health safety", "ehs", "hse", "hsse"),
    "occupational health & safety policy": (
        "occupational health", "workplace safety", "health and safety"),
    "biodiversity policy": ("biodiversity", "nature", "ecosystem"),
    "impact report": ("impact", "social impact", "purpose"),
    "human rights policy": ("human rights",),
    "human rights due diligence": (
        "human rights due diligence", "human rights impact assessment"),
    "modern slavery statement": (
        "modern slavery", "human trafficking", "supply chains act"),
    "remuneration report": (
        "remuneration", "executive compensation", "director compensation"),
    "risk management policy": (
        "risk management", "enterprise risk", "risk governance"),
    "tax strategy and governance": (
        "tax strategy", "tax governance", "approach to tax"),
}


def _clean_heading(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n•·.-")


def _parse_json_object(text: str) -> dict:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("coverage model returned no JSON object")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("coverage model response is not an object")
    return value


def _outline_headings(reader: PdfReader) -> list[dict]:
    headings = []

    def visit(items):
        for item in items or []:
            if isinstance(item, list):
                visit(item)
                continue
            title = _clean_heading(getattr(item, "title", ""))
            if not title:
                continue
            try:
                page_start = reader.get_destination_page_number(item) + 1
            except Exception:
                continue
            if page_start > 0:
                headings.append({
                    "title": title[:500],
                    "page_start": page_start,
                    "source": "pdf_bookmark",
                })

    try:
        visit(reader.outline)
    except Exception:
        pass
    return headings


def _locate_toc_title(title: str, page_texts: list[str],
                      toc_page: int) -> int | None:
    """Ground a printed-TOC title on an actual later physical PDF page."""
    normalized = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    if len(normalized) < 4:
        return None
    words = normalized.split()
    for page_number in range(max(1, toc_page + 1), len(page_texts) + 1):
        for line in page_texts[page_number - 1].splitlines():
            candidate = re.sub(
                r"[^a-z0-9]+", " ", line.casefold()).strip()
            if (normalized == candidate
                    or (len(words) >= 3 and normalized in candidate
                        and len(candidate) <= len(normalized) + 80)):
                return page_number
    return None


def extract_heading_index(body: bytes) -> list[dict]:
    """Read every text page and construct a conservative heading index.

    Printed page numbers are never trusted directly. A printed-TOC title must
    be found again on a later physical page. Image-only PDFs produce no
    references because this runtime intentionally has no OCR dependency.
    """
    reader = PdfReader(BytesIO(body), strict=False)
    page_texts = []
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
        page_texts.append(text)

    candidates = _outline_headings(reader)
    topic_terms = list(dict.fromkeys(
        term.casefold() for aliases in ALIASES.values() for term in aliases
    ))

    toc_row = re.compile(
        r"^\s*(.{4,180}?)\s*(?:\.{2,}|\s{3,})\s*(\d{1,4})\s*$")
    for toc_page, page_text in enumerate(page_texts[:40], start=1):
        for raw_line in page_text.splitlines():
            match = toc_row.match(raw_line)
            if not match:
                continue
            title = _clean_heading(match.group(1))
            if not (2 <= len(title.split()) <= 24):
                continue
            actual_page = _locate_toc_title(title, page_texts, toc_page)
            if actual_page:
                candidates.append({
                    "title": title[:500],
                    "page_start": actual_page,
                    "source": "printed_toc",
                })

    # Reports without bookmarks/TOC still get conservative topic headings.
    # This does not prove coverage; classification also requires substantive
    # section-opening text and exact heading/page validation.
    for page_number, page_text in enumerate(page_texts, start=1):
        for raw_line in page_text.splitlines():
            title = _clean_heading(raw_line)
            words = re.findall(r"[A-Za-z][A-Za-z&'/-]*", title)
            if not (2 <= len(words) <= 20 and 4 <= len(title) <= 180):
                continue
            folded = title.casefold()
            if not any(term in folded for term in topic_terms):
                continue
            if title.endswith((".", ";", ":")) and len(words) > 12:
                continue
            candidates.append({
                "title": title[:500],
                "page_start": page_number,
                "source": "topic_heading",
            })

    source_rank = {"pdf_bookmark": 0, "printed_toc": 1, "topic_heading": 2}
    deduped = {}
    for item in sorted(
            candidates,
            key=lambda value: (
                int(value["page_start"]), source_rank.get(value["source"], 9))):
        key = (item["title"].casefold(), int(item["page_start"]))
        deduped.setdefault(key, item)
    ordered = sorted(
        deduped.values(), key=lambda value: int(value["page_start"]))[:2000]

    headings = []
    for index, item in enumerate(ordered):
        next_page = (
            int(ordered[index + 1]["page_start"])
            if index + 1 < len(ordered) else len(page_texts) + 1)
        page_start = int(item["page_start"])
        page_end = max(page_start, next_page - 1)
        opening = page_texts[
            page_start - 1:min(page_end, page_start + 2)
        ]
        headings.append({
            "title": item["title"],
            "path": item["title"],
            "page_start": page_start,
            "page_end": page_end,
            "summary": re.sub(r"\s+", " ", "\n".join(opening))[:3500],
            "source": item["source"],
        })
    return headings


def classify_coverage(headings: list[dict], report_classes: list[str],
                      converse: Callable[[str, int], str]) -> dict:
    """Return only high-confidence matches grounded to exact index entries."""
    requested = list(dict.fromkeys(
        str(value or "").strip().lower() for value in report_classes
        if str(value or "").strip()
    ))
    if not headings or not requested:
        return {"headings": headings, "coverage": {}}

    terms_by_class = {
        report_class: tuple(
            term.casefold() for term in ALIASES.get(
                report_class, (report_class,)))
        for report_class in requested
    }
    relevant = []
    for heading in headings:
        haystack = " ".join((
            str(heading.get("title") or ""),
            str(heading.get("path") or ""),
            str(heading.get("summary") or "")[:1600],
        )).casefold()
        if any(
                term in haystack
                for terms in terms_by_class.values() for term in terms):
            relevant.append({
                **heading,
                "summary": str(heading.get("summary") or "")[:900],
            })
    relevant = relevant[:100]
    if not relevant:
        return {"headings": headings, "coverage": {}}

    prompt = f"""You are auditing a company's Annual Report heading index.

Requested standalone document classes that were not found:
{json.dumps(requested, ensure_ascii=False)}

Grounded headings, physical PDF page ranges, and section-opening text:
{json.dumps(relevant, ensure_ascii=False)}

For each requested class, identify a GENUINE, DEDICATED, SUBSTANTIVE section
that can serve only as a final-tier reference. A keyword, risk factor, footnote,
cross-reference, short compliance statement, or passing mention is not enough.
The heading and opening text must show sustained policies, governance,
procedures, commitments, controls, or report disclosures for that exact class.

Be strict about near-neighbours: supplier conduct is not employee conduct; risk
factors are not a risk-management policy; general ESG text is not automatically
a sustainability report; a tax footnote is not a tax strategy; and mentioning
bribery, conflicts, or insider trading is not a dedicated policy section.

Return JSON only:
{{"coverage": {{
  "<exact requested class>": {{
    "match": "substantive_section",
    "heading": "<exact supplied heading>",
    "page_start": <exact supplied integer>,
    "page_end": <exact supplied integer>,
    "confidence": "high",
    "evidence": "<concise explanation grounded in supplied opening text>"
  }}
}}}}

Omit every class that is not high confidence. Never invent or adjust a heading
or page range."""
    parsed = _parse_json_object(converse(prompt, 4000))
    raw_coverage = parsed.get("coverage") or {}
    indexed = {
        (str(item.get("title") or "").casefold(),
         item.get("page_start"), item.get("page_end")): item
        for item in relevant
    }
    coverage = {}
    for report_class, match in raw_coverage.items():
        canonical = str(report_class or "").strip().lower()
        if canonical not in requested or not isinstance(match, dict):
            continue
        if (match.get("match") != "substantive_section"
                or match.get("confidence") != "high"):
            continue
        try:
            page_start = int(match.get("page_start"))
            page_end = int(match.get("page_end"))
        except (TypeError, ValueError):
            continue
        heading = str(match.get("heading") or "").strip()
        source = indexed.get((heading.casefold(), page_start, page_end))
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


def run(payload: dict, *, configured_bucket: str, s3_client,
        converse: Callable[[str, int], str],
        integrity_error: Callable[[str, str, bytes], str]) -> dict:
    """Execute coverage mode without entering normal download-agent cleanup."""
    bucket = str((payload or {}).get("bucket") or configured_bucket)
    s3_key = str((payload or {}).get("s3_key") or "").strip()
    requested = (payload or {}).get("report_classes") or []
    if not configured_bucket or bucket != configured_bucket:
        return {"status": "error", "error": "coverage bucket is not allowed"}
    if not s3_key or not isinstance(requested, list):
        return {
            "status": "error",
            "error": "annual_report_coverage requires s3_key and report_classes",
        }
    try:
        head = s3_client.head_object(Bucket=bucket, Key=s3_key)
        size = int(head.get("ContentLength") or 0)
        max_bytes = int(os.environ.get(
            "ANNUAL_COVERAGE_MAX_PDF_BYTES", str(100 * 1024 * 1024)))
        if size <= 0 or size > max_bytes:
            raise ValueError(
                f"annual report size {size} is outside allowed range")
        body = s3_client.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
        invalid = integrity_error(s3_key, "application/pdf", body)
        if invalid:
            raise ValueError(invalid)
        headings = extract_heading_index(body)
        classified = classify_coverage(headings, requested, converse)
        return {
            "status": "ok",
            "doc_name": s3_key.rsplit("/", 1)[-1],
            "extractor": "download-agent-annual-coverage-v1",
            "headings": [{
                "title": item.get("title"),
                "page_start": item.get("page_start"),
                "page_end": item.get("page_end"),
                "source": item.get("source"),
            } for item in headings],
            "coverage": classified["coverage"],
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"annual_report_coverage_error: {exc}",
        }
