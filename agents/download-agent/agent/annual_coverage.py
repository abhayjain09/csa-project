"""Annual Report section coverage for the Download Agent runtime.

This module is intentionally independent of the PageIndex agent. It reads the
already stored Annual Report, extracts grounded PDF or SEC HTML sections, then
uses the Download Agent's model callback for strict high-confidence
classification.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from html.parser import HTMLParser
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


def _heading_id(index: int) -> str:
    return f"heading-{index:04d}"


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
            # Some landscape PDFs contain megabytes of positioning whitespace
            # on one logical line. Never feed an unbounded line to regex/string
            # matching: it adds no useful heading evidence and can dominate the
            # whole AgentCore lifetime.
            if len(line) > 4_000:
                continue
            candidate = re.sub(
                r"[^a-z0-9]+", " ", line.casefold()).strip()
            if (normalized == candidate
                    or (len(words) >= 3 and normalized in candidate
                        and len(candidate) <= len(normalized) + 80)):
                return page_number
    return None


def _bounded_page_text(value: str, max_chars: int) -> str:
    """Normalize extractor output without retaining pathological layout lines."""
    kept = []
    used = 0
    for raw_line in str(value or "").splitlines():
        if len(raw_line) > 4_000:
            # Preserve words while collapsing the positioning whitespace that
            # made pypdf layout output grow to millions of characters.
            raw_line = re.sub(r"\s+", " ", raw_line).strip()
        if len(raw_line) > 4_000:
            raw_line = raw_line[:4_000]
        remaining = max_chars - used
        if remaining <= 0:
            break
        line = raw_line[:remaining]
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


def _parse_toc_row(raw_line: str) -> tuple[str, int] | None:
    """Parse ``Title .... 123`` / ``Title   123`` in strictly linear time."""
    value = str(raw_line or "").rstrip()
    if not value:
        return None
    end = len(value)
    start_digits = end
    while start_digits > 0 and value[start_digits - 1].isdigit():
        start_digits -= 1
    digits = value[start_digits:end]
    if not (1 <= len(digits) <= 4):
        return None
    separator_start = start_digits
    while (separator_start > 0
           and (value[separator_start - 1].isspace()
                or value[separator_start - 1] == ".")):
        separator_start -= 1
    separator = value[separator_start:start_digits]
    if not (separator.count(".") >= 2
            or sum(char.isspace() for char in separator) >= 3):
        return None
    title = _clean_heading(value[:separator_start])
    if not title:
        return None
    return title, int(digits)


def _extract_pdf_page_texts(body: bytes, reader: PdfReader) -> tuple[list[str], str]:
    """Extract page text with a hard wall-clock and memory bound.

    Poppler is the primary extractor because it handles complex/landscape PDFs
    much more predictably than pypdf's layout mode. The container installs it.
    Plain pypdf extraction remains a compatibility fallback, but layout mode is
    deliberately never used here.
    """
    timeout = max(5, int(os.environ.get(
        "ANNUAL_COVERAGE_PDFTEXT_TIMEOUT_SECONDS", "60")))
    max_page_chars = max(20_000, int(os.environ.get(
        "ANNUAL_COVERAGE_MAX_PAGE_TEXT_CHARS", "250000")))
    max_pages = max(1, int(os.environ.get(
        "ANNUAL_COVERAGE_MAX_PAGES", "2000")))
    expected_pages = min(len(reader.pages), max_pages)

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(body)
            handle.flush()
            completed = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", handle.name, "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        if completed.returncode == 0 and completed.stdout:
            decoded = completed.stdout.decode("utf-8", errors="replace")
            pages = decoded.split("\f")
            if pages and not pages[-1].strip():
                pages.pop()
            pages = [
                _bounded_page_text(page, max_page_chars)
                for page in pages[:expected_pages]
            ]
            if len(pages) < expected_pages:
                pages.extend([""] * (expected_pages - len(pages)))
            return pages, "pdftotext-layout-bounded"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"[annual-coverage] pdftotext unavailable/failed: {exc}")

    pages = []
    for page in reader.pages[:expected_pages]:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append(_bounded_page_text(text, max_page_chars))
    return pages, "pypdf-plain-bounded"


def extract_heading_index(body: bytes) -> list[dict]:
    """Read every text page and construct a conservative heading index.

    Printed page numbers are never trusted directly. A printed-TOC title must
    be found again on a later physical page. Image-only PDFs produce no
    references because this runtime intentionally has no OCR dependency.
    """
    reader = PdfReader(BytesIO(body), strict=False)
    page_texts, extractor = _extract_pdf_page_texts(body, reader)
    print(f"[annual-coverage] extracted {len(page_texts)} PDF page(s) "
          f"using {extractor}; chars={sum(map(len, page_texts))}")

    candidates = _outline_headings(reader)
    topic_terms = list(dict.fromkeys(
        term.casefold() for aliases in ALIASES.values() for term in aliases
    ))

    for toc_page, page_text in enumerate(page_texts[:40], start=1):
        for raw_line in page_text.splitlines():
            if len(raw_line) > 4_000:
                continue
            toc_entry = _parse_toc_row(raw_line)
            if not toc_entry:
                continue
            title, _printed_page = toc_entry
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
            if len(raw_line) > 4_000:
                continue
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
            "heading_id": _heading_id(index + 1),
            "title": item["title"],
            "path": item["title"],
            "location_type": "pdf_page",
            "location_label": (
                f"pages {page_start}-{page_end}"
                if page_end != page_start else f"page {page_start}"),
            "page_start": page_start,
            "page_end": page_end,
            "summary": re.sub(r"\s+", " ", "\n".join(opening))[:3500],
            "source": item["source"],
        })
    return headings


class _HtmlBlockParser(HTMLParser):
    """Collect visible, bounded text blocks without executing filing HTML."""

    _BLOCK_TAGS = {
        "article", "blockquote", "caption", "dd", "div", "dt", "h1",
        "h2", "h3", "h4", "h5", "h6", "li", "p", "section", "td",
        "th",
    }
    _SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._active: list[dict] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth or tag not in self._BLOCK_TAGS:
            return
        attributes = dict(attrs or [])
        self._active.append({
            "tag": tag,
            "anchor": str(attributes.get("id") or attributes.get("name") or "")[:300],
            "parts": [],
        })

    def handle_endtag(self, tag):
        tag = str(tag or "").lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        for index in range(len(self._active) - 1, -1, -1):
            if self._active[index]["tag"] != tag:
                continue
            block = self._active.pop(index)
            text = _clean_heading(" ".join(block["parts"]))
            if text:
                self.blocks.append({
                    "tag": tag,
                    "anchor": block["anchor"],
                    "text": text[:20_000],
                })
            break

    def handle_data(self, data):
        if self._skip_depth or not self._active:
            return
        text = re.sub(r"\s+", " ", str(data or "")).strip()
        if text:
            self._active[-1]["parts"].append(text)


def extract_html_heading_index(body: bytes) -> list[dict]:
    """Construct a conservative section index for SEC 10-K/20-F HTML.

    SEC filings frequently use ``div``/``td`` elements rather than semantic
    heading tags, so Item headings and short topic-bearing blocks are also
    indexed. Section ordinals are deliberately not exposed as PDF pages.
    """
    text = body.decode("utf-8", errors="replace")
    parser = _HtmlBlockParser()
    parser.feed(text)
    parser.close()

    blocks = []
    for block in parser.blocks:
        if (blocks and block["text"].casefold() == blocks[-1]["text"].casefold()
                and block.get("anchor") == blocks[-1].get("anchor")):
            continue
        blocks.append(block)

    topic_terms = list(dict.fromkeys(
        term.casefold() for aliases in ALIASES.values() for term in aliases
    ))
    item_heading = re.compile(
        r"^(?:part\s+[ivx]+\s+)?item\s+\d+[a-z]?(?:\.|\s|$)", re.I)
    candidates = []
    for block_index, block in enumerate(blocks):
        title = _clean_heading(block.get("text", ""))
        words = re.findall(r"[A-Za-z][A-Za-z&'/-]*", title)
        semantic = block.get("tag") in {"h1", "h2", "h3", "h4", "h5", "h6"}
        item = bool(item_heading.match(title))
        topic = (
            2 <= len(words) <= 28
            and 4 <= len(title) <= 240
            and any(term in title.casefold() for term in topic_terms)
        )
        if not (semantic or item or topic):
            continue
        if len(title) > 500:
            continue
        candidates.append({
            "block_index": block_index,
            "title": title,
            "anchor": block.get("anchor", ""),
            "source": (
                "html_heading" if semantic else
                "sec_item_heading" if item else "html_topic_heading"),
        })

    candidates = candidates[:2000]
    headings = []
    for index, item in enumerate(candidates):
        block_start = int(item["block_index"])
        next_start = (
            int(candidates[index + 1]["block_index"])
            if index + 1 < len(candidates) else len(blocks))
        summary_parts = []
        for block in blocks[block_start:min(next_start, block_start + 40)]:
            value = block.get("text", "")
            if value:
                summary_parts.append(value)
            if sum(len(value) for value in summary_parts) >= 3500:
                break
        section_index = index + 1
        headings.append({
            "heading_id": _heading_id(section_index),
            "title": item["title"],
            "path": item["title"],
            "location_type": "html_section",
            "location_label": f"HTML section {section_index}",
            "section_index": section_index,
            "anchor": item.get("anchor") or "",
            "summary": re.sub(r"\s+", " ", " ".join(summary_parts))[:3500],
            "source": item["source"],
        })
    return headings


def _document_kind(s3_key: str, content_type: str, body: bytes) -> str:
    key = str(s3_key or "").lower().split("?", 1)[0]
    ctype = str(content_type or "").lower()
    prefix = bytes(body[:4096]).lstrip().lower()
    if body.startswith(b"%PDF-") or key.endswith(".pdf") or "pdf" in ctype:
        return "pdf"
    if (key.endswith((".htm", ".html")) or "html" in ctype
            or prefix.startswith((b"<!doctype html", b"<html", b"<ix:html"))):
        return "html"
    return ""


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
    max_per_class = max(4, min(20, int(os.environ.get(
        "ANNUAL_COVERAGE_HEADINGS_PER_CLASS", "12"))))
    classes_per_call = max(1, min(8, int(os.environ.get(
        "ANNUAL_COVERAGE_CLASSES_PER_CALL", "5"))))
    relevant_by_class = {}
    for report_class, terms in terms_by_class.items():
        scored = []
        for position, heading in enumerate(headings):
            title = str(heading.get("title") or "").casefold()
            path = str(heading.get("path") or "").casefold()
            summary = str(heading.get("summary") or "")[:1600].casefold()
            title_hits = sum(1 for term in terms if term in title)
            path_hits = sum(1 for term in terms if term in path)
            summary_hits = sum(1 for term in terms if term in summary)
            if not (title_hits or path_hits or summary_hits):
                continue
            structural = 1 if heading.get("source") in {
                "pdf_bookmark", "printed_toc", "html_heading",
                "sec_item_heading",
            } else 0
            scored.append((
                title_hits * 100 + path_hits * 30 + structural * 10
                + min(summary_hits, 5),
                -position,
                heading,
            ))
        scored.sort(key=lambda value: (value[0], value[1]), reverse=True)
        relevant_by_class[report_class] = [
            {
                **value[2],
                "summary": str(value[2].get("summary") or "")[:900],
            }
            for value in scored[:max_per_class]
        ]

    active_classes = [
        value for value in requested if relevant_by_class.get(value)
    ]
    if not active_classes:
        return {"headings": headings, "coverage": {}}

    coverage = {}
    for start in range(0, len(active_classes), classes_per_call):
        class_batch = active_classes[start:start + classes_per_call]
        relevant_map = {}
        for report_class in class_batch:
            for heading in relevant_by_class[report_class]:
                relevant_map.setdefault(heading["heading_id"], heading)
        relevant = list(relevant_map.values())

        prompt = f"""You are auditing a company's Annual Report heading index.

Requested standalone document classes that were not found:
{json.dumps(class_batch, ensure_ascii=False)}

Grounded headings, exact location metadata, and section-opening text:
{json.dumps(relevant, ensure_ascii=False)}

For each requested class, identify either a GENUINE, DEDICATED, SUBSTANTIVE
section, or an exact DEDICATED REFERENCE section that explicitly identifies or
links the requested policy/report under a matching heading. A keyword, generic
risk factor, footnote, unrelated cross-reference, short compliance statement,
or passing mention is not enough. Use "substantive_section" when the opening
text shows sustained policies, governance, procedures, commitments, controls,
or disclosures. Use "dedicated_reference" only for an exact matching section
whose purpose is to identify, incorporate, or link that requested document.

Be strict about near-neighbours: supplier conduct is not employee conduct; risk
factors are not a risk-management policy; general ESG text is not automatically
a sustainability report; a tax footnote is not a tax strategy; and mentioning
bribery, conflicts, or insider trading is not a dedicated policy section.

Return JSON only:
{{"coverage": {{
  "<exact requested class>": {{
    "match": "substantive_section|dedicated_reference",
    "heading_id": "<exact supplied heading_id>",
    "heading": "<exact supplied heading>",
    "confidence": "high",
    "evidence": "<concise explanation grounded in supplied opening text>"
  }}
}}}}

Omit every class that is not high confidence. Never invent or adjust a
heading_id, heading, page range, section number, or anchor."""
        parsed = _parse_json_object(converse(prompt, 4000))
        raw_coverage = parsed.get("coverage") or {}
        indexed = {
            str(item.get("heading_id") or ""): item for item in relevant
        }
        for report_class, match in raw_coverage.items():
            canonical = str(report_class or "").strip().lower()
            if canonical not in class_batch or not isinstance(match, dict):
                continue
            match_type = str(match.get("match") or "")
            if (match_type not in {
                    "substantive_section", "dedicated_reference"
                    } or match.get("confidence") != "high"):
                continue
            heading_id = str(match.get("heading_id") or "").strip()
            heading = str(match.get("heading") or "").strip()
            source = indexed.get(heading_id)
            if source is None or heading.casefold() != str(
                    source.get("title") or "").casefold():
                continue
            grounded = {
                "match": match_type,
                "heading_id": heading_id,
                "heading": source["title"],
                "location_type": source.get("location_type", "pdf_page"),
                "location_label": source.get("location_label", ""),
                "confidence": "high",
                "evidence": str(match.get("evidence") or "").strip()[:1000],
            }
            for field in (
                    "page_start", "page_end", "section_index", "anchor"):
                if source.get(field) not in (None, ""):
                    grounded[field] = source[field]
            coverage[canonical] = grounded
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
            "ANNUAL_COVERAGE_MAX_DOCUMENT_BYTES",
            os.environ.get(
                "ANNUAL_COVERAGE_MAX_PDF_BYTES",
                str(100 * 1024 * 1024))))
        if size <= 0 or size > max_bytes:
            raise ValueError(
                f"annual report size {size} is outside allowed range")
        body = s3_client.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
        content_type = str(head.get("ContentType") or "")
        document_kind = _document_kind(s3_key, content_type, body)
        if document_kind == "pdf":
            invalid = integrity_error(s3_key, "application/pdf", body)
            if invalid:
                raise ValueError(invalid)
            headings = extract_heading_index(body)
        elif document_kind == "html":
            if len(body.strip()) < 500:
                raise ValueError("annual report HTML is empty or truncated")
            headings = extract_html_heading_index(body)
        else:
            raise ValueError(
                f"unsupported annual report content type: {content_type or 'unknown'}")
        classified = classify_coverage(headings, requested, converse)
        return {
            "status": "ok",
            "doc_name": s3_key.rsplit("/", 1)[-1],
            "document_kind": document_kind,
            "extractor": "download-agent-annual-coverage-v3-bounded",
            "headings": [{
                "heading_id": item.get("heading_id"),
                "title": item.get("title"),
                "location_type": item.get("location_type"),
                "location_label": item.get("location_label"),
                "page_start": item.get("page_start"),
                "page_end": item.get("page_end"),
                "section_index": item.get("section_index"),
                "anchor": item.get("anchor"),
                "source": item.get("source"),
            } for item in headings],
            "coverage": classified["coverage"],
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"annual_report_coverage_error: {exc}",
        }
